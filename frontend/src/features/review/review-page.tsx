import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "@/shared/api/errors";
import { useCardLogs, useDueCards, useGradeCard, usePreview } from "@/shared/api/queries";
import type { Rating } from "@/shared/api/types";
import { formatDateTime, formatRelative, masteryLabel, masteryLevel, tagSummary } from "@/shared/lib/format";
import { newIdempotencyKey } from "@/shared/lib/idempotency";
import { ConfirmDialog } from "@/shared/ui/dialog";
import { InfoHint } from "@/shared/ui/info-hint";
import { useToast } from "@/shared/ui/toast";
import {
  Badge,
  Button,
  CapsuleProgress,
  MoleculeGlyph,
  Panel,
  Progress,
  Skeleton,
  StateBlock,
  cx,
} from "@/shared/ui/primitives";
import { RatingBar } from "./rating-bar";
import {
  BLOCK_SIZE,
  RATING_LABELS,
  encouragement,
  createSession,
  currentCard,
  remainingCount,
  sessionReducer,
  summarizeSession,
} from "./session";

const DUE_LIMIT = 50;

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

export function ReviewPage() {
  const toast = useToast();
  const due = useDueCards(DUE_LIMIT);
  const [session, dispatch] = useReducer(sessionReducer, createSession([]));
  const [focusMode, setFocusMode] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [pendingRating, setPendingRating] = useState<Rating | null>(null);
  const [gradeError, setGradeError] = useState<string | null>(null);

  const card = currentCard(session);
  const preview = usePreview(card?.id ?? null);
  const logs = useCardLogs(historyOpen ? (card?.id ?? null) : null);
  const grade = useGradeCard();

  const summary = useMemo(() => summarizeSession(session), [session]);
  const isOverdue = session.phase === "focused" || session.phase === "break";

  // 队列到达后同步进会话（只在概览态同步，避免打断进行中的会话）
  useEffect(() => {
    if (!due.data) return;
    if (session.phase !== "overview") return;
    dispatch({ type: "restart", cards: due.data.items });
  }, [due.data, session.phase]);

  // 专注模式：隐藏导航与次要面板（由 AppShell 响应 data 属性）
  useEffect(() => {
    document.documentElement.dataset.focus = focusMode ? "on" : "off";
    return () => {
      delete document.documentElement.dataset.focus;
    };
  }, [focusMode]);

  const requestGrade = useCallback((rating: Rating) => {
    if (!card || grade.isPending) return;
    setPendingRating(rating);
  }, [card, grade.isPending]);

  const confirmGrade = useCallback(() => {
    if (!card || pendingRating === null) return;
    const chosen = pendingRating;
    setGradeError(null);
    grade.mutate(
      { cardId: card.id, payload: { rating: chosen }, idempotencyKey: newIdempotencyKey() },
      {
        onSuccess: (result) => {
          dispatch({ type: "grade", card, result });
          setPendingRating(null);
          toast.push(
            `${RATING_LABELS[chosen]} · 下次复习 ${result.interval_label}`,
            chosen === 1 ? "warn" : "ok",
          );
        },
        onError: (error) => {
          const message =
            error instanceof ApiError ? error.message : "评分失败，请重试";
          setPendingRating(null);
          setGradeError(message);
        },
      },
    );
  }, [card, pendingRating, grade, toast]);

  // 键盘：Space/Enter 翻面，1–4 选择评分，Esc 退出专注/暂停
  useEffect(() => {
    if (typeof window === "undefined") return;
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (isTypingTarget(event.target)) return;
      if (document.querySelector("dialog[open]")) return;
      if (event.key === "Escape") {
        if (focusMode) {
          setFocusMode(false);
          return;
        }
        if (isOverdue) {
          dispatch({ type: "pause" });
          toast.push("已暂停，进度已保存", "info");
        }
        return;
      }
      if (session.phase !== "focused") return;
      if (event.key === " " || event.key === "Enter") {
        event.preventDefault();
        dispatch({ type: "reveal" });
        return;
      }
      if (["1", "2", "3", "4"].includes(event.key) && session.revealed) {
        event.preventDefault();
        requestGrade(Number(event.key) as Rating);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [focusMode, isOverdue, requestGrade, session.phase, session.revealed, toast]);

  function start() {
    dispatch({ type: "start" });
    setGradeError(null);
  }

  /* ------------------------------ 渲染 ------------------------------ */

  if (due.isLoading) {
    return (
      <div className="pl-page">
        <header className="pl-page__head">
          <h1 className="pl-page__title">今日复习</h1>
        </header>
        <Panel>
          <Skeleton lines={4} />
        </Panel>
      </div>
    );
  }

  if (due.isError) {
    const message = due.error instanceof ApiError ? due.error.message : "无法加载到期卡片";
    return (
      <div className="pl-page">
        <header className="pl-page__head">
          <h1 className="pl-page__title">今日复习</h1>
        </header>
        <Panel>
          <StateBlock
            tone="error"
            title="没能取到今天的到期卡片"
            description={message}
            action={
              <Button onClick={() => void due.refetch()} loading={due.isFetching}>
                重新加载
              </Button>
            }
          />
        </Panel>
      </div>
    );
  }

  const totalDue = due.data?.total_due ?? 0;
  const weakCount = due.data?.weak_point_count ?? 0;

  return (
    <div className={cx("pl-page", focusMode && "is-focus")}>
      <header className="pl-page__head">
        <div>
          <h1 className="pl-page__title">今日复习</h1>
          <p className="pl-page__subtitle">
            共 <strong>{totalDue}</strong> 张到期
            {weakCount > 0 ? `，其中 ${weakCount} 个薄弱知识点优先` : ""}
            <InfoHint
              term="FSRS"
              explanation="一种基于记忆稳定度的间隔重复算法：每张卡片有「稳定度」和「难度」，评分会据此推算下次复习时间。"
            />
          </p>
        </div>
        <div className="pl-page__actions">
          {isOverdue ? (
            <>
              <Button variant="ghost" onClick={() => setFocusMode((value) => !value)}>
                {focusMode ? "退出专注" : "专注模式"}
              </Button>
              <Button variant="ghost" onClick={() => dispatch({ type: "pause" })}>
                暂停
              </Button>
            </>
          ) : null}
          <Button variant="ghost" onClick={() => void due.refetch()} loading={due.isFetching}>
            刷新
          </Button>
        </div>
      </header>

      {session.phase === "overview" ? (
        <OverviewPhase
          totalDue={totalDue}
          weakCount={weakCount}
          cards={due.data?.items ?? []}
          weakCardIds={new Set(due.data?.weak_card_ids ?? [])}
          onStart={start}
        />
      ) : null}

      {session.phase === "focused" && card ? (
        <FocusedPhase
          card={card}
          revealed={session.revealed}
          progress={{ done: session.graded.length, total: Math.max(session.plannedTotal, session.graded.length) }}
          remaining={remainingCount(session)}
          options={preview.data?.options}
          previewLoading={preview.isLoading}
          grading={grade.isPending}
          gradeError={gradeError}
          historyOpen={historyOpen}
          history={logs.data}
          historyLoading={logs.isLoading}
          onReveal={() => dispatch({ type: "reveal" })}
          onChoose={requestGrade}
          onToggleHistory={() => setHistoryOpen((value) => !value)}
          onPause={() => dispatch({ type: "pause" })}
          onRetry={() => {
            if (pendingRating !== null) requestGrade(pendingRating);
          }}
        />
      ) : null}

      {session.phase === "break" ? (
        <BreakPhase
          done={session.graded.length}
          remaining={remainingCount(session)}
          onContinue={() => dispatch({ type: "continueAfterBreak" })}
          onPause={() => dispatch({ type: "pause" })}
        />
      ) : null}

      {session.phase === "done" ? (
        <SummaryPhase
          summary={summary}
          onRestart={async () => {
            // 重新拉取到期队列：上一轮已评分的卡片不应再出现在本轮
            const fresh = await due.refetch();
            dispatch({ type: "restart", cards: fresh.data?.items ?? [] });
            dispatch({ type: "start" });
          }}
          onBack={() => dispatch({ type: "pause" })}
        />
      ) : null}

      <ConfirmDialog
        open={pendingRating !== null}
        title={pendingRating !== null ? `确认为「${RATING_LABELS[pendingRating]}」？` : "确认评分"}
        description={
          pendingRating !== null ? (
            <div className="pl-confirm">
              <p className="pl-confirm__card">{card?.front.slice(0, 120)}</p>
              <p className="pl-confirm__hint">
                下次复习：
                <strong>
                  {preview.data?.options.find((option) => option.rating === pendingRating)
                    ?.interval_label ?? "…"}
                </strong>
                。评分会立即写入并推进调度，确认后不可撤销。
              </p>
            </div>
          ) : null
        }
        confirmLabel="确认评分"
        loading={grade.isPending}
        onConfirm={confirmGrade}
        onCancel={() => setPendingRating(null)}
      />
    </div>
  );
}

/* ------------------------------ 概览 ------------------------------ */

interface OverviewProps {
  totalDue: number;
  weakCount: number;
  cards: Array<{ id: number; front: string; tags: Record<string, unknown>; fsrs_stability: number | null; fsrs_due: string | null; is_new: boolean }>;
  weakCardIds: Set<number>;
  onStart: () => void;
}

function OverviewPhase({ totalDue, weakCount, cards, weakCardIds, onStart }: OverviewProps) {
  if (totalDue === 0) {
    return (
      <Panel>
        <StateBlock
          title="今天没有到期卡片"
          description={
            <>
              这不是坏事 —— 记忆正在按计划沉淀。想继续学习的话，可以从文献工作台把知识点
              转成卡片，或稍后再来看看。
            </>
          }
          action={
            <div className="pl-inline-actions">
              <Link className="pl-link-button" to="/literature">
                去文献工作台转卡片
              </Link>
              <Link className="pl-link-button pl-link-button--ghost" to="/cards">
                手动添加卡片
              </Link>
            </div>
          }
        />
      </Panel>
    );
  }

  return (
    <>
      <Panel tone="soft" className="pl-start-panel">
        <div className="pl-start-panel__main">
          <CapsuleProgress value={0} max={totalDue} label="本次进度" />
          <div>
            <h2 className="pl-start-panel__title">
              准备好了就开始，共 {Math.min(totalDue, DUE_LIMIT)} 张
            </h2>
            <p className="pl-muted">
              每 {BLOCK_SIZE} 张会有一次轻量休息提示，可随时跳过或暂停；进度是逐张保存的。
              {weakCount > 0 ? " 薄弱知识点的卡片排在最前面。" : ""}
            </p>
          </div>
        </div>
        <Button size="lg" onClick={onStart}>
          开始复习
        </Button>
      </Panel>

      <Panel className="pl-queue">
        <div className="pl-queue__head">
          <h2 className="pl-section-title">队列预览</h2>
          <span className="pl-muted">按薄弱点优先、再到期的顺序排列</span>
        </div>
        <ul className="pl-cardlist">
          {cards.slice(0, 8).map((card) => (
            <li key={card.id} className="pl-cardlist__item">
              <span
                className={cx("pl-mastery-dot", `pl-mastery-dot--${masteryLevel(card.fsrs_stability)}`)}
                aria-hidden="true"
              />
              <div className="pl-cardlist__body">
                <p className="pl-cardlist__front">{card.front}</p>
                <p className="pl-muted pl-cardlist__meta">
                  {tagSummary(card.tags)[0] ?? "未分类"} · {masteryLabel(card.fsrs_stability)}
                  {card.is_new ? " · 新卡" : ` · 到期 ${formatRelative(card.fsrs_due)}`}
                </p>
              </div>
              {weakCardIds.has(card.id) ? <Badge tone="warn">薄弱点</Badge> : null}
            </li>
          ))}
        </ul>
        {cards.length > 8 ? (
          <p className="pl-muted pl-queue__more">还有 {cards.length - 8} 张，开始后依次出现</p>
        ) : null}
      </Panel>
    </>
  );
}

/* ------------------------------ 专注 ------------------------------ */

interface FocusedProps {
  card: { id: number; front: string; back: string; tags: Record<string, unknown>; fsrs_stability: number | null; is_new: boolean };
  revealed: boolean;
  progress: { done: number; total: number };
  remaining: number;
  options: import("@/shared/api/types").PreviewOption[] | undefined;
  previewLoading: boolean;
  grading: boolean;
  gradeError: string | null;
  historyOpen: boolean;
  history: import("@/shared/api/types").ReviewLog[] | undefined;
  historyLoading: boolean;
  onReveal: () => void;
  onChoose: (rating: Rating) => void;
  onToggleHistory: () => void;
  onPause: () => void;
  onRetry: () => void;
}

function FocusedPhase({
  card,
  revealed,
  progress,
  remaining,
  options,
  previewLoading,
  grading,
  gradeError,
  historyOpen,
  history,
  historyLoading,
  onReveal,
  onChoose,
  onToggleHistory,
  onPause,
  onRetry,
}: FocusedProps) {
  return (
    <div className="pl-focus">
      <div className="pl-focus__progress">
        <Progress value={progress.done} max={progress.total} label="本次复习进度" />
        <span className="pl-muted">
          已完成 {progress.done} / {progress.total} · 剩余 {remaining}
        </span>
      </div>

      <Panel className="pl-flashcard" aria-live="polite">
        <div className="pl-flashcard__meta">
          <Badge tone={card.is_new ? "info" : "neutral"}>{card.is_new ? "新卡" : "复习"}</Badge>
          <span className="pl-muted">
            {masteryLabel(card.fsrs_stability)}
            {tagSummary(card.tags).length ? ` · ${tagSummary(card.tags).join(" / ")}` : ""}
          </span>
        </div>

        <h2 className="pl-flashcard__front">{card.front}</h2>

        {revealed ? (
          <div className="pl-flashcard__back">
            <div className="pl-flashcard__divider" aria-hidden="true" />
            <p>{card.back}</p>
          </div>
        ) : (
          <div className="pl-flashcard__reveal">
            <Button size="lg" onClick={onReveal}>
              显示答案
            </Button>
            <span className="pl-muted">空格 / Enter</span>
          </div>
        )}
      </Panel>

      {gradeError ? (
        <Panel tone="soft" className="pl-inline-error" role="alert">
          <span>{gradeError}</span>
          <Button variant="ghost" onClick={onRetry}>
            重试
          </Button>
        </Panel>
      ) : null}

      {revealed ? (
        <RatingBar
          options={options}
          loading={previewLoading}
          disabled={grading}
          onChoose={onChoose}
        />
      ) : null}

      <div className="pl-focus__footer">
        <div className="pl-inline-actions">
          <Button variant="ghost" onClick={onToggleHistory} aria-expanded={historyOpen}>
            复习历史
          </Button>
          <Button variant="ghost" onClick={onPause}>
            暂停
          </Button>
        </div>
        <span className="pl-muted">1–4 评分 · 空格翻面 · Esc 暂停</span>
      </div>

      {historyOpen ? (
        <Panel className="pl-history">
          <h3 className="pl-section-title">这张卡的最近记录</h3>
          {historyLoading ? (
            <Skeleton lines={2} />
          ) : history && history.length > 0 ? (
            <ul className="pl-history__list">
              {history.slice(0, 6).map((log) => (
                <li key={log.id}>
                  <span className="pl-history__label">{log.rating_label}</span>
                  <span className="pl-muted">
                    {formatDateTime(log.reviewed_at)} · 间隔 {log.elapsed_days.toFixed(1)} 天
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="pl-muted">还没有复习记录，这是第一次。</p>
          )}
        </Panel>
      ) : null}
    </div>
  );
}

/* ------------------------------ 休息 ------------------------------ */

function BreakPhase({
  done,
  remaining,
  onContinue,
  onPause,
}: {
  done: number;
  remaining: number;
  onContinue: () => void;
  onPause: () => void;
}) {
  return (
    <Panel className="pl-break">
      <div className="pl-break__glyph" aria-hidden="true">
        <MoleculeGlyph size={56} />
      </div>
      <h2 className="pl-break__title">已经完成 {done} 张</h2>
      <p className="pl-muted pl-break__hint">
        还有 {remaining} 张。可以起身活动 5 秒、看看远处 —— 记忆在休息时更容易巩固。
      </p>
      <div className="pl-inline-actions">
        <Button size="lg" onClick={onContinue}>
          继续复习
        </Button>
        <Button variant="ghost" size="lg" onClick={onPause}>
          稍后再说
        </Button>
      </div>
    </Panel>
  );
}

/* ------------------------------ 成果 ------------------------------ */

function SummaryPhase({
  summary,
  onRestart,
  onBack,
}: {
  summary: ReturnType<typeof summarizeSession>;
  onRestart: () => void;
  onBack: () => void;
}) {
  const delta = summary.stabilityDelta;
  return (
    <Panel className="pl-summary">
      <div className="pl-summary__badge" aria-hidden="true">
        <MoleculeGlyph size={52} />
      </div>
      <h2 className="pl-summary__title">这一轮完成了 {summary.completed} 张</h2>
      <p className="pl-summary__lede">{encouragement(summary)}</p>

      <div className="pl-summary__stats">
        <div className="pl-stat">
          <span className="pl-stat__value">{summary.completed}</span>
          <span className="pl-stat__label">完成卡片</span>
        </div>
        <div className="pl-stat">
          <span className="pl-stat__value">
            {summary.averageIntervalDays === null ? "—" : `${summary.averageIntervalDays.toFixed(1)} 天`}
          </span>
          <span className="pl-stat__label">平均下次间隔</span>
        </div>
        <div className="pl-stat">
          <span className={cx("pl-stat__value", delta !== null && delta >= 0 && "is-positive")}>
            {delta === null ? "—" : `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} 天`}
          </span>
          <span className="pl-stat__label">掌握度变化</span>
        </div>
      </div>

      <div className="pl-summary__ratings">
        {([1, 2, 3, 4] as Rating[]).map((rating) => (
          <span key={rating} className={cx("pl-summary__rating", `is-${rating}`)}>
            {RATING_LABELS[rating]} <strong>{summary.ratingCounts[rating]}</strong>
          </span>
        ))}
      </div>

      <div className="pl-inline-actions pl-summary__actions">
        <Button size="lg" onClick={onRestart}>
          再来一轮
        </Button>
        <Button variant="ghost" size="lg" onClick={onBack}>
          回到概览
        </Button>
      </div>
    </Panel>
  );
}
