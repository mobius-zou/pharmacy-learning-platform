"""错题诊断路由 /api/v1/wrong-questions（阶段二）。

交互流程（知识点识别需用户确认）::

    POST /wrong-questions/analyze   → 返回识别建议（不落库）
    用户确认/修正知识点
    POST /wrong-questions           → 落库（可带确认后的知识点）

    学生看到 socratic_question
    POST /wrong-questions/{id}/answer  → 记录回答并返回解析材料
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...deps import api_guard, get_current_user
from ...errors import NotFoundError, ValidationError
from ...logging_config import get_logger
from ...models import Card, User, WrongQuestion
from ...schemas import (
    AnswerRequest,
    AnswerResponse,
    CardOut,
    ExplanationOut,
    IncrementErrorRequest,
    QuestionAnalysisRequest,
    QuestionAnalysisResponse,
    WeakPointListResponse,
    WeakPointOut,
    WrongQuestionBulkCreateRequest,
    WrongQuestionBulkCreateResponse,
    WrongQuestionCreateRequest,
    WrongQuestionCreateResponse,
    WrongQuestionListResponse,
    WrongQuestionOut,
    WrongQuestionUpdateRequest,
)
from ...services import wrong_question_service as wq_service

logger = get_logger(__name__)
router = APIRouter(prefix="/wrong-questions", tags=["错题诊断"])


# --- 辅助 -------------------------------------------------------------------


def _weak_names(db: Session, user_id: int) -> set[str]:
    """当前用户的薄弱知识点名称集合。"""
    return {
        point.knowledge_point
        for point in wq_service.list_weak_points(
            db, user_id, weak_only=True, with_related_cards=False
        )
    }


def _to_out(record: WrongQuestion, weak_names: set[str]) -> WrongQuestionOut:
    out = WrongQuestionOut.model_validate(record)
    out.is_weak = record.knowledge_point in weak_names
    return out


def _get_owned(
    db: Session, user: User, wrong_question_id: int
) -> WrongQuestion:
    record = wq_service.get_wrong_question(db, user.id, wrong_question_id)
    if record is None:
        raise NotFoundError(f"错题不存在：{wrong_question_id}")
    return record


def _weak_point_out(point: wq_service.WeakPoint) -> WeakPointOut:
    return WeakPointOut(
        knowledge_point=point.knowledge_point,
        total_errors=point.total_errors,
        question_count=point.question_count,
        threshold=point.threshold,
        is_weak=point.is_weak,
        severity=point.severity,
        errors_to_threshold=point.errors_to_threshold,
        related_card_ids=list(point.related_card_ids),
        last_error_at=point.last_error_at,
    )


# --- 知识点识别（需用户确认） -----------------------------------------------


@router.post(
    "/analyze",
    response_model=QuestionAnalysisResponse,
    summary="识别知识点并生成苏格拉底式引导问题（不落库）",
)
@api_guard
def analyze_question(
    payload: QuestionAnalysisRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> QuestionAnalysisResponse:
    """LLM 单次调用完成「知识点识别 + 引导问题生成」。

    LLM 未配置或调用失败时自动退化为本地启发式（无 Agent 降级模式），
    结果中的 ``source`` 字段标明来源，``requires_confirmation`` 提示需用户确认。
    """
    known = wq_service.known_knowledge_points(db, current_user.id)
    analysis = wq_service.analyze_question(payload.question_text, known)

    return QuestionAnalysisResponse(
        question_text=analysis.question_text,
        knowledge_point=analysis.suggestion.knowledge_point,
        source=analysis.suggestion.source,
        confidence=analysis.suggestion.confidence,
        alternatives=list(analysis.suggestion.alternatives),
        requires_confirmation=analysis.suggestion.requires_confirmation,
        llm_error=analysis.suggestion.llm_error,
        socratic_question=analysis.socratic.question,
        socratic_question_type=analysis.socratic.question_type,
        socratic_type_label=analysis.socratic.type_label,
        socratic_source=analysis.socratic.source,
        has_standard_answer=analysis.socratic.has_answer,
    )


@router.get(
    "/weak-points",
    response_model=WeakPointListResponse,
    summary="薄弱点警报列表",
)
@api_guard
def list_weak_points(
    weak_only: bool = Query(True, description="仅返回已达阈值的知识点"),
    with_related_cards: bool = Query(True, description="是否附带关联卡片 id"),
    threshold: int | None = Query(None, ge=1, description="覆盖默认阈值"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WeakPointListResponse:
    """同一知识点累计错误次数达到阈值即触发警报。"""
    points = wq_service.list_weak_points(
        db,
        current_user.id,
        threshold,
        weak_only=weak_only,
        with_related_cards=with_related_cards,
    )
    return WeakPointListResponse(
        threshold=threshold or settings.weak_point_threshold,
        total=len(points),
        items=[_weak_point_out(point) for point in points],
    )


# --- 录入 -------------------------------------------------------------------


@router.post(
    "",
    response_model=WrongQuestionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="录入一道错题",
)
@api_guard
def create_wrong_question(
    payload: WrongQuestionCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WrongQuestionCreateResponse:
    """录入错题。

    不传 ``knowledge_point`` 时自动识别（LLM → 启发式），
    响应中的 ``knowledge_point_source`` / ``requires_confirmation`` /
    ``alternatives`` 用于提示用户确认或改选。
    """
    record, suggestion, socratic = wq_service.create_wrong_question(
        db,
        current_user.id,
        payload.question_text,
        knowledge_point=payload.knowledge_point,
        error_count=payload.error_count,
        socratic_question=payload.socratic_question,
        socratic_question_type=payload.socratic_question_type,
    )

    base = WrongQuestionOut.model_validate(record)
    base.is_weak = record.knowledge_point in _weak_names(db, current_user.id)
    return WrongQuestionCreateResponse(
        **base.model_dump(),
        knowledge_point_source=suggestion.source,
        requires_confirmation=suggestion.requires_confirmation,
        alternatives=list(suggestion.alternatives),
        llm_error=suggestion.llm_error,
        socratic_source=socratic.source,
    )


@router.post(
    "/bulk",
    response_model=WrongQuestionBulkCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="粘贴文本批量录入错题",
)
@api_guard
def create_wrong_questions_bulk(
    payload: WrongQuestionBulkCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WrongQuestionBulkCreateResponse:
    """每行一道错题；``题干 | 知识点`` 可为单行指定知识点。"""
    records, errors = wq_service.create_wrong_questions_bulk(
        db, current_user.id, payload.text, payload.knowledge_point
    )
    weak = _weak_names(db, current_user.id)
    return WrongQuestionBulkCreateResponse(
        created=len(records),
        errors=errors,
        items=[_to_out(record, weak) for record in records],
    )


# --- 查询 -------------------------------------------------------------------


@router.get("", response_model=WrongQuestionListResponse, summary="分页查询错题")
@api_guard
def list_wrong_questions(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    knowledge_point: str | None = Query(None, description="按知识点精确过滤"),
    keyword: str | None = Query(None, description="题干/知识点模糊搜索"),
    weak_only: bool = Query(False, description="仅返回薄弱知识点的错题"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WrongQuestionListResponse:
    """分页查询当前用户的错题。"""
    items, total = wq_service.list_wrong_questions(
        db,
        current_user.id,
        offset=offset,
        limit=limit,
        knowledge_point=knowledge_point,
        keyword=keyword,
        weak_only=weak_only,
    )
    weak = _weak_names(db, current_user.id)
    return WrongQuestionListResponse(
        total=total,
        offset=offset,
        limit=limit,
        weak_knowledge_points=sorted(weak),
        items=[_to_out(record, weak) for record in items],
    )


@router.get("/{wrong_question_id}", response_model=WrongQuestionOut, summary="错题详情")
@api_guard
def get_wrong_question(
    wrong_question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WrongQuestionOut:
    record = _get_owned(db, current_user, wrong_question_id)
    return _to_out(record, _weak_names(db, current_user.id))


@router.get(
    "/{wrong_question_id}/related-cards",
    response_model=list[CardOut],
    summary="该知识点关联的卡片",
)
@api_guard
def list_related_cards(
    wrong_question_id: int,
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Card]:
    """展示知识点与 cards 表的自动关联结果。"""
    record = _get_owned(db, current_user, wrong_question_id)
    return wq_service.related_cards(db, current_user.id, record.knowledge_point, limit)


# --- 修改 / 删除 ------------------------------------------------------------


@router.patch(
    "/{wrong_question_id}", response_model=WrongQuestionOut, summary="修正知识点或题干"
)
@api_guard
def update_wrong_question(
    wrong_question_id: int,
    payload: WrongQuestionUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WrongQuestionOut:
    """用于「用户确认/修正识别出的知识点」。"""
    record = _get_owned(db, current_user, wrong_question_id)
    try:
        wq_service.update_wrong_question(
            db,
            record,
            question_text=payload.question_text,
            knowledge_point=payload.knowledge_point,
            socratic_question=payload.socratic_question,
            socratic_question_type=payload.socratic_question_type,
        )
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return _to_out(record, _weak_names(db, current_user.id))


@router.delete(
    "/{wrong_question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除错题",
)
@api_guard
def delete_wrong_question(
    wrong_question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    record = _get_owned(db, current_user, wrong_question_id)
    wq_service.delete_wrong_question(db, record)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- 复习与作答 -------------------------------------------------------------


@router.post(
    "/{wrong_question_id}/errors",
    response_model=WrongQuestionOut,
    summary="再次做错，累加错误次数",
)
@api_guard
def increment_error(
    wrong_question_id: int,
    payload: IncrementErrorRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> WrongQuestionOut:
    """累加错误次数；达到阈值即触发薄弱点警报。"""
    record = _get_owned(db, current_user, wrong_question_id)
    try:
        wq_service.increment_error(db, record, payload.delta)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return _to_out(record, _weak_names(db, current_user.id))


@router.post(
    "/{wrong_question_id}/answer",
    response_model=AnswerResponse,
    summary="回答苏格拉底式提问并获取解析",
)
@api_guard
def answer_wrong_question(
    wrong_question_id: int,
    payload: AnswerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AnswerResponse:
    """先记录学生的回答，再返回解析材料。

    系统不会在学生作答前给出解析：调用本接口即完成作答，
    响应中的解析来自**关联卡片**的正确答案；偏好性问题无标准答案。
    """
    record = _get_owned(db, current_user, wrong_question_id)
    wq_service.answer_wrong_question(db, record, payload.user_answer)
    explanation = wq_service.build_explanation(db, record)

    return AnswerResponse(
        wrong_question=_to_out(record, _weak_names(db, current_user.id)),
        explanation=ExplanationOut(
            knowledge_point=explanation.knowledge_point,
            has_standard_answer=explanation.has_standard_answer,
            reference_answers=explanation.reference_answers,
            guidance=explanation.guidance,
        ),
    )
