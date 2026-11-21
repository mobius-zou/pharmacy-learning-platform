"""FSRS-6 调度逻辑单元测试（不需要数据库）。

覆盖验收要求：四种评分（忘了/困难/一般/简单）各自的调度行为、
due 时间更新、以及 state/stability/difficulty 的正确推进。

测试中统一关闭 fuzzing（enable_fuzzing=False），因此间隔是可预期的确定值。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fsrs import State

from app.services.review_service import (
    NEW_CARD_STATE,
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    RATING_LABELS,
    VALID_RATINGS,
    FsrsSnapshot,
    ReviewScheduler,
    apply_result_to_card,
    format_interval,
    is_due,
    rating_label,
    snapshot_from_card,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def scheduler() -> ReviewScheduler:
    """确定性调度器：关闭 fuzzing。"""
    return ReviewScheduler(desired_retention=0.9, enable_fuzzing=False)


def _seconds(result) -> float:
    """结果对应的间隔秒数。"""
    assert result.due is not None
    return (result.due - result.reviewed_at).total_seconds()


def _days(result) -> float:
    return _seconds(result) / 86400.0


# --- 新卡初始状态 -----------------------------------------------------------


def test_new_snapshot_is_a_fresh_card(scheduler: ReviewScheduler) -> None:
    snapshot = scheduler.new_snapshot()

    assert snapshot.state == NEW_CARD_STATE == int(State.Learning)
    assert snapshot.stability is None
    assert snapshot.difficulty is None
    assert snapshot.due is None
    assert snapshot.last_review is None
    assert snapshot.is_new is True


def test_new_snapshot_keeps_card_id(scheduler: ReviewScheduler) -> None:
    assert scheduler.new_snapshot(card_id=42).card_id == 42


def test_new_snapshot_normalizes_py_fsrs_default_due(
    scheduler: ReviewScheduler,
) -> None:
    """回归测试：py-fsrs 的 Card() 会把 due 默认成当前时刻。

    平台约定新卡 due 为空（NULL），否则「从未学过」与「已到期」无法区分，
    到期队列的排序也会退化。
    """
    from fsrs import Card as FsrsCard

    raw = FsrsCard()
    assert raw.due is not None, "py-fsrs 行为已变化，请重新评估新卡约定"

    snapshot = scheduler.new_snapshot()
    assert snapshot.due is None
    assert snapshot.last_review is None
    assert snapshot.is_new is True


# --- 四种评分（核心验收项）-------------------------------------------------


@pytest.mark.parametrize(
    ("rating", "label"),
    [
        (RATING_AGAIN, "忘了"),
        (RATING_HARD, "困难"),
        (RATING_GOOD, "一般"),
        (RATING_EASY, "简单"),
    ],
)
def test_each_rating_updates_fsrs_fields(
    scheduler: ReviewScheduler, rating: int, label: str
) -> None:
    """四种评分都能推进调度：stability/difficulty/due/last_review 全部更新。"""
    result = scheduler.schedule(scheduler.new_snapshot(), rating, reviewed_at=NOW)

    assert result.rating == rating
    assert result.rating_label == label
    assert result.reviewed_at == NOW

    snapshot = result.snapshot
    assert snapshot.stability is not None and snapshot.stability > 0
    assert snapshot.difficulty is not None and snapshot.difficulty > 0
    assert result.due is not None
    assert result.due > NOW, "下次复习时间必须晚于本次复习"
    # 快照内部按落库约定保存 naive UTC
    assert snapshot.last_review == NOW.replace(tzinfo=None)
    assert result.interval_days > 0


@pytest.mark.parametrize("rating", VALID_RATINGS)
def test_due_is_timezone_aware_utc(scheduler: ReviewScheduler, rating: int) -> None:
    """due 必须是 aware UTC —— py-fsrs 拒绝 naive 时间，落库前才转 naive。"""
    result = scheduler.schedule(scheduler.new_snapshot(), rating, reviewed_at=NOW)

    assert result.due is not None
    assert result.due.tzinfo is not None
    assert result.due.utcoffset() == timedelta(0)
    assert result.snapshot.last_review is not None
    # 快照里的时间按约定是 naive UTC（便于写入 MySQL DATETIME）
    assert result.snapshot.due.tzinfo is None
    assert result.snapshot.last_review.tzinfo is None


def test_again_keeps_new_card_in_learning(scheduler: ReviewScheduler) -> None:
    """「忘了」：停留在 Learning，间隔回到第一个学习步进（1 分钟）。"""
    result = scheduler.schedule(scheduler.new_snapshot(), RATING_AGAIN, reviewed_at=NOW)

    assert result.snapshot.state == int(State.Learning)
    assert _seconds(result) == pytest.approx(60, abs=1)
    assert result.interval_label == "1 分钟"


def test_hard_uses_short_interval(scheduler: ReviewScheduler) -> None:
    """「困难」：仍在 Learning，间隔短于「一般」，长于「忘了」。"""
    again = scheduler.schedule(scheduler.new_snapshot(), RATING_AGAIN, reviewed_at=NOW)
    hard = scheduler.schedule(scheduler.new_snapshot(), RATING_HARD, reviewed_at=NOW)
    good = scheduler.schedule(scheduler.new_snapshot(), RATING_GOOD, reviewed_at=NOW)

    assert hard.snapshot.state == int(State.Learning)
    assert _seconds(again) < _seconds(hard) < _seconds(good)
    assert _seconds(hard) == pytest.approx(330, abs=1)


def test_good_moves_to_second_learning_step(scheduler: ReviewScheduler) -> None:
    """「一般」：进入第二个学习步进（10 分钟），仍未毕业。"""
    result = scheduler.schedule(scheduler.new_snapshot(), RATING_GOOD, reviewed_at=NOW)

    assert result.snapshot.state == int(State.Learning)
    assert result.snapshot.step == 1
    assert _seconds(result) == pytest.approx(600, abs=1)
    assert result.interval_label == "10 分钟"


def test_easy_graduates_new_card_to_review(scheduler: ReviewScheduler) -> None:
    """「简单」：新卡直接毕业进入 Review，间隔跳到 8 天。"""
    result = scheduler.schedule(scheduler.new_snapshot(), RATING_EASY, reviewed_at=NOW)

    assert result.snapshot.state == int(State.Review)
    assert _days(result) == pytest.approx(8, abs=0.01)
    assert result.interval_label == "8 天"


def test_intervals_increase_monotonically_with_rating(
    scheduler: ReviewScheduler,
) -> None:
    """新卡上四档评分的间隔必须严格递增：忘了 < 困难 < 一般 < 简单。"""
    intervals = [
        _seconds(scheduler.schedule(scheduler.new_snapshot(), rating, reviewed_at=NOW))
        for rating in VALID_RATINGS
    ]
    assert intervals == sorted(intervals)
    assert len(set(intervals)) == len(intervals), f"间隔应两两不同，实际 {intervals}"


# --- 长期推进 / 遗忘 --------------------------------------------------------


def test_successive_good_reviews_lengthen_interval(
    scheduler: ReviewScheduler,
) -> None:
    """连续「一般」：毕业进入 Review 后间隔持续变长。"""
    snapshot = scheduler.new_snapshot()
    moment = NOW
    intervals: list[float] = []
    states: list[int] = []

    for _ in range(4):
        result = scheduler.schedule(snapshot, RATING_GOOD, reviewed_at=moment)
        snapshot = result.snapshot
        intervals.append(_days(result))
        states.append(snapshot.state)
        moment = result.due  # type: ignore[assignment]

    # 第一次仍在 Learning（10 分钟），随后毕业进入 Review
    assert states[0] == int(State.Learning)
    assert states[1:] == [int(State.Review)] * 3
    # 复习阶段间隔严格递增：2 天 → 11 天 → 46 天
    assert intervals[1] == pytest.approx(2, abs=0.01)
    assert intervals[2] == pytest.approx(11, abs=0.01)
    assert intervals[3] == pytest.approx(46, abs=0.01)
    assert intervals[1] < intervals[2] < intervals[3]


def test_lapse_drops_stability_and_raises_difficulty(
    scheduler: ReviewScheduler,
) -> None:
    """遗忘（在 Review 态评「忘了」）：进入 Relearning，稳定性下降、难度上升。"""
    easy = scheduler.schedule(scheduler.new_snapshot(), RATING_EASY, reviewed_at=NOW)
    learned_stability = easy.snapshot.stability
    learned_difficulty = easy.snapshot.difficulty

    lapse = scheduler.schedule(easy.snapshot, RATING_AGAIN, reviewed_at=easy.due)  # type: ignore[arg-type]

    assert lapse.snapshot.state == int(State.Relearning)
    assert lapse.snapshot.stability is not None
    assert lapse.snapshot.stability < learned_stability  # type: ignore[operator]
    assert lapse.snapshot.difficulty is not None
    assert lapse.snapshot.difficulty > learned_difficulty  # type: ignore[operator]
    assert _seconds(lapse) == pytest.approx(600, abs=1), "重学习步进 10 分钟"


def test_recovery_after_lapse_returns_to_review(scheduler: ReviewScheduler) -> None:
    """遗忘后再次答对，卡片回到 Review 态（难度已被推高，故间隔缩短为 1 天）。"""
    easy = scheduler.schedule(scheduler.new_snapshot(), RATING_EASY, reviewed_at=NOW)
    lapse = scheduler.schedule(easy.snapshot, RATING_AGAIN, reviewed_at=easy.due)  # type: ignore[arg-type]
    recovered = scheduler.schedule(
        lapse.snapshot, RATING_GOOD, reviewed_at=lapse.due  # type: ignore[arg-type]
    )

    assert recovered.snapshot.state == int(State.Review)
    assert _days(recovered) == pytest.approx(1, abs=0.01)
    # 难度被遗忘推高后，即便答对，间隔也远小于遗忘前的水平
    assert _days(recovered) < 8


# --- 间隔与可回忆度 ---------------------------------------------------------


def test_elapsed_days_measured_from_last_review(scheduler: ReviewScheduler) -> None:
    """elapsed_days = 本次复习距上次复习的天数；首刷为 0。"""
    first = scheduler.schedule(scheduler.new_snapshot(), RATING_EASY, reviewed_at=NOW)
    assert first.elapsed_days == pytest.approx(0.0)

    later = NOW + timedelta(days=8, hours=6)
    second = scheduler.schedule(first.snapshot, RATING_GOOD, reviewed_at=later)
    assert second.elapsed_days == pytest.approx(8.25, abs=1e-6)


def test_retrievability_of_new_card_is_none(scheduler: ReviewScheduler) -> None:
    assert scheduler.retrievability(scheduler.new_snapshot(), NOW) is None


def test_retrievability_near_desired_retention_when_due(
    scheduler: ReviewScheduler,
) -> None:
    """在到期时刻，可回忆度应接近设定的 desired_retention=0.9。"""
    result = scheduler.schedule(scheduler.new_snapshot(), RATING_EASY, reviewed_at=NOW)
    value = scheduler.retrievability(result.snapshot, result.due)

    assert value is not None
    assert value == pytest.approx(0.9, abs=0.02)


def test_schedule_with_naive_datetime_is_treated_as_utc(
    scheduler: ReviewScheduler,
) -> None:
    """传入 naive 时间不应崩溃（按 UTC 解释），因为前端可能不带时区。"""
    naive = datetime(2026, 1, 1, 12, 0, 0)
    result = scheduler.schedule(scheduler.new_snapshot(), RATING_GOOD, reviewed_at=naive)

    assert result.reviewed_at == NOW
    assert result.due is not None and result.due.tzinfo is not None


# --- 不可变性 / 校验 --------------------------------------------------------


def test_schedule_does_not_mutate_input_snapshot(
    scheduler: ReviewScheduler,
) -> None:
    """调度不得就地修改传入的快照（快照是 frozen dataclass）。"""
    snapshot = scheduler.new_snapshot(card_id=7)

    scheduler.schedule(snapshot, RATING_EASY, reviewed_at=NOW)

    assert snapshot.stability is None
    assert snapshot.difficulty is None
    assert snapshot.due is None
    assert snapshot.last_review is None
    assert snapshot.state == NEW_CARD_STATE


def test_snapshot_is_immutable(scheduler: ReviewScheduler) -> None:
    snapshot = scheduler.new_snapshot()
    with pytest.raises(Exception):
        snapshot.stability = 1.0  # type: ignore[misc]


@pytest.mark.parametrize("bad_rating", [0, 5, -1, 99])
def test_invalid_rating_is_rejected(
    scheduler: ReviewScheduler, bad_rating: int
) -> None:
    with pytest.raises(ValueError):
        scheduler.schedule(scheduler.new_snapshot(), bad_rating, reviewed_at=NOW)


def test_rating_labels_are_the_four_ui_buttons() -> None:
    assert RATING_LABELS == {1: "忘了", 2: "困难", 3: "一般", 4: "简单"}
    assert VALID_RATINGS == (1, 2, 3, 4)
    assert rating_label(1) == "忘了"
    assert rating_label(4) == "简单"


# --- 预览 -------------------------------------------------------------------


def test_preview_returns_all_four_options(scheduler: ReviewScheduler) -> None:
    preview = scheduler.preview(scheduler.new_snapshot(), NOW)

    assert set(preview) == set(VALID_RATINGS)
    assert [preview[r].rating_label for r in VALID_RATINGS] == [
        "忘了",
        "困难",
        "一般",
        "简单",
    ]
    intervals = [_seconds(preview[r]) for r in VALID_RATINGS]
    assert intervals == sorted(intervals)


def test_preview_does_not_mutate_snapshot(scheduler: ReviewScheduler) -> None:
    snapshot = scheduler.new_snapshot(card_id=3)
    scheduler.preview(snapshot, NOW)
    assert snapshot.stability is None and snapshot.due is None


def test_preview_is_deterministic(scheduler: ReviewScheduler) -> None:
    """预览关闭 fuzzing，两次调用结果一致，按钮数字不会跳动。"""
    snapshot = scheduler.new_snapshot()
    first = scheduler.preview(snapshot, NOW)
    second = scheduler.preview(snapshot, NOW)

    assert [first[r].due for r in VALID_RATINGS] == [
        second[r].due for r in VALID_RATINGS
    ]


def test_preview_matches_schedule_when_fuzzing_disabled(
    scheduler: ReviewScheduler,
) -> None:
    """关闭 fuzzing 时，预览结果与真实评分结果必须一致。"""
    snapshot = scheduler.new_snapshot()
    for rating in VALID_RATINGS:
        preview = scheduler.preview(snapshot, NOW)[rating]
        actual = scheduler.schedule(snapshot, rating, reviewed_at=NOW)
        assert preview.due == actual.due
        assert preview.interval_days == pytest.approx(actual.interval_days)


# --- ORM 适配层 -------------------------------------------------------------


def test_snapshot_from_card_and_apply_result_roundtrip(
    scheduler: ReviewScheduler,
) -> None:
    """ORM 适配层：从卡片提取快照 → 调度 → 写回字段。"""
    card = SimpleNamespace(
        id=11,
        fsrs_state=int(State.Learning),
        fsrs_step=None,
        fsrs_stability=None,
        fsrs_difficulty=None,
        fsrs_due=None,
        fsrs_last_review=None,
    )

    snapshot = snapshot_from_card(card)
    assert snapshot.card_id == 11
    assert snapshot.is_new is True

    result = scheduler.schedule(snapshot, RATING_EASY, reviewed_at=NOW)
    apply_result_to_card(card, result)

    assert card.fsrs_stability == pytest.approx(8.2956, abs=1e-3)
    assert card.fsrs_difficulty == pytest.approx(1.0, abs=1e-6)
    assert card.fsrs_state == int(State.Review)
    assert card.fsrs_last_review == datetime(2026, 1, 1, 12, 0, 0)  # naive UTC
    assert card.fsrs_due == datetime(2026, 1, 9, 12, 0, 0)  # naive UTC
    assert card.fsrs_due.tzinfo is None


def test_snapshot_from_card_handles_naive_db_datetimes() -> None:
    """MySQL 返回 naive datetime，必须能被正确解释为 UTC。"""
    card = SimpleNamespace(
        id=1,
        fsrs_state=int(State.Review),
        fsrs_step=None,
        fsrs_stability=10.0,
        fsrs_difficulty=5.0,
        fsrs_due=datetime(2026, 2, 1, 12, 0, 0),
        fsrs_last_review=datetime(2026, 1, 1, 12, 0, 0),
    )
    snapshot = snapshot_from_card(card)

    assert snapshot.due is not None and snapshot.due.utcoffset() == timedelta(0)
    assert snapshot.last_review is not None
    assert snapshot.last_review.utcoffset() == timedelta(0)
    assert snapshot.is_new is False


def test_snapshot_from_card_defaults_missing_state() -> None:
    """老数据可能没有 state，应回退为新卡的 Learning。"""
    card = SimpleNamespace(
        id=5,
        fsrs_stability=None,
        fsrs_difficulty=None,
        fsrs_due=None,
        fsrs_last_review=None,
    )
    assert snapshot_from_card(card).state == NEW_CARD_STATE


def test_reconstructed_snapshot_keeps_scheduling_branch(
    scheduler: ReviewScheduler,
) -> None:
    """回归测试：如果丢掉 state/step，Review 态卡片会被误当新卡，间隔完全错误。"""
    easy = scheduler.schedule(scheduler.new_snapshot(), RATING_EASY, reviewed_at=NOW)

    # 基准：内存中直接继续调度
    in_memory = scheduler.schedule(easy.snapshot, RATING_GOOD, reviewed_at=easy.due)  # type: ignore[arg-type]

    # 模拟一次落库 → 读取的往返（MySQL 返回 naive datetime）
    stored = SimpleNamespace(
        id=1,
        fsrs_state=easy.snapshot.state,
        fsrs_step=easy.snapshot.step,
        fsrs_stability=easy.snapshot.stability,
        fsrs_difficulty=easy.snapshot.difficulty,
        fsrs_due=easy.snapshot.due,
        fsrs_last_review=easy.snapshot.last_review,
    )
    restored = snapshot_from_card(stored)
    after_roundtrip = scheduler.schedule(restored, RATING_GOOD, reviewed_at=easy.due)  # type: ignore[arg-type]

    assert restored.state == int(State.Review)
    assert after_roundtrip.due == in_memory.due, "往返后调度结果必须与内存中一致"
    assert _days(after_roundtrip) == pytest.approx(39, abs=0.01)

    # 反证：若 state 丢失、被当作新卡重建，「一般」只会得到 10 分钟而非 39 天
    as_if_new = scheduler.schedule(
        scheduler.new_snapshot(), RATING_GOOD, reviewed_at=easy.due  # type: ignore[arg-type]
    )
    assert _days(as_if_new) == pytest.approx(10 / 1440, abs=1e-4)
    assert after_roundtrip.due != as_if_new.due


# --- 到期判定与格式化 -------------------------------------------------------


def test_is_due_for_new_card() -> None:
    assert is_due(FsrsSnapshot(), NOW) is True


def test_is_due_respects_due_time() -> None:
    future = FsrsSnapshot(
        state=int(State.Review),
        stability=5.0,
        difficulty=5.0,
        due=NOW + timedelta(days=1),
        last_review=NOW,
    )
    past = FsrsSnapshot(
        state=int(State.Review),
        stability=5.0,
        difficulty=5.0,
        due=NOW - timedelta(days=1),
        last_review=NOW - timedelta(days=2),
    )

    assert is_due(future, NOW) is False
    assert is_due(past, NOW) is True


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (0.0007, "1 分钟"),
        (600 / 86400, "10 分钟"),
        (0.5, "12 小时"),
        (8, "8 天"),
        (45, "1.5 个月"),
        (400, "1.1 年"),
    ],
)
def test_format_interval(days: float, expected: str) -> None:
    assert format_interval(days) == expected


def test_desired_retention_is_configurable() -> None:
    """Scheduler(desired_retention=...) 可调：更高的保留率 => 更短的间隔。"""
    low = ReviewScheduler(desired_retention=0.7, enable_fuzzing=False)
    high = ReviewScheduler(desired_retention=0.97, enable_fuzzing=False)

    low_result = low.schedule(low.new_snapshot(), RATING_EASY, reviewed_at=NOW)
    high_result = high.schedule(high.new_snapshot(), RATING_EASY, reviewed_at=NOW)

    assert low.desired_retention == 0.7
    assert high.desired_retention == 0.97
    assert _days(high_result) < _days(low_result)
