"""卡片相关 Schema。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from ..tags import normalize_tags
from ..timeutils import to_aware_utc, utcnow
from .types import (
    MAX_BULK_TEXT_LENGTH,
    MAX_CONTENT_LENGTH,
    MAX_SHORT_TEXT_LENGTH,
    UTCDateTime,
)


def _validate_tags(value: Any) -> dict[str, list[str]]:
    return normalize_tags(value)


class CardCreateRequest(BaseModel):
    front: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH, description="正面（问题）")
    back: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH, description="背面（答案）")
    tags: dict[str, list[str]] | None = Field(default=None, description="三维度标签")

    @field_validator("front", "back")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("内容不能为空")
        return value.strip()

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: Any) -> dict[str, list[str]] | None:
        return None if value is None else _validate_tags(value)


class CardUpdateRequest(BaseModel):
    front: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_LENGTH)
    back: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_LENGTH)
    tags: dict[str, list[str]] | None = None

    @field_validator("front", "back")
    @classmethod
    def _not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("内容不能为空")
        return value.strip()

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: Any) -> dict[str, list[str]] | None:
        return None if value is None else _validate_tags(value)


class CardBulkCreateRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=MAX_BULK_TEXT_LENGTH,
        description="每行一张卡片，正反面用 | :: 或 Tab 分隔",
    )
    tags: dict[str, list[str]] | None = Field(default=None, description="应用于本批全部卡片")

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: Any) -> dict[str, list[str]] | None:
        return None if value is None else _validate_tags(value)


class CardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    front: str
    back: str
    tags: dict[str, Any]
    created_at: UTCDateTime

    fsrs_stability: float | None = None
    fsrs_difficulty: float | None = None
    fsrs_due: UTCDateTime | None = None
    fsrs_last_review: UTCDateTime | None = None
    fsrs_state: int = 1
    fsrs_step: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_new(self) -> bool:
        """是否为从未复习过的新卡。"""
        return self.fsrs_last_review is None and self.fsrs_stability is None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_due(self) -> bool:
        """当前是否到期。"""
        if self.fsrs_due is None:
            return True
        due = to_aware_utc(self.fsrs_due)
        if due is None:  # pragma: no cover - 上面已判过
            return True
        return due <= utcnow()


class CardListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[CardOut]


class CardBulkCreateResponse(BaseModel):
    created: int
    errors: list[str] = Field(default_factory=list)
    items: list[CardOut] = Field(default_factory=list)
