/**
 * 设计系统一致性 + 可读性（对比度）回归。
 *
 * 这些是**验收标准**里写死的量化项，必须能被机器验证：
 * - 间距只用 4/8/12/16/24/32；
 * - 圆角以 12–16px 为主（主控件用 md/lg）；
 * - 动效时长 160–240ms；
 * - 浅色/深色两套令牌必须**成对完整**（不是简单反色、也不许缺项）；
 * - 正文与次要文本对比度 ≥ 4.5:1。
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const STYLES = path.resolve(__dirname, "../src/shared/styles");
const tokensCss = readFileSync(path.join(STYLES, "tokens.css"), "utf8");
const componentsCss = readFileSync(path.join(STYLES, "components.css"), "utf8");
const featuresCss = readFileSync(path.join(STYLES, "features.css"), "utf8");

/** 取出某个主题块里的自定义属性 */
function tokensOf(selector: string): Record<string, string> {
  const start = tokensCss.indexOf(selector);
  if (start === -1) throw new Error(`找不到主题块 ${selector}`);
  const open = tokensCss.indexOf("{", start);
  const close = tokensCss.indexOf("}", open);
  const block = tokensCss.slice(open + 1, close);
  const result: Record<string, string> = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    const name = match[1];
    const value = match[2];
    if (name && value) result[name] = value.trim();
  }
  return result;
}

/** 基础块（间距/圆角/动效/字号）：`tokens.css` 开头那个 :root */
function baseTokens(): Record<string, string> {
  const open = tokensCss.indexOf(":root {");
  if (open === -1) throw new Error("找不到基础 :root 块");
  const brace = tokensCss.indexOf("{", open);
  const close = tokensCss.indexOf("}", brace);
  const block = tokensCss.slice(brace + 1, close);
  const result: Record<string, string> = {};
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    const name = match[1];
    const value = match[2];
    if (name && value) result[name] = value.trim();
  }
  return result;
}

const base = baseTokens();
const light = { ...base, ...tokensOf(":root,\n[data-theme=\"light\"]") };
const dark = { ...base, ...tokensOf("[data-theme=\"dark\"]") };

/* ---------------------------- 对比度计算 ---------------------------- */

