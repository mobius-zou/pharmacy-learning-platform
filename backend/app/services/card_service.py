"""卡片业务服务：增删改查、到期查询、粘贴文本导入。

全部通过 SQLAlchemy ORM 完成，不手写原生 SQL。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..limits import MAX_CONTENT_LENGTH, clamp_text, like_contains
from ..errors import ConflictError
from ..config import settings
from ..logging_config import get_logger
from ..models import Card, ReviewLog
from ..tags import KEY_CHAPTERS, KEY_POINT_TYPES, KEY_SUBJECTS, normalize_tags
from ..timeutils import to_aware_utc, to_naive_utc, utcnow
from .review_service import FsrsSnapshot, ReviewScheduler, apply_result_to_card

logger = get_logger(__name__)

# 粘贴文本时支持的分隔符：竖线 / 制表符 / 双冒号 / 全角竖线
_SEPARATORS = re.compile(r"\s*(?:\|\||\||\t|：：|::|｜)\s*")


@dataclass
class BulkParseResult:
    """粘贴文本解析结果。"""

    items: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --- 创建 -------------------------------------------------------------------


def create_card(
    db: Session,
    user_id: int,
    front: str,
    back: str,
    tags: Any = None,
    scheduler: ReviewScheduler | None = None,
) -> Card:
    """创建一张新卡，并写入 FSRS 初始调度状态。"""
    normalized = normalize_tags(tags)
    # 服务层再 clamp 一次：除 API（Pydantic）之外，文献转卡片、考点转卡片、
    # 批量导入等路径都直接调用本函数，必须保证不超列上限。
    front_text, _ = clamp_text(front.strip(), MAX_CONTENT_LENGTH)
    back_text, _ = clamp_text(back.strip(), MAX_CONTENT_LENGTH)
    card = Card(
        user_id=user_id,
        front=front_text,
        back=back_text,
        tags=normalized,
        created_at=utcnow(),
    )
    engine = scheduler
    snapshot = (engine or _default()).new_snapshot()
    # 新卡：due 立即到期，stability/difficulty 为空
    card.fsrs_state = snapshot.state
    card.fsrs_step = snapshot.step
    card.fsrs_stability = snapshot.stability
    card.fsrs_difficulty = snapshot.difficulty
    card.fsrs_due = to_naive_utc(snapshot.due)
    card.fsrs_last_review = to_naive_utc(snapshot.last_review)

    db.add(card)
    db.flush()  # 取得自增 id
    logger.info("创建卡片 id=%s user_id=%s", card.id, user_id)
    return card


def create_cards_bulk(
    db: Session,
    user_id: int,
    text: str,
    tags: Any = None,
    scheduler: ReviewScheduler | None = None,
) -> tuple[list[Card], list[str]]:
    """从粘贴文本批量创建卡片。返回 (已创建卡片, 错误信息)。"""
    parsed = parse_bulk_text(text)
    created: list[Card] = []
    for front, back in parsed.items:
        created.append(
            create_card(db, user_id, front, back, tags, scheduler=scheduler)
        )
    if created:
        logger.info("批量创建 %d 张卡片 user_id=%s", len(created), user_id)
    return created, parsed.errors


def parse_bulk_text(text: str) -> BulkParseResult:
    """解析粘贴文本。

    每行一张卡片，正面与背面用 ``|``、Tab、``::`` 或 ``｜`` 分隔。
    以 ``#`` 开头的行视为注释，空行忽略。
    """
    result = BulkParseResult()
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _SEPARATORS.split(line, maxsplit=1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            result.errors.append(f"第 {lineno} 行无法解析（缺少分隔符或内容为空）：{line[:50]}")
            continue
        # 单条过长要截断，避免撞上 TEXT 列的字节上限（否则整批 500）
        front, front_cut = clamp_text(parts[0].strip(), MAX_CONTENT_LENGTH)
        back, back_cut = clamp_text(parts[1].strip(), MAX_CONTENT_LENGTH)
        if front_cut or back_cut:
            result.errors.append(
                f"第 {lineno} 行内容超过 {MAX_CONTENT_LENGTH} 字符，已截断"
            )
        result.items.append((front, back))
    return result


# --- 查询 -------------------------------------------------------------------


def _tag_filter(stmt: Select, key: str, values: list[str] | None) -> Select:
    """按标签数组包含关系过滤（MySQL JSON_CONTAINS，走 ORM 表达式）。

    语义：**同一维度内多值是「或」（并集），不同维度之间是「与」（交集）**。
    例如同时选「药理学」和「药物化学」，应返回两类卡片，而不是要求一张卡片
    同时属于两个专业。
    """
    if not values:
        return stmt
    conditions = [
        func.json_contains(Card.tags[key], json.dumps(value, ensure_ascii=False))
        for value in values
    ]
    return stmt.where(or_(*conditions))


def build_cards_query(
    user_id: int,
    keyword: str | None = None,
    subjects: list[str] | None = None,
    chapters: list[str] | None = None,
    point_types: list[str] | None = None,
) -> Select:
    """构造卡片列表查询。"""
    stmt = select(Card).where(Card.user_id == user_id)
    stmt = _tag_filter(stmt, KEY_SUBJECTS, subjects)
    stmt = _tag_filter(stmt, KEY_CHAPTERS, chapters)
    stmt = _tag_filter(stmt, KEY_POINT_TYPES, point_types)
    if keyword:
        pattern, escape = like_contains(keyword.strip())
        stmt = stmt.where(
            or_(
                Card.front.ilike(pattern, escape=escape),
                Card.back.ilike(pattern, escape=escape),
            )
        )
    return stmt


def list_cards(
    db: Session,
    user_id: int,
    offset: int = 0,
    limit: int = 20,
    keyword: str | None = None,
    subjects: list[str] | None = None,
    chapters: list[str] | None = None,
    point_types: list[str] | None = None,
) -> tuple[list[Card], int]:
    """分页查询卡片，返回 (卡片列表, 总数)。"""
    stmt = build_cards_query(user_id, keyword, subjects, chapters, point_types)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int(db.execute(count_stmt).scalar_one())

    page_stmt = stmt.order_by(Card.created_at.desc(), Card.id.desc()).offset(offset).limit(limit)
    items = list(db.execute(page_stmt).scalars().all())
    return items, total


def get_card(db: Session, user_id: int, card_id: int) -> Card | None:
    """按 id 取卡片（限定归属用户）。"""
    stmt = select(Card).where(Card.id == card_id, Card.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()


def count_cards(db: Session, user_id: int) -> int:
    stmt = select(func.count()).select_from(Card).where(Card.user_id == user_id)
    return int(db.execute(stmt).scalar_one())


def due_query(
    user_id: int,
    now: datetime | None = None,
    priority_card_ids: list[int] | tuple[int, ...] | None = None,
) -> Select:
    """到期卡片查询。

    排序优先级（阶段二新增第 1 条）：
    1. **薄弱知识点关联的卡片排在最前**（``priority_card_ids``）；
    2. 新卡（due 为空）；
    3. 其余按 due 升序。

    这里只调整**队列顺序**，不改变 FSRS 的到期判定 —— 未到期的薄弱卡片
    不会被强行提前，以免破坏间隔重复的有效性。
    """
    moment = to_naive_utc(now or utcnow())
    stmt = select(Card).where(
        Card.user_id == user_id,
        or_(Card.fsrs_due.is_(None), Card.fsrs_due <= moment),
    )

    ordering = []
    priority = [int(cid) for cid in (priority_card_ids or [])]
    if priority:
        # case(...) 比 `id IN (...) DESC` 更可移植，MySQL/PG 均可
        ordering.append(case((Card.id.in_(priority), 0), else_=1).asc())
    ordering.extend([Card.fsrs_due.is_(None).desc(), Card.fsrs_due.asc(), Card.id.asc()])
    return stmt.order_by(*ordering)


def list_due_cards(
    db: Session,
    user_id: int,
    limit: int = 20,
    offset: int = 0,
    now: datetime | None = None,
    priority_card_ids: list[int] | tuple[int, ...] | None = None,
) -> list[Card]:
    """查询到期卡片（分页）。"""
    stmt = (
        due_query(user_id, now, priority_card_ids)
        .offset(offset)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def count_due_cards(db: Session, user_id: int, now: datetime | None = None) -> int:
    """统计到期卡片数量。"""
    moment = to_naive_utc(now or utcnow())
    stmt = (
        select(func.count())
        .select_from(Card)
        .where(
            Card.user_id == user_id,
            or_(Card.fsrs_due.is_(None), Card.fsrs_due <= moment),
        )
    )
    return int(db.execute(stmt).scalar_one())


# --- 更新 / 删除 ------------------------------------------------------------


def update_card(
    db: Session,
    card: Card,
    front: str | None = None,
    back: str | None = None,
    tags: Any = None,
) -> Card:
    """更新卡片内容（不触碰调度字段）。"""
    if front is not None:
        card.front = front.strip()
    if back is not None:
        card.back = back.strip()
    if tags is not None:
        card.tags = normalize_tags(tags)
    db.flush()
    logger.info("更新卡片 id=%s", card.id)
    return card


def delete_card(db: Session, card: Card) -> None:
    """删除卡片（关联复习日志级联删除）。"""
    card_id = card.id
    db.delete(card)
    db.flush()
    logger.info("删除卡片 id=%s", card_id)


def reset_card_progress(
    db: Session, card: Card, scheduler: ReviewScheduler | None = None
) -> Card:
    """把卡片重置为新卡（保留内容，清空调度进度与复习日志）。"""
    snapshot: FsrsSnapshot = (scheduler or _default()).new_snapshot(card.id)
    card.fsrs_state = snapshot.state
    card.fsrs_step = snapshot.step
    card.fsrs_stability = None
    card.fsrs_difficulty = None
    card.fsrs_due = None
    card.fsrs_last_review = None
    db.execute(delete(ReviewLog).where(ReviewLog.card_id == card.id))
    db.flush()
    logger.info("重置卡片进度 id=%s", card.id)
    return card


def grade_card(
    db: Session,
    card: Card,
    rating: int,
    reviewed_at: datetime | None = None,
    elapsed_days: float | None = None,
    scheduler: ReviewScheduler | None = None,
) -> tuple[Card, Any]:
    """对卡片评分：推进 FSRS 调度并写入复习日志。

    返回 (更新后的卡片, ScheduleResult)。
    """
    from .review_service import snapshot_from_card

    engine = scheduler or _default()
    snapshot = snapshot_from_card(card)

    # 校验补录时间：既不能早于上次复习（否则历史倒退、间隔为负），
    # 也不能推到过远的未来（否则可以伪造调度、让卡片立刻「到期很久」）。
    if reviewed_at is not None:
        moment = to_aware_utc(reviewed_at)
        last = to_aware_utc(card.fsrs_last_review)
        if last is not None and moment is not None and moment < last:
            raise ValueError(
                "reviewed_at 不能早于该卡片上次复习时间"
                f"（{last.isoformat()}）"
            )
        if moment is not None:
            limit = utcnow() + timedelta(days=settings.review_max_future_days)
            if moment > limit:
                raise ValueError(
                    f"reviewed_at 超出允许范围（最多可提前 {settings.review_max_future_days} 天）"
                )

    result = engine.schedule(snapshot, rating, reviewed_at=reviewed_at)

    apply_result_to_card(card, result)

    reviewed_at = to_naive_utc(result.reviewed_at)
    log = ReviewLog(
        card_id=card.id,
        rating=result.rating,
        reviewed_at=reviewed_at,
        # elapsed_days 一律由服务端按 FSRS 计算，忽略客户端传入值，
        # 避免伪造间隔（负数还会让 FSRS 异常）。
        elapsed_days=float(result.elapsed_days),
    )
    try:
        with db.begin_nested():
            db.add(log)
    except IntegrityError as exc:
        # (card_id, reviewed_at) 唯一约束：双击/重试的幂等护栏
        logger.warning(
            "重复评分被拦截 card_id=%s reviewed_at=%s", card.id, reviewed_at
        )
        raise ConflictError(
            "该卡片在此时间点已有评分记录，请勿重复提交",
            detail={"card_id": card.id, "reviewed_at": str(reviewed_at)},
        ) from exc
    logger.info(
        "评分落库 card_id=%s rating=%s due=%s", card.id, result.rating_label, card.fsrs_due
    )
    return card, result


def list_review_logs(
    db: Session, user_id: int, card_id: int, offset: int = 0, limit: int = 50
) -> list[ReviewLog]:
    """查询某张卡片的复习历史（校验归属用户）。"""
    stmt = (
        select(ReviewLog)
        .join(Card, ReviewLog.card_id == Card.id)
        .where(ReviewLog.card_id == card_id, Card.user_id == user_id)
        .order_by(ReviewLog.reviewed_at.desc(), ReviewLog.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def _default() -> ReviewScheduler:
    from .review_service import default_scheduler

    return default_scheduler
