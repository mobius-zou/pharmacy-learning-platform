"""考点匹配路由 /api/v1/exam-points（阶段五）。

流程
----
1. ``POST /exam-points/match``：试匹配（**不落库**），先看匹配质量；
2. ``POST /exam-points/import``：导入考点文本 → 匹配 → 判定优先级 → 落库；
   ``import/image``（OCR，PPT 截图 / 试卷照片）与 ``import/file``（txt）同理；
3. ``GET /exam-points/review-list``：按 高危 / 巩固 / 确认 生成针对性复习清单；
4. ``POST /exam-points/to-review-queue``：把高危考点插入 FSRS 复习队列。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ... import ocr as ocr_module
from ...config import settings
from ...database import get_db
from ...deps import api_guard, get_current_user
from ...embeddings import describe as describe_embedding
from ...errors import APIError, NotFoundError, ValidationError
from ...logging_config import get_logger
from ...middleware import read_upload_limited
from ...models import PRIORITIES, PRIORITY_CONFIRM, PRIORITY_CONSOLIDATE, PRIORITY_HIGH, User
from ...schemas import (
    EmbeddingStatusOut,
    ExamCapabilitiesResponse,
    ExamFindingOut,
    ExamImportResponse,
    ExamPointImportRequest,
    ExamPointListResponse,
    ExamPointMatchRequest,
    ExamPointOut,
    OCRImportResponse,
    OCRStatusOut,
    PrioritySummaryOut,
    QueueInsertRequest,
    QueueInsertResponse,
    ReviewListResponse,
)
from ...services import exam_match_service as exam_service

logger = get_logger(__name__)
router = APIRouter(prefix="/exam-points", tags=["考点匹配"])

# 支持的导入方式（供前端展示）
IMPORT_SOURCES = ["text", "txt_file", "image_ocr"]


# --- 辅助 -------------------------------------------------------------------


def _finding_out(finding: exam_service.ExamFinding) -> ExamFindingOut:
    return ExamFindingOut(**finding.to_dict())


def _summary_out(db: Session, user_id: int) -> PrioritySummaryOut:
    return PrioritySummaryOut(**exam_service.priority_summary(db, user_id))


def _queue_note(inserted: int, skipped: int) -> str:
    if inserted and skipped:
        return f"已插入 {inserted} 条高危考点，{skipped} 条已存在（幂等跳过）"
    if inserted:
        return f"已插入 {inserted} 条高危考点到复习队列"
    if skipped:
        return f"{skipped} 条高危考点已在复习队列中，无需重复插入"
    return "没有需要插入的高危考点"


def _auto_insert_high_risk(
    db: Session, user_id: int, findings: list[exam_service.ExamFinding]
) -> tuple[int, int]:
    """导入后自动把高危考点插入复习队列。

    ``insert_into_review_queue`` 内部会重新生成一批 ``ExamFinding``，
    因此必须把新生成的 card_ids **回填**到调用方持有的 findings 上，
    否则响应里 ``inserted_to_queue>0`` 却看不到任何 card_ids。
    """
    high_risk_ids = [
        finding.exam_point_id
        for finding in findings
        if finding.priority == PRIORITY_HIGH and finding.exam_point_id
    ]
    if not high_risk_ids:
        return 0, 0

    inserted, skipped = exam_service.insert_into_review_queue(
        db, user_id, exam_point_ids=high_risk_ids, priorities=[PRIORITY_HIGH]
    )
    created_by_point = {item.exam_point_id: list(item.card_ids) for item in inserted}
    for finding in findings:
        card_ids = created_by_point.get(finding.exam_point_id)
        if card_ids:
            finding.card_ids = card_ids
    return len(inserted), skipped


def _import_text(
    db: Session,
    user_id: int,
    text: str,
    max_points: int | None,
    auto_insert: bool,
) -> tuple[list[exam_service.ExamFinding], int, int]:
    findings = exam_service.import_exam_points(db, user_id, text, max_points=max_points)
    inserted = skipped = 0
    if auto_insert and findings:
        inserted, skipped = _auto_insert_high_risk(db, user_id, findings)
    return findings, inserted, skipped


# --- 能力与状态（必须声明在 /{exam_point_id} 之前） -------------------------


@router.get(
    "/capabilities",
    response_model=ExamCapabilitiesResponse,
    summary="考点模块能力与依赖状态",
)
@api_guard
def get_capabilities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExamCapabilitiesResponse:
    """返回嵌入后端与 OCR 的可用性。

    ``embedding.degraded=True`` 表示 sentence-transformers 不可用、
    已降级为离线确定性嵌入；``ocr.available=False`` 表示图片导入不可用，
    但文本导入始终可用。
    """
    embedding = describe_embedding(probe=False)  # 不加载模型，避免接口阻塞
    return ExamCapabilitiesResponse(
        embedding=EmbeddingStatusOut(**embedding),
        ocr=OCRStatusOut(**ocr_module.ocr_status()),
        priorities=list(PRIORITIES),
        import_sources=IMPORT_SOURCES,
    )


# --- 试匹配（不落库） -------------------------------------------------------


@router.post("/match", response_model=ExamImportResponse, summary="试匹配考点（不落库）")
@api_guard
def match_exam_points(
    payload: ExamPointMatchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExamImportResponse:
    """先看匹配质量：切分 + 匹配 + 优先级判定，但不写入数据库。"""
    findings = exam_service.preview_matches(
        db, current_user.id, payload.text, max_points=payload.max_points
    )
    embedding = describe_embedding()
    return ExamImportResponse(
        imported=0,
        points_found=len(findings),
        embedding_backend=embedding["backend"],
        degraded=bool(embedding["degraded"]),
        summary=_summary_out(db, current_user.id),
        findings=[_finding_out(f) for f in findings],
        note="试匹配结果未写入数据库",
    )


# --- 导入 -------------------------------------------------------------------


@router.post(
    "/import",
    response_model=ExamImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="导入考点文本（自动匹配 + 高危插入复习队列）",
)
@api_guard
def import_exam_points(
    payload: ExamPointImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExamImportResponse:
    """验收标准 1：导入一段考点文本，能匹配到知识图谱中的知识点。

    匹配同时按验收标准 2 分好 高危 / 巩固 / 确认 三级；
    默认把高危考点自动插入复习队列（验收标准 3）。
    """
    findings, inserted, skipped = _import_text(
        db,
        current_user.id,
        payload.text,
        payload.max_points,
        payload.auto_insert_high_risk,
    )
    if not findings:
        raise ValidationError("没有解析到任何考点，请检查文本内容")

    embedding = describe_embedding()
    logger.info(
        "考点导入完成 user_id=%s 共 %d 条，插入队列 %d 条",
        current_user.id,
        len(findings),
        inserted,
    )
    return ExamImportResponse(
        imported=len(findings),
        points_found=len(findings),
        embedding_backend=embedding["backend"],
        degraded=bool(embedding["degraded"]),
        summary=_summary_out(db, current_user.id),
        findings=[_finding_out(f) for f in findings],
        inserted_to_queue=inserted,
        queue_skipped=skipped,
        note=_queue_note(inserted, skipped) if payload.auto_insert_high_risk else "未自动插入复习队列",
    )


def _decode_text_file(raw: bytes) -> str:
    """中文 txt 常见 utf-8 / gbk，逐个尝试。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@router.post(
    "/import/file",
    response_model=ExamImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="从 txt 文件导入考点",
)
@api_guard
async def import_from_file(
    file: UploadFile = File(..., description="考点文本文件（txt）"),
    max_points: int | None = Query(None, ge=1, le=500),
    auto_insert_high_risk: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExamImportResponse:
    """上传一个文本文件导入考点（支持 utf-8 / GBK 编码）。"""
    # 分块限量读取：先看声明大小，再边读边计数，避免先把超大文件读进内存
    raw = await read_upload_limited(file, settings.max_text_file_bytes)
    if not raw:
        raise ValidationError("文件内容为空")

    text = _decode_text_file(raw)
    # _import_text 里是同步 DB + 嵌入模型调用，直接跑在事件循环上会阻塞所有请求
    findings, inserted, skipped = await run_in_threadpool(
        _import_text, db, current_user.id, text, max_points, auto_insert_high_risk
    )
    if not findings:
        raise ValidationError("文件中没有解析到任何考点")

    embedding = describe_embedding()
    return ExamImportResponse(
        imported=len(findings),
        points_found=len(findings),
        embedding_backend=embedding["backend"],
        degraded=bool(embedding["degraded"]),
        summary=_summary_out(db, current_user.id),
        findings=[_finding_out(f) for f in findings],
        inserted_to_queue=inserted,
        queue_skipped=skipped,
        note=f"来源文件：{file.filename}",
    )


@router.post(
    "/import/image",
    response_model=OCRImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="从 PPT 截图 / 试卷照片导入考点（OCR）",
)
@api_guard
async def import_from_image(
    file: UploadFile = File(..., description="图片：PPT 截图 / 试卷照片"),
    max_points: int | None = Query(None, ge=1, le=500),
    auto_insert_high_risk: bool = Query(True),
    languages: str = Query(ocr_module.DEFAULT_LANGUAGES, description="OCR 语言，如 chi_sim+eng"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> OCRImportResponse:
    """图片 → OCR → 考点匹配。

    OCR 引擎不可用时返回 503 并附安装指引（**不会** 500），
    此时可改用文本粘贴或 txt 文件导入。
    """
    # 分块限量读取（OCR 层还有一层大小校验，形成双保险）
    raw = await read_upload_limited(file, ocr_module.MAX_IMAGE_BYTES)
    if not raw:
        raise ValidationError("图片内容为空")

    # OCR 是 subprocess + PIL 的同步重活（秒级），必须放到线程池，
    # 否则会占住事件循环，导致同 worker 的其他请求全部超时。
    result = await run_in_threadpool(
        ocr_module.extract_text_from_image, raw, languages
    )
    if not result.ok:
        status_info = ocr_module.ocr_status()
        if not status_info.get("available"):
            raise APIError(
                "OCR 引擎不可用，无法识别图片",
                status_code=503,
                error="ocr_unavailable",
                detail={
                    "reason": status_info.get("reason"),
                    "install_hint": status_info.get("install_hint"),
                    "alternatives": status_info.get("alternatives", []),
                },
            )
        raise ValidationError(f"OCR 识别失败：{result.error}")

    if not result.text.strip():
        raise ValidationError("OCR 未能从图片中识别出文字，请上传更清晰的图片")

    findings, inserted, _skipped = await run_in_threadpool(
        _import_text, db, current_user.id, result.text, max_points, auto_insert_high_risk
    )
    return OCRImportResponse(
        ocr_text=result.text,
        ocr_backend=result.backend,
        ocr_languages=result.languages,
        imported=len(findings),
        summary=_summary_out(db, current_user.id),
        findings=[_finding_out(f) for f in findings],
        inserted_to_queue=inserted,
        note=f"来源图片：{file.filename}" if file.filename else "",
    )


# --- 复习清单 ---------------------------------------------------------------


@router.get("/review-list", response_model=ReviewListResponse, summary="针对性复习清单")
@api_guard
def get_review_list(
    priorities: list[str] | None = Query(None, description="只返回指定优先级"),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewListResponse:
    """按 高危 / 巩固 / 确认 分组输出复习清单。"""
    selected = None
    if priorities:
        selected = []
        for value in priorities:
            text = value.strip()
            if not text:
                continue
            if text not in PRIORITIES:
                raise ValidationError(f"未知优先级：{text}；可选 {'、'.join(PRIORITIES)}")
            if text not in selected:
                selected.append(text)

    findings = exam_service.review_list(db, current_user.id, priorities=selected, limit=limit)
    groups: dict[str, list[ExamFindingOut]] = {priority: [] for priority in PRIORITIES}
    for finding in findings:
        groups.setdefault(finding.priority, []).append(_finding_out(finding))

    high = len(groups.get(PRIORITY_HIGH, []))
    return ReviewListResponse(
        total=len(findings),
        summary=_summary_out(db, current_user.id),
        groups={key: value for key, value in groups.items() if value},
        items=[_finding_out(f) for f in findings],
        note=(
            f"高危 {high} 条建议优先复习"
            if high
            else "暂无高危考点"
        ),
    )


@router.get("/summary", response_model=PrioritySummaryOut, summary="考点优先级统计")
@api_guard
def get_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PrioritySummaryOut:
    return _summary_out(db, current_user.id)


# --- 插入复习队列 -----------------------------------------------------------


@router.post(
    "/to-review-queue",
    response_model=QueueInsertResponse,
    summary="验收标准 3：高危考点插入复习队列",
)
@api_guard
def insert_to_queue(
    payload: QueueInsertRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> QueueInsertResponse:
    """把考点转成 FSRS 卡片并插入复习队列（重复调用幂等）。"""
    inserted, skipped = exam_service.insert_into_review_queue(
        db,
        current_user.id,
        exam_point_ids=payload.exam_point_ids,
        priorities=payload.priorities,
        limit=payload.limit,
    )
    card_ids = [card_id for finding in inserted for card_id in finding.card_ids]
    return QueueInsertResponse(
        inserted=len(inserted),
        skipped=skipped,
        card_ids=card_ids,
        items=[
            {
                "exam_point_id": finding.exam_point_id,
                "point_text": finding.point_text,
                "priority": finding.priority,
                "card_ids": finding.card_ids,
            }
            for finding in inserted
        ],
        note=_queue_note(len(inserted), skipped),
    )


# --- 查询 / 删除 ------------------------------------------------------------


@router.get("", response_model=ExamPointListResponse, summary="分页查询已导入考点")
@api_guard
def list_exam_points(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    priority: str | None = Query(None, description="高危 / 巩固 / 确认"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExamPointListResponse:
    if priority and priority not in PRIORITIES:
        raise ValidationError(f"未知优先级：{priority}；可选 {'、'.join(PRIORITIES)}")

    items, total = exam_service.list_exam_points(
        db, current_user.id, offset=offset, limit=limit, priority=priority
    )
    return ExamPointListResponse(
        total=total,
        offset=offset,
        limit=limit,
        items=[ExamPointOut.model_validate(item) for item in items],
    )


@router.get("/{exam_point_id}", response_model=ExamPointOut, summary="考点详情")
@api_guard
def get_exam_point(
    exam_point_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExamPointOut:
    record = exam_service.get_exam_point(db, current_user.id, exam_point_id)
    if record is None:
        raise NotFoundError(f"考点不存在：{exam_point_id}")
    return ExamPointOut.model_validate(record)


@router.delete(
    "/{exam_point_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除考点"
)
@api_guard
def delete_exam_point(
    exam_point_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    record = exam_service.get_exam_point(db, current_user.id, exam_point_id)
    if record is None:
        raise NotFoundError(f"考点不存在：{exam_point_id}")

    # 级联清理由该考点生成的复习卡片，否则会留下 front 带「【考点】」的孤儿卡片，
    # 学生会在复习队列里看到一条已经删掉的考点。
    removed_cards = exam_service.delete_exam_point(db, record)
    logger.info(
        "删除考点 id=%s，同时清理其生成的卡片 %d 张", exam_point_id, removed_cards
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
