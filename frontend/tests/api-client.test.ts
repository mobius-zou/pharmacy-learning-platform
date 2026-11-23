import { afterEach, describe, expect, it, vi } from "vitest";
import { UNAUTHORIZED_EVENT, LEGACY_TOKEN_KEY, request } from "@/shared/api/client";
import { ApiError, messageFromBody } from "@/shared/api/errors";
import { newIdempotencyKey } from "@/shared/lib/idempotency";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function captureFetch(): ReturnType<typeof vi.fn> {
  const mock = vi.fn();
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

describe("Cookie 会话请求", () => {
  it("带 credentials: include，让浏览器自动携带 HttpOnly Cookie", async () => {
    const fetchMock = captureFetch();
    fetchMock.mockImplementation(async () => jsonResponse(200, { ok: true }));

    await request("/auth/me");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.credentials).toBe("include");
  });

  it("不再注入 Authorization 头（token 不在 JS 里）", async () => {
    window.localStorage.setItem(LEGACY_TOKEN_KEY, "legacy-token");
    const fetchMock = captureFetch();
    fetchMock.mockImplementation(async () => jsonResponse(200, {}));

    await request("/auth/me");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});

describe("幂等键", () => {
  it("写请求自动带 Idempotency-Key，读请求不带", async () => {
    const fetchMock = captureFetch();
    fetchMock.mockImplementation(async () => jsonResponse(200, {}));

    await request("/review/1/grade", { method: "POST", body: { rating: 3 } });
    await request("/review/due", { method: "GET" });

    const postHeaders = (fetchMock.mock.calls[0] as [string, RequestInit])[1].headers as Record<string, string>;
    const getHeaders = (fetchMock.mock.calls[1] as [string, RequestInit])[1].headers as Record<string, string>;
    expect(postHeaders["Idempotency-Key"]).toBeTruthy();
    expect(getHeaders["Idempotency-Key"]).toBeUndefined();
  });

  it("每次提交生成**不同**的键（重复请求由服务端按键幂等）", async () => {
    const fetchMock = captureFetch();
    fetchMock.mockImplementation(async () => jsonResponse(200, {}));

    await request("/review/1/grade", { method: "POST", body: { rating: 3 } });
    await request("/review/1/grade", { method: "POST", body: { rating: 3 } });

    const first = (fetchMock.mock.calls[0] as [string, RequestInit])[1].headers as Record<string, string>;
    const second = (fetchMock.mock.calls[1] as [string, RequestInit])[1].headers as Record<string, string>;
    expect(first["Idempotency-Key"]).not.toBe(second["Idempotency-Key"]);
  });

  it("可显式指定幂等键", async () => {
    const fetchMock = captureFetch();
    fetchMock.mockImplementation(async () => jsonResponse(200, {}));

    await request("/review/1/grade", { method: "POST", body: {}, idempotencyKey: "fixed-key" });

    const headers = (fetchMock.mock.calls[0] as [string, RequestInit])[1].headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBe("fixed-key");
  });

  it("幂等键是 UUID 格式（后端按字符串键存储）", () => {
    expect(newIdempotencyKey()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  });
});

describe("错误处理", () => {
  it("401 广播事件并清理旧单页遗留的 token", async () => {
    window.localStorage.setItem(LEGACY_TOKEN_KEY, "stale");
    const fetchMock = captureFetch();
    fetchMock.mockResolvedValue(jsonResponse(401, { error: "unauthorized", message: "登录已失效" }));
    const onUnauthorized = vi.fn();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);

    // 只发一次请求：401 会广播事件，重复请求会让计数变成 2
    const caught = await request("/auth/me").catch((error: unknown) => error);

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(401);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(window.localStorage.getItem(LEGACY_TOKEN_KEY)).toBeNull();
    window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  });

  it("展示后端返回的 message", async () => {
    const fetchMock = captureFetch();
    fetchMock.mockResolvedValue(
      jsonResponse(409, { error: "conflict", message: "该 Idempotency-Key 已用于不同的评分请求" }),
    );

    await expect(request("/review/1/grade", { method: "POST", body: {} })).rejects.toMatchObject({
      status: 409,
      message: "该 Idempotency-Key 已用于不同的评分请求",
    });
  });

  it("422 的 FastAPI detail 数组会被拼成可读文案", () => {
    const message = messageFromBody(422, {
      detail: [
        { loc: ["body", "rating"], msg: "Input should be less than or equal to 4", type: "less_than_equal" },
        { loc: ["body", "username"], msg: "String should have at least 3 characters", type: "string_too_short" },
      ],
    });

    expect(message).toContain("less than or equal to 4");
    expect(message).toContain("at least 3 characters");
  });

  it("413/429/5xx 在没有 message 时有兜底文案", () => {
    expect(messageFromBody(413, null)).toContain("过大");
    expect(messageFromBody(429, null)).toContain("频繁");
    expect(messageFromBody(500, null)).toContain("服务器");
  });

  it("网络错误归一化为 status=0 的 ApiError", async () => {
    const fetchMock = captureFetch();
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    await expect(request("/auth/me")).rejects.toMatchObject({
      status: 0,
      code: "network_error",
    });
  });
});

describe("响应解析", () => {
  it("204 返回 undefined（而不是尝试解析 JSON）", async () => {
    const fetchMock = captureFetch();
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(request("/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });
});
