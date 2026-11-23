/**
 * 统一 API 客户端（Cookie 会话）。
 *
 * 安全模型：
 * - 令牌只存在于 **HttpOnly Cookie**（登录走 `POST /auth/session`），
 *   前端 JS 读不到，XSS 无法窃取凭据；
 * - 请求带 `credentials: "include"`，浏览器自动携带会话 Cookie；
 * - 401 统一广播事件，由 AuthProvider 清态并跳转 `/login?next=…`；
 * - 写请求自动带 `Idempotency-Key`（每次提交新 UUID）。
 *
 * 兼容：既有 `/ui/literature` 单页仍用 `Authorization: Bearer`，
 * 后端两种方式都接受，互不影响。
 */

import { ApiError, messageFromBody } from "./errors";
import { newIdempotencyKey } from "../lib/idempotency";

export const UNAUTHORIZED_EVENT = "pharm:unauthorized";

/** 旧单页使用的 localStorage 键名；正式前端**不再写入**，仅在 401 时清理。 */
export const LEGACY_TOKEN_KEY = "pharm_token";

const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.trim() || "/api/v1";

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  /** 显式指定幂等键；默认写请求自动生成 */
  idempotencyKey?: string;
  signal?: AbortSignal;
}

/** 401 时广播，让上层统一跳登录；顺手清掉旧单页遗留的 token。 */
function handleUnauthorized(): void {
  try {
    window.localStorage.removeItem(LEGACY_TOKEN_KEY);
  } catch {
    /* 忽略：隐私模式下不可写 */
  }
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
  }
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };

  let bodyInit: BodyInit | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    bodyInit = JSON.stringify(options.body);
  }

  if (method !== "GET") {
    headers["Idempotency-Key"] = options.idempotencyKey ?? newIdempotencyKey();
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: bodyInit,
      // 同源默认会带 Cookie；显式声明以便生产跨子域部署时也生效
      credentials: "include",
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "网络不可用，请检查连接后重试");
  }

  if (response.status === 401) {
    handleUnauthorized();
    const body = await safeJson(response);
    throw new ApiError(401, "unauthorized", messageFromBody(401, body), body);
  }

  if (!response.ok) {
    const body = await safeJson(response);
    const code =
      body && typeof body === "object" && typeof (body as { error?: unknown }).error === "string"
        ? (body as { error: string }).error
        : `http_${response.status}`;
    throw new ApiError(response.status, code, messageFromBody(response.status, body), body);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function safeJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export const api = {
  get: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "POST", body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "PATCH", body }),
  delete: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "DELETE" }),
};
