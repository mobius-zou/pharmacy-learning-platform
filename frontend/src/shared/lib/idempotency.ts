/**
 * 幂等键：每次**提交**生成一个新的 UUID。
 *
 * 与后端约定：`POST /review/{id}/grade` 带 `Idempotency-Key`，
 * 同键重放只生效一次，不会重复推进 FSRS。
 *
 * 注意与「重试」的区别：本前端的策略是**每次用户确认提交都生成新键**，
 * 因为每次点击都是一次新的、用户明确确认过的操作；
 * 网络失败后由用户重新确认（并生成新键），而不是静默自动重试。
 */

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // 退化实现：仅用于极老浏览器/测试环境
  const random = Math.random().toString(36).slice(2, 10);
  return `idem-${Date.now().toString(36)}-${random}`;
}
