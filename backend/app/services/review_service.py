"""FSRS-6 复习调度核心服务。

设计要点
--------
1. **纯逻辑、无数据库依赖**：本模块只接受/返回不可变快照（``FsrsSnapshot``）与
   结果对象（``ScheduleResult``），因此可以在没有 MySQL 的环境下完整单测。
2. **无 Agent 降级模式**：调度完全由本地 py-fsrs 完成，不依赖任何 DSH 插件或外部
   AI 服务；DSH 仅在开发阶段提供增强，核心业务闭环始终保持可用。
3. **时区纪律**：py-fsrs 强制要求 aware UTC。所有进入本模块的时间先经
   ``to_aware_utc`` 归一化，所有落库时间经 ``to_naive_utc`` 归一化。

评分映射（与前端四档按钮一致）::

    1 = Again = 忘了
    2 = Hard  = 困难
    3 = Good  = 一般
    4 = Easy  = 简单
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Sequence

from fsrs import Card as FsrsCard
from fsrs import Rating, Scheduler, State

from ..config import settings
from ..logging_config import get_logger
from ..timeutils import days_between, to_aware_utc, to_naive_utc, utcnow

logger = get_logger(__name__)

# --- 评分常量 ---------------------------------------------------------------

RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4

RATING_LABELS: dict[int, str] = {
    RATING_AGAIN: "忘了",
    RATING_HARD: "困难",
    RATING_GOOD: "一般",
    RATING_EASY: "简单",
}

VALID_RATINGS: tuple[int, ...] = (RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY)

# 新卡的初始状态：py-fsrs 6.x 的 State 没有 New，新卡即 Learning
NEW_CARD_STATE = int(State.Learning)


def rating_label(rating: int) -> str:
    return RATING_LABELS.get(int(rating), f"未知({rating})")


def _to_fsrs_rating(rating: int) -> Rating:
    """int(1..4) -> fsrs.Rating，非法值直接报错。"""
    value = int(rating)
    if value not in VALID_RATINGS:
        raise ValueError(
            f"非法评分 {rating!r}，允许值为 "
            + "、".join(f"{k}({v})" for k, v in RATING_LABELS.items())
        )
    return Rating(value)


# --- 数据结构 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FsrsSnapshot:
    """一张卡片调度状态的不可变快照（时间均为 aware UTC）。"""

    card_id: int | None = None
    state: int = NEW_CARD_STATE
    step: int | None = None
    stability: float | None = None
    difficulty: float | None = None
    due: datetime | None = None
    last_review: datetime | None = None

    @property
    def is_new(self) -> bool:
        return self.last_review is None and self.stability is None


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """一次评分/预览的结果。"""

    snapshot: FsrsSnapshot
    rating: int
    rating_label: str
    reviewed_at: datetime
    elapsed_days: float
    interval_days: float
    retrievability: float | None = None

    @property
    def due(self) -> datetime | None:
        """下次复习时间（aware UTC）。

        快照内部按落库约定保存 naive UTC，对外一律给出 aware UTC，
        否则与 ``reviewed_at`` 做减法会抛 TypeError。
        """
        return to_aware_utc(self.snapshot.due)

    @property
    def interval_label(self) -> str:
        """人类可读的间隔（用于前端按钮提示）。"""
        return format_interval(self.interval_days)


def format_interval(days: float | None) -> str:
    """把天数格式化为「N 分钟 / N 小时 / N 天 / N 个月 / N 年」。"""
    if days is None:
        return "-"
    if days < 0:
        days = 0.0
    minutes = days * 24 * 60
    if minutes < 60:
        return f"{max(1, round(minutes))} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{round(hours)} 小时"
    if days < 30:
        return f"{round(days)} 天"
    if days < 365:
        return f"{days / 30:.1f} 个月"
    return f"{days / 365:.1f} 年"


# --- 核心调度器 -------------------------------------------------------------


class ReviewScheduler:
    """py-fsrs 调度的薄封装，面向本平台的数据模型。

    实现 ``protocols.SchedulingEngine``。
    """

    def __init__(
        self,
        desired_retention: float | None = None,
        enable_fuzzing: bool | None = None,
        maximum_interval: int | None = None,
        parameters: Sequence[float] | None = None,
    ) -> None:
        self.desired_retention = (
            settings.fsrs_desired_retention if desired_retention is None else desired_retention
        )
        self.enable_fuzzing = (
            settings.fsrs_enable_fuzzing if enable_fuzzing is None else enable_fuzzing
        )
        self.maximum_interval = (
            settings.fsrs_maximum_interval if maximum_interval is None else maximum_interval
        )
        self._parameters = parameters

        scheduler_kwargs: dict[str, Any] = {
            "desired_retention": self.desired_retention,
            "enable_fuzzing": self.enable_fuzzing,
            "maximum_interval": self.maximum_interval,
        }
        if parameters is not None:
            scheduler_kwargs["parameters"] = tuple(parameters)

        self.scheduler = Scheduler(**scheduler_kwargs)
        # 预览用：关闭 fuzzing，避免按钮上的间隔数字随机跳动
        preview_kwargs = dict(scheduler_kwargs)
        preview_kwargs["enable_fuzzing"] = False
        self._preview_scheduler = Scheduler(**preview_kwargs)

        logger.debug(
            "ReviewScheduler 初始化 desired_retention=%s enable_fuzzing=%s maximum_interval=%s",
            self.desired_retention,
            self.enable_fuzzing,
            self.maximum_interval,
        )

    # --- 快照 <-> py-fsrs Card -------------------------------------------

    @staticmethod
    def _to_fsrs_card(snapshot: FsrsSnapshot) -> FsrsCard:
        """快照 -> fsrs.Card。每次新建，避免 py-fsrs 内部原地修改污染快照。"""
        return FsrsCard(
            card_id=snapshot.card_id,
            state=State(int(snapshot.state)),
            step=snapshot.step,
            stability=snapshot.stability,
            difficulty=snapshot.difficulty,
            due=to_aware_utc(snapshot.due),
            last_review=to_aware_utc(snapshot.last_review),
        )

    @staticmethod
    def _from_fsrs_card(card: FsrsCard) -> FsrsSnapshot:
        """fsrs.Card -> 快照（时间为 naive UTC，便于直接落库）。"""
        due = to_naive_utc(card.due)
        last_review = to_naive_utc(card.last_review)
        return FsrsSnapshot(
            card_id=card.card_id,
            state=int(card.state),
            step=card.step,
            stability=card.stability,
            difficulty=card.difficulty,
            due=due,
            last_review=last_review,
        )

    # --- 对外接口 ---------------------------------------------------------

    def new_snapshot(self, card_id: int | None = None) -> FsrsSnapshot:
        """全新卡片的初始快照。

        注意：py-fsrs 的 ``Card()`` 会把 ``due`` 默认成「当前时刻」，
        而平台约定新卡的 ``fsrs_due`` 为空（到期查询据此把新卡排在队列最前，
        也便于区分「从未学过」与「已到期」），因此这里显式归一化为 None。
        """
        card = FsrsCard(card_id=card_id)
        snapshot = self._from_fsrs_card(card)
        snapshot = replace(snapshot, due=None)
        logger.debug("创建初始快照 card_id=%s state=%s", card_id, snapshot.state)
        return snapshot

    def _advance(
        self,
        scheduler: Scheduler,
        snapshot: FsrsSnapshot,
        rating: int,
        reviewed_at: datetime,
    ) -> tuple[FsrsSnapshot, float]:
        fsrs_card = self._to_fsrs_card(snapshot)
        new_card, _log = scheduler.review_card(
            fsrs_card, _to_fsrs_rating(rating), review_datetime=reviewed_at
        )
        new_snapshot = self._from_fsrs_card(new_card)
        if new_snapshot.card_id is None:
            new_snapshot = replace(new_snapshot, card_id=snapshot.card_id)
        return new_snapshot, days_between(reviewed_at, new_snapshot.due)

    def schedule(
        self,
        snapshot: FsrsSnapshot,
        rating: int,
        reviewed_at: datetime | None = None,
    ) -> ScheduleResult:
        """按评分推进调度状态。

        ``snapshot`` 不会被修改；返回全新的 ``ScheduleResult``。
        """
        now = to_aware_utc(reviewed_at) or utcnow()
        elapsed = days_between(snapshot.last_review, now)

        new_snapshot, interval_days = self._advance(
            self.scheduler, snapshot, rating, now
        )

        retrievability: float | None = None
        if new_snapshot.stability is not None:
            try:
                retrievability = self.scheduler.get_card_retrievability(
                    self._to_fsrs_card(new_snapshot), now
                )
            except Exception:  # pragma: no cover - 可回忆度仅为附加信息
                logger.debug("计算可回忆度失败 card_id=%s", snapshot.card_id, exc_info=True)

        result = ScheduleResult(
            snapshot=new_snapshot,
            rating=int(rating),
            rating_label=rating_label(rating),
            reviewed_at=now,
            elapsed_days=elapsed,
            interval_days=interval_days,
            retrievability=retrievability,
        )
        logger.info(
            "调度完成 card_id=%s rating=%s state=%s stability=%s difficulty=%s due=%s interval=%.3f天",
            snapshot.card_id,
            result.rating_label,
            new_snapshot.state,
            None if new_snapshot.stability is None else round(new_snapshot.stability, 4),
            None if new_snapshot.difficulty is None else round(new_snapshot.difficulty, 4),
            new_snapshot.due,
            interval_days,
        )
        return result

    def preview(
        self, snapshot: FsrsSnapshot, now: datetime | None = None
    ) -> dict[int, ScheduleResult]:
        """预览四种评分的结果（关闭 fuzzing，数值稳定）。"""
        moment = to_aware_utc(now) or utcnow()
        out: dict[int, ScheduleResult] = {}
        for rating in VALID_RATINGS:
            preview_snapshot, interval_days = self._advance(
                self._preview_scheduler, snapshot, rating, moment
            )
            out[rating] = ScheduleResult(
                snapshot=preview_snapshot,
                rating=rating,
                rating_label=rating_label(rating),
                reviewed_at=moment,
                elapsed_days=days_between(snapshot.last_review, moment),
                interval_days=interval_days,
            )
        return out

    def retrievability(
        self, snapshot: FsrsSnapshot, now: datetime | None = None
    ) -> float | None:
        """当前可回忆概率；新卡（无 stability）返回 None。"""
        if snapshot.stability is None:
            return None
        moment = to_aware_utc(now) or utcnow()
        try:
            return self.scheduler.get_card_retrievability(
                self._to_fsrs_card(snapshot), moment
            )
        except Exception:
            logger.warning("可回忆度计算失败 card_id=%s", snapshot.card_id, exc_info=True)
            return None


# --- ORM 适配层（只依赖属性，不依赖会话） -----------------------------------


def snapshot_from_card(card: Any) -> FsrsSnapshot:
    """从 ORM Card 对象提取调度快照。"""
    return FsrsSnapshot(
        card_id=getattr(card, "id", None),
        state=int(getattr(card, "fsrs_state", None) or NEW_CARD_STATE),
        step=getattr(card, "fsrs_step", None),
        stability=getattr(card, "fsrs_stability", None),
        difficulty=getattr(card, "fsrs_difficulty", None),
        due=to_aware_utc(getattr(card, "fsrs_due", None)),
        last_review=to_aware_utc(getattr(card, "fsrs_last_review", None)),
    )


def apply_result_to_card(card: Any, result: ScheduleResult) -> Any:
    """把调度结果写回 ORM Card 对象的字段（不提交事务）。"""
    snapshot = result.snapshot
    card.fsrs_stability = snapshot.stability
    card.fsrs_difficulty = snapshot.difficulty
    card.fsrs_due = to_naive_utc(snapshot.due)
    card.fsrs_last_review = to_naive_utc(snapshot.last_review)
    card.fsrs_state = int(snapshot.state)
    card.fsrs_step = snapshot.step
    return card


def is_due(snapshot: FsrsSnapshot, now: datetime | None = None) -> bool:
    """卡片是否到期（新卡永远到期）。"""
    if snapshot.last_review is None and snapshot.due is None:
        return True
    if snapshot.due is None:
        return True
    moment = to_aware_utc(now) or utcnow()
    due = to_aware_utc(snapshot.due)
    if due is None:  # pragma: no cover - 已在上方处理
        return True
    return due <= moment


# 默认调度器实例（路由层复用）
default_scheduler = ReviewScheduler()
