"""复习路由 /api/v1/review —— 到期查询、评分、预览、历史。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...database import get_db
from ...deps import api_guard, get_current_user
from ...errors import NotFoundError, ValidationError
from ...logging_config import get_logger
from ...models import Card, User
from ...schemas import (
    CardOut,
    DueCardsResponse,
    GradeRequest,
    GradeResponse,
    PreviewOption,
    ReviewLogOut,
    ReviewPreviewResponse,
)
from ...services import card_service
from ...services import wrong_question_service as wq_service
from ...services.review_service import (
    VALID_RATINGS,
    default_scheduler,
    snapshot_from_card,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/review", tags=["复习"])


def _get_owned_card(db: Session, user: User, card_id: int) -> Card:
    card = card_service.get_card(db, user.id, card_id)
    if card is None:
        raise NotFoundError(f"卡片不存在：{card_id}")
    return card


@router.get("/due", response_model=DueCardsResponse, summary="查询到期卡片（薄弱点优先）")
@api_guard
def list_due_cards(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DueCardsResponse:
    """返回当前到期的卡片。

    排序（阶段二）：**薄弱知识点关联的卡片最前** → 新卡 → 其余按到期时间升序。
    只调整顺序，不改变 FSRS 的到期判定。
    """
    total_due = card_service.count_due_cards(db, current_user.id)

    # 薄弱点卡片优先。这里**只算一次**聚合：原先同时调 weak_card_ids() 与
    # list_weak_points()，两者都会重跑相同的错题聚合与关联查询（重复工作）。
    weak_points = wq_service.list_weak_points(
        db, current_user.id, weak_only=True, with_related_cards=True
    )
    weak_ids = sorted({cid for point in weak_points for cid in point.related_card_ids})

    items = card_service.list_due_cards(
        db,
        current_user.id,
        limit=limit,
        offset=offset,
        priority_card_ids=weak_ids,
    )
    item_ids = {card.id for card in items}
    return DueCardsResponse(
        total_due=total_due,
        offset=offset,
        limit=limit,
        weak_point_count=len(weak_points),
        weak_card_ids=[cid for cid in weak_ids if cid in item_ids],
        items=[CardOut.model_validate(card) for card in items],
    )


@router.get(
    "/{card_id}/preview",
    response_model=ReviewPreviewResponse,
    summary="预览四档评分对应的间隔",
)
@api_guard
def preview_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewPreviewResponse:
    """给出「忘了/困难/一般/简单」四档各自的下次复习间隔。"""
    card = _get_owned_card(db, current_user, card_id)
    snapshot = snapshot_from_card(card)
    previews = default_scheduler.preview(snapshot)
    return ReviewPreviewResponse(
        card_id=card.id,
        is_new=snapshot.is_new,
        retrievability=default_scheduler.retrievability(snapshot),
        options=[
            PreviewOption(
                rating=rating,
                label=previews[rating].rating_label,
                interval_days=previews[rating].interval_days,
                interval_label=previews[rating].interval_label,
                due=previews[rating].due,
            )
            for rating in VALID_RATINGS
        ],
    )


@router.post(
    "/{card_id}/grade",
    response_model=GradeResponse,
    summary="评分并推进 FSRS 调度",
)
@api_guard
def grade_card(
    card_id: int,
    payload: GradeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GradeResponse:
    """核心闭环：评分 → 更新 stability/difficulty/due/last_review + 写复习日志。"""
    card = _get_owned_card(db, current_user, card_id)
    try:
        card, result = card_service.grade_card(
            db,
            card,
            payload.rating,
            reviewed_at=payload.reviewed_at,
            elapsed_days=payload.elapsed_days,
        )
    except ValueError as exc:
        # 补录时间不合法等业务校验 → 422（而不是被 api_guard 兜成 500）
        raise ValidationError(str(exc)) from exc
    return GradeResponse(
        card=CardOut.model_validate(card),
        rating=result.rating,
        rating_label=result.rating_label,
        elapsed_days=result.elapsed_days,
        interval_days=result.interval_days,
        interval_label=result.interval_label,
        due=result.due,
        retrievability=result.retrievability,
    )


@router.get(
    "/{card_id}/logs",
    response_model=list[ReviewLogOut],
    summary="卡片复习历史",
)
@api_guard
def list_logs(
    card_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list:
    """返回该卡的复习日志（按时间倒序）。"""
    _get_owned_card(db, current_user, card_id)  # 归属校验
    return card_service.list_review_logs(db, current_user.id, card_id, offset, limit)
