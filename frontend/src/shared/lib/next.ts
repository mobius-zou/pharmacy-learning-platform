/**
 * 登录后回跳路径的安全校验（防开放重定向）。
 *
 * 只接受**站内绝对路径**：以单个 `/` 开头，且不是协议相对地址（`//host`）。
 * 其它情况一律回落到默认首页，避免 `?next=https://evil.com` 把用户带走。
 */

export const DEFAULT_AFTER_LOGIN = "/review";

const REJECTED_PREFIXES = ["/login", "/register"];

export function safeNextPath(raw: string | null | undefined): string {
  if (!raw) return DEFAULT_AFTER_LOGIN;
  const value = raw.trim();

  // 必须以单个斜杠开头 —— 排除 https://、//evil.com、javascript:
  if (!value.startsWith("/") || value.startsWith("//")) return DEFAULT_AFTER_LOGIN;
  // 反斜杠在部分浏览器里等价于斜杠（/\evil.com → //evil.com）
  if (value.includes("\\")) return DEFAULT_AFTER_LOGIN;
  // 头注入
  if (/[\r\n]/.test(value)) return DEFAULT_AFTER_LOGIN;
  // 避免登录页 / 注册页互相回跳造成循环
  if (REJECTED_PREFIXES.some((prefix) => value === prefix || value.startsWith(`${prefix}?`))) {
    return DEFAULT_AFTER_LOGIN;
  }
  return value;
}

/** 由当前地址生成「去登录并把原地址带上」的路径。 */
export function loginPathFor(pathname: string, search = ""): string {
  const target = `${pathname}${search}`;
  if (!target || target === "/") return "/login";
  return `/login?next=${encodeURIComponent(target)}`;
}
