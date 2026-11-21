"""复习评分相关 Schema。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from ..services.review_service import RATING_AGAIN, RATING_EASY, rating_label
from .card import CardOut
from .types import UTCDateTime


class GradeRequest(BaseModel):
    rating: int = Field(
        ge=RATING_AGAIN,
        le=RATING_EASY,
        description="评分：1=忘了 2=困难 3=一般 4=简单",
    )
    # 允许补录历史复习；不传则用服务器当前时间
    reviewed_at: datetime | None = Field(default=None, description="复习时间（ISO 8601）")
    elapsed_days: float | None = Field(
        default=None, ge=0, description="覆盖自动计算的间隔天数（一般不需要）"
    )


class GradeResponse(BaseModel):
    card: CardOut
    rating: int
    rating_label: str
    elapsed_days: float
    interval_days: float
    interval_label: str
    due: UTCDateTime | None
    retrievability: float | None = None


class PreviewOption(BaseModel):
    rating: int
    label: str
    interval_days: float
    interval_label: str
    due: UTCDateTime | None


class ReviewPreviewResponse(BaseModel):
    card_id: int
    is_new: bool
    retrievability: float | None = None
    options: list[PreviewOption]


class ReviewLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    card_id: int
    rating: int
    reviewed_at: UTCDateTime
    elapsed_days: float

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rating_label(self) -> str:
        return rating_label(self.rating)


class DueCardsResponse(BaseModel):
    """到期卡片列表。

    ``weak_card_ids`` 标记队列中属于薄弱知识点的卡片（已排在最前），
    便于前端加醒目提示。
    """

    total_due: int
    offset: int
    limit: int
    weak_point_count: int = 0
    weak_card_ids: list[int] = Field(default_factory=list)
    items: list[CardOut]
