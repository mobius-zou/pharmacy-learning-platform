import { expect, test } from "@playwright/test";

/**
 * 认证与复习主链路。
 *
 * 关注点：
 * · 未登录 → /login?next=… → 登录后原路返回；
 * · 独立 /register 页；
 * · 令牌只在 HttpOnly Cookie 里（localStorage 不应有 pharm_token）；
 * · 评分必须二次确认，且请求带 Idempotency-Key；
 * · 登出后回到 /login 且会话失效。
 */

const unique = () => `e2e_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

test("未登录访问受保护页 → 带 next 跳登录 → 登录后原路返回", async ({ page }) => {
  await page.goto("/cards");

  await expect(page).toHaveURL(/\/login\?next=%2Fcards/);
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();

  // 用演示账号登录（seed-demo.mjs 创建）
  await page.getByLabel("用户名").fill("demo_student");
  await page.getByLabel("密码", { exact: true }).fill("pharmacy2026");
  await page.getByRole("button", { name: "登录", exact: true }).click();

  await expect(page).toHaveURL(/\/cards$/);
  await expect(page.getByRole("heading", { name: "卡片管理" })).toBeVisible();
});

test("注册（独立页面）→ 登录 → 评分一张卡 → 登出", async ({ page }) => {
  const username = unique();
  const password = "e2ePassword123";

  // 1) 独立注册页
  await page.goto("/register");
  await expect(page.getByRole("heading", { name: "创建学习账号" })).toBeVisible();
  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByLabel("确认密码").fill(password);
  await page.getByRole("button", { name: "注册并登录" }).click();

  await expect(page.getByRole("heading", { name: "今日复习" })).toBeVisible();

  // 2) 令牌不得落在 localStorage（HttpOnly Cookie 承载会话）
  const stored = await page.evaluate(() => window.localStorage.getItem("pharm_token"));
  expect(stored, "正式前端不应把 token 写进 localStorage").toBeNull();

  // 3) 新账号没有卡片：空状态给出下一步
  await expect(page.getByText("今天没有到期卡片")).toBeVisible();

  // 4) 通过 API 造一张卡（同一 Cookie 会话）
  const created = await page.request.post("/api/v1/cards", {
    headers: { "Content-Type": "application/json" },
    data: { front: "阿司匹林抑制哪种酶？", back: "环氧化酶（COX）" },
  });
  expect(created.ok()).toBeTruthy();

  // 5) 开始复习 → 翻面 → 评分（需确认）
  await page.reload();
  await page.getByRole("button", { name: "开始复习" }).click();
  await expect(page.getByText("阿司匹林抑制哪种酶？")).toBeVisible();
  await page.getByRole("button", { name: "显示答案" }).click();
  await expect(page.getByText("环氧化酶（COX）")).toBeVisible();

  await page.getByRole("button", { name: /^一般/ }).click();
  const dialog = page.locator("dialog[open]");
  await expect(dialog).toBeVisible();

  const gradeRequest = page.waitForRequest(
    (request) => request.url().includes("/grade") && request.method() === "POST",
  );
  await dialog.getByRole("button", { name: "确认评分" }).click();
  const request = await gradeRequest;
  expect(
    request.headers()["idempotency-key"],
    "评分请求必须带 Idempotency-Key",
  ).toMatch(/^[0-9a-f-]{36}$/i);

  await expect(page.getByRole("heading", { name: /这一轮完成了 1 张/ })).toBeVisible();

  // 6) 登出 → 回 /login，且受保护页再次拦截
  await page.getByRole("button", { name: "登出" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.goto("/review");
  await expect(page).toHaveURL(/\/login\?next=%2Freview/);
});

test("错误密码给出可读错误，且停留登录页", async ({ page }) => {
  await page.goto("/login");
  // 用户名每次唯一：否则多次运行会累积失败次数，触发限流而不是密码错误
  await page.getByLabel("用户名").fill(`no_such_${Date.now().toString(36)}`);
  await page.getByLabel("密码", { exact: true }).fill("wrong-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();

  await expect(page.getByRole("alert")).toContainText("用户名或密码错误");
  await expect(page).toHaveURL(/\/login$/);
});

test("拒绝外部 next，登录后回到默认首页（防开放重定向）", async ({ page }) => {
  await page.goto("/login?next=https%3A%2F%2Fevil.com%2Fsteal");
  await page.getByLabel("用户名").fill("demo_student");
  await page.getByLabel("密码", { exact: true }).fill("pharmacy2026");
  await page.getByRole("button", { name: "登录", exact: true }).click();

  await expect(page).toHaveURL(/127\.0\.0\.1:5173\/review$/);
});
