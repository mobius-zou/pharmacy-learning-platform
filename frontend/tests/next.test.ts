import { describe, expect, it } from "vitest";
import { DEFAULT_AFTER_LOGIN, loginPathFor, safeNextPath } from "@/shared/lib/next";

describe("登录后回跳路径（防开放重定向）", () => {
  it("接受站内绝对路径", () => {
    expect(safeNextPath("/review")).toBe("/review");
    expect(safeNextPath("/cards?tab=all")).toBe("/cards?tab=all");
    expect(safeNextPath("  /experiments  ")).toBe("/experiments");
  });

  it("缺失或空值回到默认首页", () => {
    expect(safeNextPath(null)).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath(undefined)).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("   ")).toBe(DEFAULT_AFTER_LOGIN);
  });

  it("拒绝外部站点与协议相对地址", () => {
    expect(safeNextPath("https://evil.com")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("//evil.com")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("//evil.com/review")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("javascript:alert(1)")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("evil.com")).toBe(DEFAULT_AFTER_LOGIN);
  });

  it("拒绝反斜杠变形与头注入", () => {
    expect(safeNextPath("/\\evil.com")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("/review\r\nX-Injected: 1")).toBe(DEFAULT_AFTER_LOGIN);
  });

  it("避免登录/注册页互相回跳", () => {
    expect(safeNextPath("/login")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("/login?next=/review")).toBe(DEFAULT_AFTER_LOGIN);
    expect(safeNextPath("/register")).toBe(DEFAULT_AFTER_LOGIN);
  });
});

describe("生成登录跳转", () => {
  it("带上原始路径与查询串", () => {
    expect(loginPathFor("/review")).toBe("/login?next=%2Freview");
    expect(loginPathFor("/cards", "?q=abc")).toBe("/login?next=%2Fcards%3Fq%3Dabc");
  });

  it("根路径不带 next（避免登录后回到根再跳一次）", () => {
    expect(loginPathFor("/")).toBe("/login");
  });
});
