"""考点匹配 Schema（阶段五）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import PRIORITIES
from .types import (
    MAX_BULK_TEXT_LENGTH,
    MAX_CONTENT_LENGTH,
    MAX_SHORT_TEXT_LENGTH,
    UTCDateTime,
)


def _validate_priority(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text not in PRIORITIES:
        raise ValueError(f"优先级必须是 {'、'.join(PRIORITIES)} 之一")
    return text


# --- 考点匹配结果 -----------------------------------------------------------


class ExamFindingOut(BaseModel):
    """一条考点的匹配与优先级结论。"""

    exam_point_id: int = 0
    point_text: str = Field(max_length=MAX_SHORT_TEXT_LENGTH)
    matched_knowledge_point: str | None = None
    matched: bool
    match_score: float = 0.0
    edit_similarity: float = 0.0
    semantic_similarity: float = 0.0
    match_backend: str = ""
    confidence: str = ""
    alternatives: list[str] = Field(default_factory=list)
    priority: str
    mastery: str = ""
    reason: str = ""
    card_ids: list[int] = Field(default_factory=list)


class PrioritySummaryOut(BaseModel):
    total: int = 0
    matched: int = 0
    unmatched: int = 0
    by_priority: dict[str, int] = Field(default_factory=dict)
    generated_at: str = ""


# --- 导入 / 试匹配 ----------------------------------------------------------


class ExamPointMatchRequest(BaseModel):
    """试匹配（不落库）。"""

    text: str = Field(
        min_length=1,
        max_length=MAX_BULK_TEXT_LENGTH,
        description="考点文本，可含编号/项目符号/多行",
    )
    max_points: int | None = Field(default=None, ge=1, le=200, description="上限与服务端一致（200）")

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("考点文本不能为空")
        return value


class ExamPointImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_BULK_TEXT_LENGTH)
    max_points: int | None = Field(default=None, ge=1, le=200, description="上限与服务端一致（200）")
    auto_insert_high_risk: bool = Field(
        default=True, description="导入后是否自动把高危考点插入复习队列"
    )

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("考点文本不能为空")
        return value


class ExamImportResponse(BaseModel):
    imported: int
    points_found: int
    embedding_backend: str = ""
    degraded: bool = Field(default=False, description="是否使用了降级的离线嵌入")
    summary: PrioritySummaryOut
    findings: list[ExamFindingOut] = Field(default_factory=list)
    inserted_to_queue: int = 0
    queue_skipped: int = 0
    duplicates_skipped: int = Field(
        default=0,
        description="因与已有考点重复而未被新增（已合并）的条数",
    )
    note: str = ""


class ExamPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    point_text: str = Field(max_length=MAX_SHORT_TEXT_LENGTH)
    matched_knowledge_point: str | None = None
    priority: str
    created_at: UTCDateTime


class ExamPointListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[ExamPointOut]


# --- 复习清单 ---------------------------------------------------------------


class ReviewListResponse(BaseModel):
    total: int
    summary: PrioritySummaryOut
    groups: dict[str, list[ExamFindingOut]] = Field(default_factory=dict)
    items: list[ExamFindingOut] = Field(default_factory=list)
    note: str = ""


class QueueInsertRequest(BaseModel):
    exam_point_ids: list[int] | None = Field(
        default=None, description="为空表示按优先级批量插入"
    )
    priorities: list[str] = Field(
        default_factory=lambda: ["高危"], description="要插入的优先级，默认仅高危"
    )
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("priorities")
    @classmethod
    def _check_priorities(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            validated = _validate_priority(value)
            if validated and validated not in cleaned:
                cleaned.append(validated)
        if not cleaned:
            raise ValueError(f"优先级不能为空，可选 {'、'.join(PRIORITIES)}")
        return cleaned


class QueueInsertResponse(BaseModel):
    inserted: int
    skipped: int
    card_ids: list[int] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""


# --- OCR / 后端状态 ---------------------------------------------------------


class OCRConcurrencyOut(BaseModel):
    """OCR 并发闸门状态（前端据此提示「逐个上传」）。"""

    max_concurrency: int = 1
    waiting: int = 0


class OCRStatusOut(BaseModel):
    backend: str
    available: bool
    languages: list[str] = Field(default_factory=list)
    default_languages: str = ""
    max_image_bytes: int = 0
    alternatives: list[str] = Field(default_factory=list)
    reason: str | None = None
    install_hint: str | None = None
    concurrency: OCRConcurrencyOut = Field(default_factory=OCRConcurrencyOut)


class EmbeddingStatusOut(BaseModel):
    backend: str
    is_semantic: bool
    degraded: bool
    model: str | None = None
    fallback_reason: str | None = None
    loaded: bool = Field(default=False, description="模型是否已加载（未加载时 /capabilities 不阻塞）")


class ExamCapabilitiesResponse(BaseModel):
    """考点模块能力与依赖状态（前端据此决定是否显示图片导入）。"""

    embedding: EmbeddingStatusOut
    ocr: OCRStatusOut
    priorities: list[str]
    import_sources: list[str]
    match_algorithm: str = "编辑距离初筛 + 语义精筛"


class OCRImportResponse(BaseModel):
    ocr_text: str
    ocr_backend: str
    ocr_languages: str = ""
    imported: int
    summary: PrioritySummaryOut
    findings: list[ExamFindingOut] = Field(default_factory=list)
    inserted_to_queue: int = 0
    duplicates_skipped: int = 0
    note: str = ""


__all__ = [
    "EmbeddingStatusOut",
    "ExamCapabilitiesResponse",
    "ExamFindingOut",
    "ExamImportResponse",
    "ExamPointImportRequest",
    "ExamPointListResponse",
    "ExamPointMatchRequest",
    "ExamPointOut",
    "OCRImportResponse",
    "OCRStatusOut",
    "PrioritySummaryOut",
    "QueueInsertRequest",
    "QueueInsertResponse",
    "ReviewListResponse",
]
