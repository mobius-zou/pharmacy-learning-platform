"""实验档案路由 /api/v1/experiments（阶段四）。

偏差回溯闭环
------------
1. ``POST /experiments``：记录实验；若填写了偏差，响应中**自动带回**历史同类偏差；
2. ``GET /experiments/template/{experiment_type}``：进入某类实验模板时，
   **主动提示**该类型的历史偏差与风险点；
3. ``GET /experiments/warnings``：按实验类型 + 药物/试剂相似度召回历史偏差。

全部为规则匹配 + MySQL 查询，不涉及任何 AI 调用。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ...database import get_db
from ...deps import api_guard, get_current_user
from ...errors import NotFoundError, ValidationError
from ...logging_config import get_logger
from ...models import DEVIATION_TYPES, EXPERIMENT_TYPES, Experiment, User
from ...schemas import (
    DeviationMatchOut,
    DeviationSummaryResponse,
    ExperimentCreateRequest,
    ExperimentCreateResponse,
    ExperimentListResponse,
    ExperimentOut,
    ExperimentUpdateRequest,
    OptionsResponse,
    TemplateOut,
    TemplateWarningsResponse,
    TracebackOut,
    WarningsQueryResponse,
)
from ...services import experiment_service as exp_service

logger = get_logger(__name__)
router = APIRouter(prefix="/experiments", tags=["实验档案"])

# 表单字段提示（供前端渲染模板骨架）
FIELD_HINTS: dict[str, str] = {
    "experiment_type": "实验类型：合成 / 分析 / 药理 / 药剂",
    "drug_name": "药物名称",
    "drug_structure": "药物结构：结构式 / 分子式 / SMILES",
    "reagents": '试剂与批号，如 [{"name":"盐酸","lot":"20240101"}]',
    "instruments": '仪器与校准信息，如 [{"name":"HPLC","calibration":"2024-01 已校准"}]',
    "procedure": "操作步骤",
    "expected_result": "预期结果",
    "actual_result": "实际结果",
    "deviation_description": "偏差描述（无偏差可留空）",
    "deviation_type": "偏差类型：操作误差 / 试剂问题 / 仪器异常 / 方案设计缺陷",
    "deviation_handling": "处理方式",
}


# --- 辅助 -------------------------------------------------------------------


def _traceback_out(result: exp_service.TracebackResult, experiment_type: str = "") -> TracebackOut:
    return TracebackOut(
        total=result.total,
        total_scanned=result.total_scanned,
        deviation_type_counts=result.deviation_type_counts,
        top_deviation_type=result.top_deviation_type,
        advice=exp_service.build_advice(result, experiment_type),
        matches=[
            DeviationMatchOut(
                experiment_id=match.experiment_id,
                experiment_type=match.facts.experiment_type,
                drug_name=match.facts.drug_name,
                deviation_type=match.facts.deviation_type,
                deviation_description=match.facts.deviation_description,
                deviation_handling=match.facts.deviation_handling,
                created_at=match.facts.created_at,
                relevance=match.relevance,
                level=match.level,
                reasons=list(match.reasons),
            )
            for match in result.matches
        ],
    )


def _template_out(template: exp_service.ExperimentTemplate) -> TemplateOut:
    return TemplateOut(
        experiment_type=template.experiment_type,
        label=template.label,
        typical_reagents=list(template.typical_reagents),
        typical_instruments=list(template.typical_instruments),
        procedure_steps=list(template.procedure_steps),
        key_parameters=list(template.key_parameters),
        risk_points=list(template.risk_points),
    )


def _get_owned(db: Session, user: User, experiment_id: int) -> Experiment:
    record = exp_service.get_experiment(db, user.id, experiment_id)
    if record is None:
        raise NotFoundError(f"实验记录不存在：{experiment_id}")
    return record


# --- 枚举与模板（必须声明在 /{experiment_id} 之前） -------------------------


@router.get("/options", response_model=OptionsResponse, summary="实验/偏差类型选项")
@api_guard
def get_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OptionsResponse:
    return OptionsResponse(
        experiment_types=list(EXPERIMENT_TYPES), deviation_types=list(DEVIATION_TYPES)
    )


@router.get("/templates", response_model=list[TemplateOut], summary="全部实验模板")
@api_guard
def list_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TemplateOut]:
    return [_template_out(template) for template in exp_service.list_templates()]


@router.get(
    "/template/{experiment_type}",
    response_model=TemplateWarningsResponse,
    summary="进入实验模板时主动提示历史偏差",
)
@api_guard
def get_template_with_warnings(
    experiment_type: str,
    drug_name: str = Query("", description="将要做的药物名称，用于提升提示相关度"),
    reagent: list[str] | None = Query(None, description="计划使用的试剂（可重复）"),
    instrument: list[str] | None = Query(None, description="计划使用的仪器（可重复）"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TemplateWarningsResponse:
    """验收标准 2：下次创建相似实验时，系统提示历史偏差。

    模板骨架 + 该实验类型的历史偏差（按药物/试剂/仪器相似度排序）。
    """
    template = exp_service.get_template(experiment_type)
    if template is None:
        raise ValidationError(
            f"未知实验类型：{experiment_type}；可选 {'、'.join(EXPERIMENT_TYPES)}"
        )

    result = exp_service.template_warnings(
        db,
        current_user.id,
        experiment_type,
        drug_name=drug_name,
        reagents=reagent or [],
        instruments=instrument or [],
        limit=limit,
    )
    return TemplateWarningsResponse(
        template=_template_out(template),
        history=_traceback_out(result, experiment_type),
        field_hints=FIELD_HINTS,
    )


@router.get(
    "/warnings",
    response_model=WarningsQueryResponse,
    summary="按条件查询历史偏差（主动提示）",
)
@api_guard
def query_warnings(
    experiment_type: str = Query(..., description="实验类型"),
    drug_name: str = Query(""),
    deviation_type: str | None = Query(None, description="留空表示尚未确定偏差类型"),
    reagent: list[str] | None = Query(None),
    instrument: list[str] | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WarningsQueryResponse:
    """规则引擎召回历史偏差：实验类型 + 偏差类型为必要条件。"""
    if experiment_type not in EXPERIMENT_TYPES:
        raise ValidationError(
            f"未知实验类型：{experiment_type}；可选 {'、'.join(EXPERIMENT_TYPES)}"
        )
    if deviation_type and deviation_type not in DEVIATION_TYPES:
        raise ValidationError(
            f"未知偏差类型：{deviation_type}；可选 {'、'.join(DEVIATION_TYPES)}"
        )

    query = exp_service.DeviationQuery(
        experiment_type=experiment_type,
        deviation_type=deviation_type,
        drug_name=drug_name,
        reagents=tuple(exp_service.normalize_reagent_list(reagent or [])),
        instruments=tuple(exp_service.normalize_instrument_list(instrument or [])),
    )
    result = exp_service.find_related_deviations(db, current_user.id, query, limit=limit)
    return WarningsQueryResponse(
        experiment_type=experiment_type,
        drug_name=drug_name,
        history=_traceback_out(result, experiment_type),
    )


@router.get("/summary", response_model=DeviationSummaryResponse, summary="偏差统计")
@api_guard
def get_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DeviationSummaryResponse:
    return DeviationSummaryResponse(**exp_service.deviation_summary(db, current_user.id))


# --- 创建（含自动回溯） -----------------------------------------------------


@router.post(
    "",
    response_model=ExperimentCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="记录实验（自动回溯历史偏差）",
)
@api_guard
def create_experiment(
    payload: ExperimentCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExperimentCreateResponse:
    """验收标准 1：能创建一份实验记录，含偏差描述和偏差类型。

    记录完成后规则引擎立即检索历史记录：
    **偏差类型相同 + 实验类型相同** 的历史偏差会被带回响应。
    """
    try:
        record = exp_service.create_experiment(
            db,
            current_user.id,
            experiment_type=payload.experiment_type,
            drug_name=payload.drug_name,
            drug_structure=payload.drug_structure,
            reagents=payload.reagents,
            instruments=payload.instruments,
            procedure=payload.procedure,
            expected_result=payload.expected_result,
            actual_result=payload.actual_result,
            deviation_description=payload.deviation_description,
            deviation_type=payload.deviation_type,
            deviation_handling=payload.deviation_handling,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    # 记录完成后自动回溯（排除刚写入的这条自身）
    result = exp_service.TracebackResult()
    if record.deviation_type:
        query = exp_service.DeviationQuery(
            experiment_type=record.experiment_type,
            deviation_type=record.deviation_type,
            drug_name=record.drug_name,
            reagents=tuple(exp_service.normalize_reagent_list(record.reagents)),
            instruments=tuple(exp_service.normalize_instrument_list(record.instruments)),
            exclude_experiment_id=record.id,
        )
        result = exp_service.find_related_deviations(db, current_user.id, query)

    return ExperimentCreateResponse(
        experiment=ExperimentOut.model_validate(record),
        traceback=_traceback_out(result, record.experiment_type),
        history_hint=exp_service.build_advice(result, record.experiment_type),
    )


# --- 查询 -------------------------------------------------------------------


@router.get("", response_model=ExperimentListResponse, summary="分页查询实验记录")
@api_guard
def list_experiments(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    experiment_type: str | None = Query(None),
    drug_name: str | None = Query(None),
    deviation_type: str | None = Query(None),
    only_deviations: bool = Query(False, description="仅返回有偏差的记录"),
    keyword: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExperimentListResponse:
    items, total = exp_service.list_experiments(
        db,
        current_user.id,
        offset=offset,
        limit=limit,
        experiment_type=experiment_type,
        drug_name=drug_name,
        deviation_type=deviation_type,
        only_deviations=only_deviations,
        keyword=keyword,
    )
    return ExperimentListResponse(
        total=total,
        offset=offset,
        limit=limit,
        items=[ExperimentOut.model_validate(item) for item in items],
    )


@router.get("/{experiment_id}", response_model=ExperimentOut, summary="实验记录详情")
@api_guard
def get_experiment(
    experiment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExperimentOut:
    return ExperimentOut.model_validate(_get_owned(db, current_user, experiment_id))


@router.get(
    "/{experiment_id}/traceback",
    response_model=TracebackOut,
    summary="对该记录重新执行偏差回溯",
)
@api_guard
def get_traceback(
    experiment_id: int,
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TracebackOut:
    """按「实验类型 + 偏差类型」检索其他历史记录。"""
    record = _get_owned(db, current_user, experiment_id)
    query = exp_service.DeviationQuery(
        experiment_type=record.experiment_type,
        deviation_type=record.deviation_type,
        drug_name=record.drug_name,
        reagents=tuple(exp_service.normalize_reagent_list(record.reagents)),
        instruments=tuple(exp_service.normalize_instrument_list(record.instruments)),
        exclude_experiment_id=record.id,
    )
    result = exp_service.find_related_deviations(db, current_user.id, query, limit=limit)
    return _traceback_out(result, record.experiment_type)


# --- 修改 / 删除 ------------------------------------------------------------


@router.patch("/{experiment_id}", response_model=ExperimentOut, summary="修改实验记录")
@api_guard
def update_experiment(
    experiment_id: int,
    payload: ExperimentUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExperimentOut:
    record = _get_owned(db, current_user, experiment_id)
    fields: dict[str, Any] = payload.model_dump(exclude_unset=True)
    try:
        exp_service.update_experiment(db, record, **fields)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return ExperimentOut.model_validate(record)


@router.delete("/{experiment_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除实验记录")
@api_guard
def delete_experiment(
    experiment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    record = _get_owned(db, current_user, experiment_id)
    exp_service.delete_experiment(db, record)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
