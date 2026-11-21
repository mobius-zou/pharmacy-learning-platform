"""卡片路由 /api/v1/cards。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ...database import get_db
from ...deps import api_guard, get_current_user
from ...errors import NotFoundError, ValidationError
from ...logging_config import get_logger
from ...models import Card, User
from ...schemas import (
    CardBulkCreateRequest,
    CardBulkCreateResponse,
    CardCreateRequest,
    CardListResponse,
    CardOut,
    CardUpdateRequest,
)
from ...tags import TagValidationError
from ...services import card_service

logger = get_logger(__name__)
router = APIRouter(prefix="/cards", tags=["卡片"])


def _split_multi(values: list[str] | None) -> list[str] | None:
    """支持 ?subjects=药理学,药剂学 与重复参数两种写法。"""
    if not values:
        return None
    out: list[str] = []
    for raw in values:
        for part in str(raw).split(","):
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return out or None


def _get_owned_card(db: Session, user: User, card_id: int) -> Card:
    card = card_service.get_card(db, user.id, card_id)
    if card is None:
        raise NotFoundError(f"卡片不存在：{card_id}")
    return card


@router.post(
    "",
    response_model=CardOut,
    status_code=status.HTTP_201_CREATED,
    summary="手动录入一张卡片",
)
@api_guard
def create_card(
    payload: CardCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Card:
    """创建单张卡片，并初始化 FSRS 调度状态。"""
    try:
        card = card_service.create_card(
            db, current_user.id, payload.front, payload.back, payload.tags
        )
    except TagValidationError as exc:
        raise ValidationError(str(exc)) from exc
    return card


@router.post(
    "/bulk",
    response_model=CardBulkCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="粘贴文本批量录入",
)
@api_guard
def create_cards_bulk(
    payload: CardBulkCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardBulkCreateResponse:
    """每行一张卡片，正反面用 |、::、｜ 或 Tab 分隔。"""
    try:
        cards, errors = card_service.create_cards_bulk(
            db, current_user.id, payload.text, payload.tags
        )
    except TagValidationError as exc:
        raise ValidationError(str(exc)) from exc
    return CardBulkCreateResponse(
        created=len(cards),
        errors=errors,
        items=[CardOut.model_validate(card) for card in cards],
    )


@router.get("", response_model=CardListResponse, summary="分页查询卡片")
@api_guard
def list_cards(
    offset: int = Query(0, ge=0, description="偏移量"),
    limit: int = Query(20, ge=1, le=200, description="每页数量"),
    keyword: str | None = Query(None, description="正反面关键词模糊搜索"),
    subjects: list[str] | None = Query(None, description="专业课维度（可重复/逗号分隔）"),
    chapters: list[str] | None = Query(None, description="章节维度"),
    point_types: list[str] | None = Query(None, description="考点类型维度"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CardListResponse:
    """按标签与关键词分页查询当前用户的卡片。"""
    items, total = card_service.list_cards(
        db,
        current_user.id,
        offset=offset,
        limit=limit,
        keyword=keyword,
        subjects=_split_multi(subjects),
        chapters=_split_multi(chapters),
        point_types=_split_multi(point_types),
    )
    return CardListResponse(
        total=total,
        offset=offset,
        limit=limit,
        items=[CardOut.model_validate(card) for card in items],
    )


@router.get("/{card_id}", response_model=CardOut, summary="卡片详情")
@api_guard
def get_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Card:
    """按 id 获取卡片。"""
    return _get_owned_card(db, current_user, card_id)


@router.patch("/{card_id}", response_model=CardOut, summary="修改卡片内容")
@api_guard
def update_card(
    card_id: int,
    payload: CardUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Card:
    """修改正面/背面/标签，不影响 FSRS 进度。"""
    card = _get_owned_card(db, current_user, card_id)
    try:
        card_service.update_card(db, card, payload.front, payload.back, payload.tags)
    except TagValidationError as exc:
        raise ValidationError(str(exc)) from exc
    return card


@router.delete(
    "/{card_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除卡片",
)
@api_guard
def delete_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """删除卡片及其复习日志。"""
    card = _get_owned_card(db, current_user, card_id)
    card_service.delete_card(db, card)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{card_id}/reset", response_model=CardOut, summary="重置卡片学习进度")
@api_guard
def reset_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Card:
    """清空该卡的调度进度与复习日志，重新变为新卡。"""
    card = _get_owned_card(db, current_user, card_id)
    card_service.reset_card_progress(db, card)
    return card
