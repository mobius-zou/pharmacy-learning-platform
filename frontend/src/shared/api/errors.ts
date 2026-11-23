/**
 * 后端统一错误体：`{ error, message, detail }`（见 backend/app/errors.py）。
 * 前端只依赖 `message` 做展示，`error` 用于分支判断（401/409/413…）。
 */

export interface ApiErrorBody {
  error?: string;
  message?: string;
  detail?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }

  /** 网络层失败（未拿到 HTTP 响应） */
  get isNetworkError(): boolean {
    return this.status === 0;
  }

  get isAuthError(): boolean {
    return this.status === 401;
  }

  get isRateLimited(): boolean {
    return this.status === 429;
  }
}

/** 从任意响应体里尽力取出人类可读信息 */
export function messageFromBody(status: number, body: unknown): string {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const message = record.message;
    if (typeof message === "string" && message.trim()) return message.trim();

    // FastAPI 422：{ detail: [{ loc, msg, type }] }
    const detail = record.detail;
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) => {
          if (item && typeof item === "object" && typeof (item as { msg?: unknown }).msg === "string") {
            return (item as { msg: string }).msg;
          }
          return null;
        })
        .filter((value): value is string => Boolean(value));
      if (parts.length) return parts.join("；");
    }
    if (typeof detail === "string" && detail.trim()) return detail.trim();
  }
  if (status === 0) return "网络不可用，请检查连接后重试";
  if (status === 413) return "提交内容过大，已被服务器拒绝";
  if (status === 429) return "操作过于频繁，请稍后再试";
  if (status >= 500) return "服务器开小差了，请稍后重试";
  return `请求失败（HTTP ${status}）`;
}
