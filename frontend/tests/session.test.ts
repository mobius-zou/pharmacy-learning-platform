import { describe, expect, it } from "vitest";
import type { Card, GradeResponse, Rating } from "@/shared/api/types";
import {
  BLOCK_SIZE,
  RATING_LABELS,
  createSession,
  currentCard,
  encouragement,
  hitBlockBoundary,
  remainingCount,
  sessionReducer,
  summarizeSession,
} from "@/features/review/session";

function makeCard(id: number, stability: number | null = null): Card {
  return {
    id,
    user_id: 1,
    front: `卡片 ${id}`,
    back: "答案",
    tags: {},
    created_at: "2026-01-01T00:00:00+00:00",
    fsrs_stability: stability,
    fsrs_difficulty: null,
    fsrs_due: null,
    fsrs_last_review: null,
    fsrs_state: 1,
    fsrs_step: 0,
    is_new: stability === null,
    is_due: true,
  };
}

function makeResult(card: Card, rating: Rating, intervalDays: number, stability: number): GradeResponse {
  return {
    card: { ...card, fsrs_stability: stability },
    rating,
    rating_label: RATING_LABELS[rating],
    elapsed_days: 1,
    interval_days: intervalDays,
    interval_label: `${intervalDays} 天`,
    due: "2026-02-01T00:00:00+00:00",
    retrievability: 0.9,
  };
}

function makeCards(count: number): Card[] {
  return Array.from({ length: count }, (_, index) => makeCard(index + 1));
}

describe("会话状态机", () => {
  it("初始为概览，start 后进入专注", () => {
    const state = createSession(makeCards(3));
    expect(state.phase).toBe("overview");
    expect(remainingCount(state)).toBe(3);

    const running = sessionReducer(state, { type: "start" });
    expect(running.phase).toBe("focused");
    expect(currentCard(running)?.id).toBe(1);
  });

  it("空队列时 start 不会进入专注（避免空白页）", () => {
    const state = createSession([]);
    expect(sessionReducer(state, { type: "start" }).phase).toBe("overview");
  });

  it("评分推进到下一张，并保留翻面状态重置", () => {
    let state = sessionReducer(createSession(makeCards(2)), { type: "start" });
    state = sessionReducer(state, { type: "reveal" });
    expect(state.revealed).toBe(true);

    state = sessionReducer(state, { type: "grade", card: makeCard(1), result: makeResult(makeCard(1), 3, 4, 4) });

    expect(state.index).toBe(1);
    expect(state.revealed).toBe(false);
    expect(currentCard(state)?.id).toBe(2);
    expect(state.graded).toHaveLength(1);
  });

  it("未进入专注时评分被忽略（防止误触）", () => {
    const state = createSession(makeCards(2));
    const next = sessionReducer(state, { type: "grade", card: makeCard(1), result: makeResult(makeCard(1), 3, 1, 1) });
    expect(next.graded).toHaveLength(0);
  });

  it("每完成一个块（10 张）进入休息，且可跳过继续", () => {
    let state = sessionReducer(createSession(makeCards(BLOCK_SIZE + 3)), { type: "start" });

    for (let index = 0; index < BLOCK_SIZE; index += 1) {
      const card = currentCard(state);
      if (!card) throw new Error("队列不应提前为空");
      state = sessionReducer(state, { type: "grade", card, result: makeResult(card, 3, 5, 5) });
    }

    expect(state.phase).toBe("break");
    expect(state.graded).toHaveLength(BLOCK_SIZE);
    expect(hitBlockBoundary(state)).toBe(true);

    const continued = sessionReducer(state, { type: "continueAfterBreak" });
    expect(continued.phase).toBe("focused");
    expect(currentCard(continued)?.id).toBe(BLOCK_SIZE + 1);
  });

  it("最后一张评完直接进入成果页，不弹休息", () => {
    let state = sessionReducer(createSession(makeCards(BLOCK_SIZE)), { type: "start" });
    for (let index = 0; index < BLOCK_SIZE; index += 1) {
      const card = currentCard(state);
      if (!card) throw new Error("队列不应提前为空");
      state = sessionReducer(state, { type: "grade", card, result: makeResult(card, 3, 5, 5) });
    }

    expect(state.phase).toBe("done");
    expect(hitBlockBoundary(state)).toBe(false);
  });

  it("暂停回到概览，已评分记录保留（进度不丢）", () => {
    let state = sessionReducer(createSession(makeCards(4)), { type: "start" });
    const card = currentCard(state);
    if (!card) throw new Error("应有一张卡");
    state = sessionReducer(state, { type: "grade", card, result: makeResult(card, 3, 2, 2) });

    const paused = sessionReducer(state, { type: "pause" });

    expect(paused.phase).toBe("overview");
    expect(paused.graded).toHaveLength(1);
  });

  it("翻面只在专注态生效", () => {
    const state = createSession(makeCards(1));
    expect(sessionReducer(state, { type: "reveal" }).revealed).toBe(false);
  });

  it("restart 会清空评分记录并回到概览（等待用户显式开始）", () => {
    let state = sessionReducer(createSession(makeCards(2)), { type: "start" });
    const card = currentCard(state);
    if (!card) throw new Error("应有一张卡");
    state = sessionReducer(state, { type: "grade", card, result: makeResult(card, 3, 2, 2) });

    const restarted = sessionReducer(state, { type: "restart", cards: makeCards(5) });

    expect(restarted.graded).toHaveLength(0);
    expect(restarted.phase).toBe("overview");
    expect(restarted.queue).toHaveLength(5);

    // 显式开始后才进入专注
    expect(sessionReducer(restarted, { type: "start" }).phase).toBe("focused");
  });
});

