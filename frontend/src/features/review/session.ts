/**
 * 复习会话的纯逻辑（不依赖 React，便于单测）。
 *
 * 学习状态曲线：
 *   进入（概览） → 专注（逐张） → 心流（每 10 张一次轻量休息） → 收束（成果页）
 *
 * 关键设计：
 * - 每个评分**立即**落库，因此随时退出不丢进度（「暂停」= 退出会话，无需保存）；
 * - 每 BLOCK_SIZE 张提示一次休息，可跳过；休息不阻塞、不惩罚；
 * - 成果页只做正向陈述，展示真实可得的指标（不编造连续天数）。
 */

import type { Card, GradeResponse, Rating } from "@/shared/api/types";

/** 每块卡片数：10 张一次轻量过渡（需求：10–12） */
export const BLOCK_SIZE = 10;

export interface GradedCard {
  cardId: number;
  rating: Rating;
  intervalDays: number;
  stabilityBefore: number | null;
  stabilityAfter: number | null;
  retrievability: number | null;
  due: string | null;
}

export type SessionPhase = "overview" | "focused" | "break" | "done";

export interface SessionState {
  phase: SessionPhase;
  queue: Card[];
  index: number;
  revealed: boolean;
  graded: GradedCard[];
  /** 本次会话开始前队列里的到期总数（用于进度分母） */
  plannedTotal: number;
}

export type SessionAction =
  | { type: "start" }
  | { type: "reveal" }
  | { type: "grade"; card: Card; result: GradeResponse }
  | { type: "takeBreak" }
  | { type: "continueAfterBreak" }
  | { type: "pause" }
  | { type: "restart"; cards: Card[] };

export function createSession(cards: Card[]): SessionState {
  return {
    phase: "overview",
    queue: cards,
    index: 0,
    revealed: false,
    graded: [],
    plannedTotal: cards.length,
  };
}

export function currentCard(state: SessionState): Card | null {
  if (state.phase !== "focused" && state.phase !== "break") return null;
  return state.queue[state.index] ?? null;
}

export function remainingCount(state: SessionState): number {
  return Math.max(0, state.queue.length - state.index);
}

/** 是否到达「块」的边界：刚评完 BLOCK_SIZE 的整数倍，且还有剩余卡片。 */
export function hitBlockBoundary(state: SessionState): boolean {
  return (
    state.graded.length > 0 &&
    state.graded.length % BLOCK_SIZE === 0 &&
    remainingCount(state) > 0
  );
}

export function sessionReducer(state: SessionState, action: SessionAction): SessionState {
  switch (action.type) {
    case "start":
      if (state.queue.length === 0) return { ...state, phase: "overview" };
      return { ...state, phase: "focused", index: 0, revealed: false, graded: [] };

    case "restart":
      // 只替换队列并回到概览：由用户显式点「开始复习」才进入专注态。
      // （否则数据到达就会自动开课，既没有开始按钮，也剥夺了用户的进入节奏。）
      return createSession(action.cards);

    case "reveal":
      if (state.phase !== "focused") return state;
      return { ...state, revealed: true };

    case "grade": {
      if (state.phase !== "focused") return state;
      const graded: GradedCard = {
        cardId: action.result.card.id,
        rating: action.result.rating as Rating,
        intervalDays: action.result.interval_days,
        stabilityBefore: action.card.fsrs_stability,
        stabilityAfter: action.result.card.fsrs_stability,
        retrievability: action.result.retrievability,
        due: action.result.due,
      };
      const nextGraded = [...state.graded, graded];
      const nextIndex = state.index + 1;
      const finished = nextIndex >= state.queue.length;
      const nextState: SessionState = {
        ...state,
        graded: nextGraded,
        index: nextIndex,
        revealed: false,
        phase: finished ? "done" : "focused",
      };
      if (!finished && hitBlockBoundary(nextState)) {
        return { ...nextState, phase: "break" };
      }
      return nextState;
    }

    case "takeBreak":
      if (state.phase !== "break") return state;
      return { ...state, phase: "break" };

    case "continueAfterBreak":
      if (state.phase !== "break") return state;
      return { ...state, phase: "focused" };

    case "pause":
      // 已评分的卡片已落库，暂停 = 回到概览
      return { ...state, phase: "overview", revealed: false };

    default:
      return state;
  }
}

export interface SessionSummary {
  completed: number;
  planned: number;
  /** 1..4 -> 数量 */
  ratingCounts: Record<Rating, number>;
  averageIntervalDays: number | null;
  averageStabilityBefore: number | null;
  averageStabilityAfter: number | null;
  /** 掌握度变化（平均 stability 的绝对增量，单位天） */
  stabilityDelta: number | null;
  averageRetrievability: number | null;
}

function average(values: number[]): number | null {
  if (values.length === 0) return null;
  const sum = values.reduce((acc, value) => acc + value, 0);
  return sum / values.length;
}

/** 汇总本次会话成果。所有指标都来自真实响应，不臆造。 */
export function summarizeSession(state: SessionState): SessionSummary {
  const ratingCounts: Record<Rating, number> = { 1: 0, 2: 0, 3: 0, 4: 0 };
  for (const item of state.graded) ratingCounts[item.rating] += 1;

  const intervals = state.graded.map((item) => item.intervalDays).filter(Number.isFinite);
  const before = state.graded
    .map((item) => item.stabilityBefore)
    .filter((value): value is number => typeof value === "number");
  const after = state.graded
    .map((item) => item.stabilityAfter)
    .filter((value): value is number => typeof value === "number");
  const retrievability = state.graded
    .map((item) => item.retrievability)
    .filter((value): value is number => typeof value === "number");

  const averageBefore = average(before);
  const averageAfter = average(after);

  return {
    completed: state.graded.length,
    planned: state.plannedTotal,
    ratingCounts,
    averageIntervalDays: average(intervals),
    averageStabilityBefore: averageBefore,
    averageStabilityAfter: averageAfter,
    stabilityDelta:
      averageBefore !== null && averageAfter !== null ? averageAfter - averageBefore : null,
    averageRetrievability: average(retrievability),
  };
}

/** 成果页文案：温和正向，不排名、不制造焦虑。 */
export function encouragement(summary: SessionSummary): string {
  if (summary.completed === 0) return "随时可以从一张卡片开始。";
  if (summary.completed >= 20) return "这一轮很扎实，记忆正在被稳稳地加固。";
  if (summary.completed >= BLOCK_SIZE) return "完成了一整个小节，保持这个节奏就好。";
  return "已经开始了，比昨天多认识了一点。";
}

export const RATING_LABELS: Record<Rating, string> = {
  1: "忘了",
  2: "困难",
  3: "一般",
  4: "简单",
};

export const RATING_HINTS: Record<Rating, string> = {
  1: "完全没印象，需要重新学",
  2: "想起来很吃力，间隔会缩短",
  3: "正常回忆，按计划推进",
  4: "几乎不用想，间隔会拉长",
};
