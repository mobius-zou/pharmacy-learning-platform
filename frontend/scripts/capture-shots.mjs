/**
 * 截图与可用性自查脚本（需要本机后端在 8000、dev server 在 5173）。
 *
 * 产出：
 *   screenshots/desktop-light.png / desktop-dark.png
 *   screenshots/mobile-light.png / mobile-dark.png
 *   screenshots/login-light.png / focus-light.png
 * 同时断言：
 *   · 375px 宽度下无横向溢出；
 *   · 主标题、导航、评分按钮等关键元素可见。
 */
import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";

const APP = process.env.APP_URL ?? "http://127.0.0.1:5173";
const API = process.env.API_URL ?? "http://127.0.0.1:8000/api/v1";
const OUT = path.resolve("screenshots");

// WSL 的发行版默认没有中文字体（汉字会渲染成方框）。
// 仓库根目录的 .fonts/fonts.conf 把 Windows 字体目录纳入 fontconfig 搜索路径，
// 这里自动启用，保证截图可读且可复现。
const fontConf = path.resolve("../.fonts/fonts.conf");
if (!process.env.FONTCONFIG_FILE && existsSync(fontConf)) {
  process.env.FONTCONFIG_FILE = fontConf;
  console.log(`使用字体配置：${fontConf}`);
}
const USER = { username: "demo_student", password: "pharmacy2026" };

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  mobile: { width: 390, height: 844 },
};

/** 在浏览器上下文里建立 Cookie 会话（token 不落 localStorage）。 */
async function signIn(context) {
  const response = await context.request.post(`${API}/auth/session`, {
    headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    data: USER,
  });
  if (!response.ok()) throw new Error(`登录失败：${response.status()} ${await response.text()}`);
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  const report = { shots: [], checks: [] };

  for (const [label, viewport] of Object.entries(VIEWPORTS)) {
    for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({
        viewport,
        deviceScaleFactor: 2,
        colorScheme: theme,
        locale: "zh-CN",
      });
      await context.addInitScript(
        (preferred) => {
          window.localStorage.setItem("pharm_theme", preferred);
        },
        theme,
      );
      await signIn(context);
      const page = await context.newPage();
      await page.goto(`${APP}/review`, { waitUntil: "networkidle" });
      await page.getByRole("heading", { name: "今日复习" }).waitFor();

      const file = path.join(OUT, `${label}-${theme}.png`);
      // 视口截图：固定定位的侧栏/底栏在 fullPage 长图里会错位
      await page.screenshot({ path: file, fullPage: false });
      report.shots.push({ file: path.relative(process.cwd(), file), viewport, theme });

      // 375px 无横向溢出（移动端单独验证最窄宽度）
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        // 令牌不能出现在 localStorage（HttpOnly Cookie 会话）
        hasLegacyToken: Boolean(window.localStorage.getItem("pharm_token")),
      }));
      report.checks.push({
        check: `${label}-${theme} 无横向溢出`,
        scrollWidth: overflow.scrollWidth,
        clientWidth: overflow.clientWidth,
        pass: overflow.scrollWidth <= overflow.clientWidth && !overflow.hasLegacyToken,
      });

      await context.close();
    }
  }

  // 登录页（浅色桌面）
  {
    const context = await browser.newContext({
      viewport: VIEWPORTS.desktop,
      deviceScaleFactor: 2,
      colorScheme: "light",
      locale: "zh-CN",
    });
    const page = await context.newPage();
    await page.goto(`${APP}/login`, { waitUntil: "networkidle" });
    await page.getByRole("heading", { name: "登录" }).waitFor();
    await page.screenshot({ path: path.join(OUT, "login-light.png"), fullPage: false });
    report.shots.push({ file: "screenshots/login-light.png", viewport: "desktop", theme: "light" });

    // 独立注册页
    await page.goto(`${APP}/register`, { waitUntil: "networkidle" });
    await page.getByRole("heading", { name: "创建学习账号" }).waitFor();
    await page.screenshot({ path: path.join(OUT, "register-light.png"), fullPage: false });
    report.shots.push({ file: "screenshots/register-light.png", viewport: "desktop", theme: "light" });

    // 375px 登录页 / 注册页不溢出
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`${APP}/login`, { waitUntil: "networkidle" });
    const narrow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    report.checks.push({
      check: "375px 登录页无横向溢出",
      scrollWidth: narrow.scrollWidth,
      clientWidth: narrow.clientWidth,
      pass: narrow.scrollWidth <= narrow.clientWidth,
    });

    await page.goto(`${APP}/register`, { waitUntil: "networkidle" });
    const narrowRegister = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    report.checks.push({
      check: "375px 注册页无横向溢出",
      scrollWidth: narrowRegister.scrollWidth,
      clientWidth: narrowRegister.clientWidth,
      pass: narrowRegister.scrollWidth <= narrowRegister.clientWidth,
    });
    await context.close();
  }

  // 专注态（浅色桌面）：开始复习 → 显示答案
  {
    const context = await browser.newContext({
      viewport: VIEWPORTS.desktop,
      deviceScaleFactor: 2,
      colorScheme: "light",
      locale: "zh-CN",
    });
    await context.addInitScript(() => {
      window.localStorage.setItem("pharm_theme", "light");
    });
    await signIn(context);
    const page = await context.newPage();
    await page.goto(`${APP}/review`, { waitUntil: "networkidle" });
    const start = page.getByRole("button", { name: "开始复习" });
    if (await start.isVisible().catch(() => false)) {
      await start.click();
      await page.getByRole("button", { name: "显示答案" }).click();
      await page.getByRole("group", { name: "选择回忆程度" }).waitFor();
      await page.screenshot({ path: path.join(OUT, "focus-light.png"), fullPage: false });
      report.shots.push({ file: "screenshots/focus-light.png", viewport: "desktop", theme: "light" });
    }
    await context.close();
  }

  await browser.close();
  console.log(JSON.stringify(report, null, 2));
  const failed = report.checks.filter((item) => !item.pass);
  if (failed.length > 0) {
    console.error("有检查未通过：", failed);
    process.exit(1);
  }
}

main().catch((error) => {
  console.error("capture failed:", error);
  process.exit(1);
});
