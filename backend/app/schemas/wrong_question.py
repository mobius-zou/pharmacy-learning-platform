"""错题诊断相关 Schema（阶段二）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from ..services.wrong_question_service import (
    SOCRATIC_TYPE_LABELS,
    VALID_SOCRATIC_TYPES,
    socratic_has_answer,
    socratic_type_label,
)
from .types import (
    MAX_BULK_TEXT_LENGTH,
    MAX_CONTENT_LENGTH,
    MAX_SHORT_TEXT_LENGTH,
    UTCDateTime,
)


def _validate_socratic_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_SOCRATIC_TYPES:
        raise ValueError(
            f"问题类型必须是 {'、'.join(VALID_SOCRATIC_TYPES)} 之一"
        )
    return normalized


# --- 录入 -------------------------------------------------------------------


class WrongQuestionCreateRequest(BaseModel):
    question_text: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH, description="题干")
    knowledge_point: str | None = Field(
        default=None,
        max_length=MAX_SHORT_TEXT_LENGTH,
        description="知识点。为空时自动识别（LLM → 本地启发式），识别结果需用户确认",
    )
    error_count: int = Field(default=1, ge=1, le=1000, description="累计错误次数")
    socratic_question: str | None = Field(
        default=None, max_length=MAX_CONTENT_LENGTH, description="引导问题（为空则自动生成）"
    )
    socratic_question_type: str | None = Field(
        default=None, description="procedural / preference / judgment"
    )

    @field_validator("question_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("题干不能为空")
        return value.strip()

    @field_validator("knowledge_point")
    @classmethod
    def _clean_kp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split()).strip()
        return text or None

    @field_validator("socratic_question_type")
    @classmethod
    def _check_type(cls, value: str | None) -> str | None:
        return _validate_socratic_type(value)


class WrongQuestionBulkCreateRequest(BaseModel):
    text: str = Field(
        min_length=1,
        max_length=MAX_BULK_TEXT_LENGTH,
        description="每行一道错题；可用 `题干 | 知识点` 指定",
    )
    knowledge_point: str | None = Field(
        default=None, max_length=MAX_SHORT_TEXT_LENGTH, description="应用于本批全部错题"
    )


class WrongQuestionUpdateRequest(BaseModel):
    question_text: str | None = Field(default=None, min_length=1, max_length=MAX_CONTENT_LENGTH)
    knowledge_point: str | None = Field(
        default=None, max_length=MAX_SHORT_TEXT_LENGTH, description="用户确认/修正后的知识点"
    )
    socratic_question: str | None = None
    socratic_question_type: str | None = None

    @field_validator("socratic_question_type")
    @classmethod
    def _check_type(cls, value: str | None) -> str | None:
        return _validate_socratic_type(value)


# --- 输出 -------------------------------------------------------------------


class WrongQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    question_text: str
    knowledge_point: str
    error_count: int
    created_at: UTCDateTime
    last_reviewed_at: UTCDateTime | None = None

    socratic_question: str | None = None
    socratic_question_type: str | None = None
    user_answer: str | None = None

    # 由路由层根据薄弱点聚合结果填充（数据库中没有该列）
    is_weak: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def socratic_type_label(self) -> str | None:
        return socratic_type_label(self.socratic_question_type)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_standard_answer(self) -> bool:
        """程序性/判断性问题有答案；偏好性问题无答案。"""
        return socratic_has_answer(self.socratic_question_type)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_answered(self) -> bool:
        return bool(self.user_answer)


class WrongQuestionListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    weak_knowledge_points: list[str] = Field(default_factory=list)
    items: list[WrongQuestionOut]


class WrongQuestionCreateResponse(WrongQuestionOut):
    """创建结果 + 知识点识别元信息（提示前端是否需要用户确认）。"""

    knowledge_point_source: str = Field(description="llm / heuristic / user")
    requires_confirmation: bool = False
    alternatives: list[str] = Field(default_factory=list)
    llm_error: str | None = None
    socratic_source: str = "heuristic"


class WrongQuestionBulkCreateResponse(BaseModel):
    created: int
    errors: list[str] = Field(default_factory=list)
    items: list[WrongQuestionOut] = Field(default_factory=list)


# --- 知识点识别（需用户确认） -----------------------------------------------


class QuestionAnalysisRequest(BaseModel):
    question_text: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)

    @field_validator("question_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("题干不能为空")
        return value.strip()


class QuestionAnalysisResponse(BaseModel):
    """识别结果，供用户确认后再落库。"""

    question_text: str
    knowledge_point: str
    source: str = Field(description="llm / heuristic / user")
    confidence: float
    alternatives: list[str] = Field(default_factory=list)
    requires_confirmation: bool = True
    llm_error: str | None = None

    socratic_question: str
    socratic_question_type: str
    socratic_type_label: str
    socratic_source: str
    has_standard_answer: bool


# --- 薄弱点 -----------------------------------------------------------------


class WeakPointOut(BaseModel):
    knowledge_point: str
    total_errors: int
    question_count: int
    threshold: int
    is_weak: bool
    severity: str
    errors_to_threshold: int
    related_card_ids: list[int] = Field(default_factory=list)
    last_error_at: UTCDateTime | None = None


class WeakPointListResponse(BaseModel):
    threshold: int
    total: int
    items: list[WeakPointOut]


# --- 苏格拉底式作答 ---------------------------------------------------------


class AnswerRequest(BaseModel):
    user_answer: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH, description="学生对引导问题的回答")


class ReferenceAnswer(BaseModel):
    card_id: int
    front: str
    back: str


class ExplanationOut(BaseModel):
    knowledge_point: str
    has_standard_answer: bool
    reference_answers: list[ReferenceAnswer] = Field(default_factory=list)
    guidance: str = ""


class AnswerResponse(BaseModel):
    wrong_question: WrongQuestionOut
    explanation: ExplanationOut


class IncrementErrorRequest(BaseModel):
    delta: int = Field(default=1, ge=1, le=100, description="本次新增的错误次数")


__all__ = [
    "AnswerRequest",
    "AnswerResponse",
    "ExplanationOut",
    "IncrementErrorRequest",
    "QuestionAnalysisRequest",
    "QuestionAnalysisResponse",
    "ReferenceAnswer",
    "WeakPointListResponse",
    "WeakPointOut",
    "WrongQuestionBulkCreateRequest",
    "WrongQuestionBulkCreateResponse",
    "WrongQuestionCreateRequest",
    "WrongQuestionCreateResponse",
    "WrongQuestionListResponse",
    "WrongQuestionOut",
    "WrongQuestionUpdateRequest",
    "SOCRATIC_TYPE_LABELS",
]