describe("会话成果", () => {
  function gradedState(entries: Array<{ rating: Rating; interval: number; before: number | null; after: number }>) {
    let state = sessionReducer(createSession(makeCards(entries.length)), { type: "start" });
    for (const entry of entries) {
      const card = currentCard(state);
      if (!card) throw new Error("队列不应提前为空");
      const withStability = { ...card, fsrs_stability: entry.before };
      state = sessionReducer(state, {
        type: "grade",
        card: withStability,
        result: makeResult(withStability, entry.rating, entry.interval, entry.after),
      });
    }
    return state;
  }

  it("统计完成量、评分分布、平均间隔与掌握度变化", () => {
    const state = gradedState([
      { rating: 3, interval: 4, before: 2, after: 6 },
      { rating: 1, interval: 1, before: 10, after: 4 },
    ]);

    const summary = summarizeSession(state);

    expect(summary.completed).toBe(2);
    expect(summary.ratingCounts[3]).toBe(1);
    expect(summary.ratingCounts[1]).toBe(1);
    expect(summary.averageIntervalDays).toBeCloseTo(2.5);
    expect(summary.averageStabilityBefore).toBeCloseTo(6);
    expect(summary.averageStabilityAfter).toBeCloseTo(5);
    expect(summary.stabilityDelta).toBeCloseTo(-1);
  });

  it("新卡（无稳定度）不参与掌握度变化，避免 NaN", () => {
    const state = gradedState([{ rating: 3, interval: 3, before: null, after: 3 }]);

    const summary = summarizeSession(state);

    expect(summary.averageStabilityBefore).toBeNull();
    expect(summary.stabilityDelta).toBeNull();
  });

  it("空会话的成果页不产生假数据", () => {
    const summary = summarizeSession(createSession([]));

    expect(summary.completed).toBe(0);
    expect(summary.averageIntervalDays).toBeNull();
    expect(summary.stabilityDelta).toBeNull();
    expect(encouragement(summary)).toContain("一张卡片");
  });

  it("鼓励语随完成量变化，且不含排名/惩罚性措辞", () => {
    const many = summarizeSession(gradedState(Array.from({ length: 20 }, () => ({ rating: 3 as Rating, interval: 5, before: 1, after: 5 }))));
    const text = encouragement(many);
    expect(text.length).toBeGreaterThan(0);
    for (const banned of ["排名", "落后", "失败", "惩罚"]) {
      expect(text).not.toContain(banned);
    }
  });
});