function relativeLuminance(hex: string): number {
  const normalized = hex.replace("#", "");
  const channels = [0, 2, 4].map((index) =>
    Number.parseInt(normalized.slice(index, index + 2), 16) / 255,
  );
  const [r = 0, g = 0, b = 0] = channels.map((channel) =>
    channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(foreground: string, background: string): number {
  const [a, b] = [relativeLuminance(foreground), relativeLuminance(background)].sort(
    (left, right) => right - left,
  );
  return ((a ?? 0) + 0.05) / ((b ?? 0) + 0.05);
}

function hexOf(tokens: Record<string, string>, name: string): string {
  const value = tokens[name];
  if (!value) throw new Error(`缺少令牌 ${name}`);
  if (!/^#[0-9a-f]{6}$/i.test(value)) throw new Error(`令牌 ${name} 不是纯色：${value}`);
  return value;
}

/* ------------------------------- 间距 ------------------------------- */

describe("间距节奏", () => {
  it("只用 4/8/12/16/24/32", () => {
    const expected = ["4px", "8px", "12px", "16px", "24px", "32px"];
    const actual = ["--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6"].map(
      (name) => light[name],
    );
    expect(actual).toEqual(expected);
  });

  it("样式表里不出现奇数像素的 margin/padding（防止随意堆砌）", () => {
    const offenders: string[] = [];
    for (const [file, css] of [
      ["components.css", componentsCss],
      ["features.css", featuresCss],
    ] as const) {
      for (const match of css.matchAll(/(?:margin|padding)(?:-[a-z]+)?:\s*([^;]+);/g)) {
        const value = match[1] ?? "";
        if (/var\(/.test(value)) continue;
        for (const px of value.matchAll(/(\d+)px/g)) {
          const number = Number(px[1]);
          if (number > 0 && number % 4 !== 0) offenders.push(`${file}: ${value.trim()}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});

/* ------------------------------- 圆角 ------------------------------- */

describe("圆角刻度", () => {
  it("主控件使用 12–16px，卡片上限不超过 22px", () => {
    expect(light["--r-md"]).toBe("12px");
    expect(light["--r-lg"]).toBe("16px");
    expect(Number.parseInt(light["--r-xl"] ?? "0", 10)).toBeLessThanOrEqual(22);
  });

  it("按钮与面板分别引用 md / lg 令牌（而不是写死像素）", () => {
    expect(componentsCss).toMatch(/\.pl-btn[\s\S]*?border-radius:\s*var\(--r-md\)/);
    expect(componentsCss).toMatch(/\.pl-panel[\s\S]*?border-radius:\s*var\(--r-lg\)/);
  });
});

/* ------------------------------- 动效 ------------------------------- */

describe("动效时长", () => {
  it("三档时长都在 160–240ms", () => {
    for (const name of ["--t-fast", "--t-base", "--t-slow"]) {
      const value = light[name];
      expect(value, `缺少 ${name}`).toBeDefined();
      const ms = Number.parseInt((value ?? "").replace("ms", ""), 10);
      expect(ms, `${name}=${value} 超出 160–240ms`).toBeGreaterThanOrEqual(160);
      expect(ms, `${name}=${value} 超出 160–240ms`).toBeLessThanOrEqual(240);
    }
  });

  it("不使用弹跳超出型缓动（缓动必须自然）", () => {
    expect(light["--ease"]).toBeDefined();
    expect(light["--ease"]).not.toMatch(/back|elastic/i);
  });
});

/* ---------------------------- 主题完整性 ---------------------------- */

describe("浅色/深色令牌成对", () => {
  it("两套主题定义了同一组**颜色**令牌（基础块是共享的）", () => {
    const onlyColor = (tokens: Record<string, string>) =>
      Object.keys(tokens)
        .filter((name) => !(name in base))
        .sort();
    expect(onlyColor(light)).toEqual(onlyColor(dark));
  });

  it("颜色令牌数量足够支撑完整界面（≥ 25 项）", () => {
    const colorTokens = Object.keys(light).filter((name) => !(name in base));
    expect(colorTokens.length).toBeGreaterThanOrEqual(25);
  });

  it("两套主题都有非空值", () => {
    for (const [name, value] of Object.entries(dark)) {
      expect(value.trim(), `深色主题 ${name} 为空`).not.toBe("");
    }
  });

  it("深色不是浅色的简单反色（背景与前景各自独立定义）", () => {
    expect(light["--bg"]).not.toBe(dark["--bg"]);
    expect(light["--fg"]).not.toBe(dark["--fg"]);
    // 若只是反色，主色会变成浅色的补色；这里两者都是低饱和蓝绿
    expect(light["--primary"]).not.toBe(dark["--primary"]);
  });
});

/* ------------------------------- 对比度 ----------------------------- */

describe("文字对比度 ≥ 4.5:1", () => {
  const pairs: Array<[string, string, string, Record<string, string>]> = [
    ["浅色 正文 / 背景", "--fg", "--bg", light],
    ["浅色 正文 / 卡片", "--fg", "--surface", light],
    ["浅色 次要文本 / 卡片", "--fg-muted", "--surface", light],
    ["浅色 更次要文本 / 卡片", "--fg-subtle", "--surface", light],
    ["浅色 更次要文本 / 次级面板", "--fg-subtle", "--surface-2", light],
    ["浅色 主色文字 / 卡片", "--primary", "--surface", light],
    ["浅色 危险色文字 / 卡片", "--danger", "--surface", light],
    ["浅色 警示色文字 / 卡片", "--warn", "--surface", light],
    ["浅色 成功色文字 / 卡片", "--ok", "--surface", light],
    ["深色 正文 / 背景", "--fg", "--bg", dark],
    ["深色 正文 / 卡片", "--fg", "--surface", dark],
    ["深色 次要文本 / 卡片", "--fg-muted", "--surface", dark],
    ["深色 更次要文本 / 卡片", "--fg-subtle", "--surface", dark],
    ["深色 更次要文本 / 次级面板", "--fg-subtle", "--surface-2", dark],
    ["深色 主色文字 / 卡片", "--primary", "--surface", dark],
    ["深色 危险色文字 / 卡片", "--danger", "--surface", dark],
    ["深色 警示色文字 / 卡片", "--warn", "--surface", dark],
    ["深色 成功色文字 / 卡片", "--ok", "--surface", dark],
  ];

  for (const [label, fgToken, bgToken, theme] of pairs) {
    it(`${label}`, () => {
      const value = contrast(hexOf(theme, fgToken), hexOf(theme, bgToken));
      expect(value, `${label} 对比度仅 ${value.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5);
    });
  }
});

/* --------------------------- 品牌独特性 ---------------------------- */

describe("品牌元素与可访问性基础", () => {
  it("存在分子/胶囊等自有品牌图形样式", () => {
    expect(componentsCss).toContain(".pl-molecule__node");
    expect(componentsCss).toContain(".pl-capsule");
    expect(componentsCss).toContain(".pl-capsule::-webkit-progress-value");
  });

  it("有键盘焦点样式与减少动效兜底", () => {
    expect(readFileSync(path.join(STYLES, "base.css"), "utf8")).toMatch(/:focus-visible/);
    expect(readFileSync(path.join(STYLES, "base.css"), "utf8")).toContain("prefers-reduced-motion");
  });

  it("组件不使用内联 style（严格 CSP 的前提）", () => {
    const source = readFileSync(
      path.resolve(__dirname, "../src/shared/ui/primitives.tsx"),
      "utf8",
    );
    expect(source).not.toMatch(/style=\{\{/);
  });

  it("严格 CSP 只在构建期注入（开发期写死在 HTML 会拦掉 Vite 的内联样式）", () => {
    const html = readFileSync(path.resolve(__dirname, "../index.html"), "utf8");
    const config = readFileSync(path.resolve(__dirname, "../vite.config.ts"), "utf8");

    // 开发期 HTML 里不能有 CSP —— 否则页面会失去样式（实测过）
    expect(html).not.toContain("Content-Security-Policy");

    // 构建期由插件注入，且不含 unsafe-inline
    expect(config).toContain("pharm-inject-csp");
    expect(config).toContain("script-src 'self'");
    expect(config).toContain("style-src 'self'");
    expect(config).not.toContain("unsafe-inline");
  });

  it("窄屏（≤900px）有独立布局规则，侧栏转为底部导航", () => {
    expect(featuresCss).toMatch(/@media \(max-width: 900px\)/);
  });
});
