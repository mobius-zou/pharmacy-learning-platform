"""实验档案 Schema（阶段四）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import DEVIATION_TYPES, EXPERIMENT_TYPES
from .types import MAX_CONTENT_LENGTH, MAX_NAME_LENGTH, UTCDateTime


def _validate_experiment_type(value: str) -> str:
    text = value.strip()
    if text not in EXPERIMENT_TYPES:
        raise ValueError(f"实验类型必须是 {'、'.join(EXPERIMENT_TYPES)} 之一")
    return text


def _validate_deviation_type(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text not in DEVIATION_TYPES:
        raise ValueError(f"偏差类型必须是 {'、'.join(DEVIATION_TYPES)} 之一")
    return text


# --- 试剂 / 仪器 ------------------------------------------------------------


class ReagentOut(BaseModel):
    name: str
    lot: str = ""
    grade: str = ""
    supplier: str = ""


class InstrumentOut(BaseModel):
    name: str
    model: str = ""
    calibration: str = ""
    calibrated_at: str = ""


class DeviationMatchOut(BaseModel):
    """一条命中的历史偏差。"""

    experiment_id: int
    experiment_type: str
    drug_name: str
    deviation_type: str
    deviation_description: str
    deviation_handling: str = ""
    created_at: UTCDateTime | None = None
    relevance: float
    level: str
    reasons: list[str] = Field(default_factory=list)


class TracebackOut(BaseModel):
    """偏差回溯结果。"""

    total: int
    total_scanned: int
    deviation_type_counts: dict[str, int] = Field(default_factory=dict)
    top_deviation_type: str | None = None
    matches: list[DeviationMatchOut] = Field(default_factory=list)
    advice: str = ""


# --- 实验记录 ---------------------------------------------------------------


class ExperimentCreateRequest(BaseModel):
    experiment_type: str = Field(description="合成 / 分析 / 药理 / 药剂")
    drug_name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    drug_structure: str | None = Field(
        default=None, max_length=MAX_CONTENT_LENGTH, description="结构式 / 分子式 / SMILES"
    )
    reagents: list[Any] | None = Field(
        default=None,
        description='试剂与批号，如 [{"name":"盐酸","lot":"20240101"}] 或 ["盐酸|20240101"]',
    )
    instruments: list[Any] | None = Field(
        default=None,
        description='仪器与校准信息，如 [{"name":"HPLC","calibration":"2024-01 已校准"}]',
    )
    procedure: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    expected_result: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    actual_result: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    deviation_description: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    deviation_type: str | None = Field(default=None, description="操作误差 / 试剂问题 / 仪器异常 / 方案设计缺陷")
    deviation_handling: str | None = Field(
        default=None, max_length=MAX_CONTENT_LENGTH, description="处理方式"
    )

    @field_validator("experiment_type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        return _validate_experiment_type(value)

    @field_validator("deviation_type")
    @classmethod
    def _check_deviation(cls, value: str | None) -> str | None:
        return _validate_deviation_type(value)

    @field_validator("drug_name")
    @classmethod
    def _check_drug(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("药物名称不能为空")
        return text


class ExperimentUpdateRequest(BaseModel):
    experiment_type: str | None = None
    drug_name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    drug_structure: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    reagents: list[Any] | None = None
    instruments: list[Any] | None = None
    procedure: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    expected_result: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    actual_result: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    deviation_description: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    deviation_type: str | None = None
    deviation_handling: str | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)

    @field_validator("experiment_type")
    @classmethod
    def _check_type(cls, value: str | None) -> str | None:
        return None if value is None else _validate_experiment_type(value)

    @field_validator("deviation_type")
    @classmethod
    def _check_deviation(cls, value: str | None) -> str | None:
        return _validate_deviation_type(value)


class ExperimentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    experiment_type: str
    drug_name: str
    drug_structure: str | None = None
    reagents: list[Any] = Field(default_factory=list)
    instruments: list[Any] = Field(default_factory=list)
    procedure: str | None = None
    expected_result: str | None = None
    actual_result: str | None = None
    deviation_description: str | None = None
    deviation_type: str | None = None
    deviation_handling: str | None = None
    created_at: UTCDateTime


class ExperimentCreateResponse(BaseModel):
    """创建结果 + **偏差回溯**（创建后自动检索历史记录）。"""

    experiment: ExperimentOut
    traceback: TracebackOut
    history_hint: str = Field(description="给使用者的提示语（有历史偏差时给出）")


class ExperimentListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[ExperimentOut]


# --- 模板与主动提示 ---------------------------------------------------------


class TemplateOut(BaseModel):
    experiment_type: str
    label: str
    typical_reagents: list[str] = Field(default_factory=list)
    typical_instruments: list[str] = Field(default_factory=list)
    procedure_steps: list[str] = Field(default_factory=list)
    key_parameters: list[str] = Field(default_factory=list)
    risk_points: list[str] = Field(default_factory=list)


class TemplateWarningsResponse(BaseModel):
    """进入相似实验模板时的主动提示。"""

    template: TemplateOut
    history: TracebackOut
    # 字段骨架，前端可直接渲染成表单
    field_hints: dict[str, str] = Field(default_factory=dict)


class WarningsQueryResponse(BaseModel):
    experiment_type: str
    drug_name: str = ""
    history: TracebackOut


class DeviationSummaryResponse(BaseModel):
    total: int
    deviation_total: int
    by_experiment_type: dict[str, int] = Field(default_factory=dict)
    by_deviation_type: dict[str, int] = Field(default_factory=dict)
    cross: list[dict[str, Any]] = Field(default_factory=list)
    top_drugs_with_deviations: list[dict[str, Any]] = Field(default_factory=list)
    unknown_deviations: int = 0
    generated_at: str = ""


class OptionsResponse(BaseModel):
    """枚举选项，供前端渲染下拉框。"""

    experiment_types: list[str]
    deviation_types: list[str]


__all__ = [
    "DeviationMatchOut",
    "DeviationSummaryResponse",
    "ExperimentCreateRequest",
    "ExperimentCreateResponse",
    "ExperimentListResponse",
    "ExperimentOut",
    "ExperimentUpdateRequest",
    "InstrumentOut",
    "OptionsResponse",
    "ReagentOut",
    "TemplateOut",
    "TemplateWarningsResponse",
    "TracebackOut",
    "WarningsQueryResponse",
]
