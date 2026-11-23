"""文献工作台路由 /api/v1/literature（阶段三）。

人工校对闭环
------------
1. ``POST /literature/extract``：按 PMID 抓取文献并抽取 PICO-D + 三元组，
   结果**只写入 ChromaDB 草稿**，MySQL 中不会有任何记录；
2. ``PATCH /literature/reviews/{draft_id}``：人工修改 PICO-D / 三元组 / 知识点；
3. ``POST /literature/reviews/{draft_id}/confirm``：确认后才写入 ``knowledge_graph``；
4. ``POST /literature/reviews/{draft_id}/to-cards``：知识点一键转复习卡片。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...database import get_db
from ...deps import api_guard, get_current_user
from ...errors import APIError, NotFoundError, ValidationError
from ...logging_config import get_logger
from ...models import Card, KnowledgeGraph, User
from ...schemas.literature import (
    ArticleBrief,
    ArticleOut,
    ConfirmRequest,
    ConfirmResponse,
    DraftListResponse,
    DraftOut,
    DraftUpdateRequest,
    ExtractRequest,
    GraphEdge,
    GraphNode,
    GraphResponse,
    KnowledgePointSuggestionOut,
    PicoDFieldOut,
    PicoDOut,
    PubMedSearchResponse,
    RejectRequest,
    ToCardsRequest,
    ToCardsResponse,
    TripleOut,
    TripleRecordOut,
)
from ...services import card_service, kg_service, literature_service as lit
from ...services.kg_service import (
    DRAFT_INDEXED,
    DRAFT_INDEXING,
    DRAFT_PENDING,
    DRAFT_REJECTED,
    Triple,
)
from ...tags import MAX_TAG_LENGTH

logger = get_logger(__name__)
router = APIRouter(prefix="/literature", tags=["文献工作台"])

# 简单的进程内 PubMed 客户端（无状态）
_pubmed = lit.PubMedClient()


# --- 辅助 -------------------------------------------------------------------


def _pico_d_out(payload: dict[str, Any]) -> PicoDOut:
    raw = payload.get("pico_d") or {}
    fields = {}
    filled = 0
    for key in ("population", "intervention", "comparator", "outcome", "design"):
        item = raw.get(key) or {}
        field = PicoDFieldOut(
            value=str(item.get("value", "") or ""),
            confidence=float(item.get("confidence", 0.0) or 0.0),
            evidence=str(item.get("evidence", "") or ""),
        )
        if field.value.strip():
            filled += 1
        fields[key] = field

    return PicoDOut(
        **fields,
        source=str(raw.get("source", "heuristic")),
        llm_error=raw.get("llm_error"),
        filled_count=filled,
    )


def _triples_out(payload: dict[str, Any]) -> list[TripleOut]:
    out: list[TripleOut] = []
    for item in payload.get("triples") or []:
        try:
            out.append(
                TripleOut(
                    entity=str(item.get("entity", "")),
                    relation=str(item.get("relation", "")),
                    target=str(item.get("target", "")),
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                    evidence=str(item.get("evidence", "") or ""),
                    source=str(item.get("source", "rule")),
                )
            )
        except Exception:  # 跳过损坏的草稿条目，但要留痕
            logger.warning("草稿中的三元组条目损坏，已跳过：%r", item)
            continue
    return out


def _knowledge_points_out(payload: dict[str, Any]) -> list[KnowledgePointSuggestionOut]:
    out: list[KnowledgePointSuggestionOut] = []
    for item in payload.get("knowledge_points") or []:
        try:
            out.append(
                KnowledgePointSuggestionOut(
                    knowledge_point=str(item.get("knowledge_point", "")),
                    front=str(item.get("front", "")),
                    back=str(item.get("back", "")),
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                )
            )
        except Exception:
            logger.warning("草稿中的知识点条目损坏，已跳过：%r", item)
            continue
    return out


def _draft_out(draft: kg_service.Draft) -> DraftOut:
    payload = draft.payload or {}
    return DraftOut(
        draft_id=draft.draft_id,
        user_id=draft.user_id,
        pmid=draft.pmid,
        status=draft.status,
        extraction_source=draft.extraction_source,
        fallback_reason=payload.get("fallback_reason"),
        note=draft.note,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        article=payload.get("article") or {},
        pico_d=(payload.get("pico_d") or {}),
        triples=_triples_out(payload),
        knowledge_points=_knowledge_points_out(payload),
    )


def _load_draft(draft_id: str, user: User) -> kg_service.Draft:
    draft = kg_service.get_draft(draft_id, user.id)
    if draft is None:
        raise NotFoundError(f"草稿不存在或不属于当前用户：{draft_id}")
    return draft


def _article_to_dict(article: lit.Article) -> dict[str, Any]:
    return article.to_dict()


# --- PubMed 检索 ------------------------------------------------------------


@router.get("/search", response_model=PubMedSearchResponse, summary="PubMed 检索")
@api_guard
def search_pubmed(
    q: str = Query(..., min_length=1, description="检索式，支持 PubMed 语法"),
    retmax: int = Query(10, ge=1, le=50),
    retstart: int = Query(0, ge=0),
    sort: str = Query("relevance", description="relevance 或 pub_date"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PubMedSearchResponse:
    """检索 PubMed，并返回文献摘要级元数据。"""
    try:
        pmids, total = _pubmed.search(q, retmax=retmax, retstart=retstart, sort=sort)
        articles = _pubmed.fetch_many(pmids)
    except lit.PubMedError as exc:
        raise APIError(str(exc), status_code=502, error="pubmed_unavailable") from exc

    return PubMedSearchResponse(
        query=q,
        total=total,
        retmax=retmax,
        retstart=retstart,
        items=[
            ArticleBrief(
                pmid=article.pmid,
                title=article.title,
                journal=article.journal,
                year=article.year,
                url=article.url,
                authors=article.authors[:8],
            )
            for article in articles
        ],
    )


@router.get(
    "/pubmed/{pmid}",
    response_model=ArticleOut,
    summary="按 PMID 获取文献详情",
)
@api_guard
def get_article(
    pmid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ArticleOut:
    """抓取单篇文献的元数据与摘要，并写入向量库。"""
    try:
        article = _pubmed.fetch(pmid)
    except lit.PubMedError as exc:
        raise APIError(str(exc), status_code=502, error="pubmed_unavailable") from exc

    kg_service.index_article(article)
    return ArticleOut(**_article_to_dict(article))


# --- 抽取（生成草稿，不入库） -----------------------------------------------


@router.post(
    "/extract",
    response_model=DraftOut,
    summary="按 PMID 抽取 PICO-D 与三元组（生成待校对草稿，不落库）",
)
@api_guard
def extract_literature(
    payload: ExtractRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DraftOut:
    """验收标准 1：输入一个 PubMed ID，返回 PICO-D 结构化摘要。

    同时抽取三元组与知识点建议。**结果只进 ChromaDB 草稿，不进 MySQL。**
    """
    try:
        article = _pubmed.fetch(payload.pmid)
    except lit.PubMedError as exc:
        raise APIError(str(exc), status_code=502, error="pubmed_unavailable") from exc

    kg_service.index_article(article)

    pico = lit.extract_pico_d(article)
    triples, triple_source, fallback_reason = kg_service.extract_triples(article, pico)
    knowledge_points = kg_service.suggest_knowledge_points(pico, triples, article)

    draft_payload: dict[str, Any] = {
        "article": _article_to_dict(article),
        "pico_d": pico.to_dict(),
        "triples": [triple.to_dict() for triple in triples],
        "knowledge_points": knowledge_points,
        "fallback_reason": fallback_reason,
        "triple_source": triple_source,
    }
    draft = kg_service.save_draft(
        current_user.id, article.pmid, draft_payload, extraction_source=pico.source
    )
    return _draft_out(draft)


# --- 人工校对 ---------------------------------------------------------------


@router.get("/reviews", response_model=DraftListResponse, summary="待校对草稿列表")
@api_guard
def list_reviews(
    status: str = Query(
        DRAFT_PENDING,
        description="pending / indexing / indexed / rejected；all 表示全部",
    ),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DraftListResponse:
    """列出抽取草稿（默认只看待校对）。"""
    filter_status = None if status in ("all", "") else status
    if filter_status is not None and filter_status not in kg_service.DRAFT_STATUSES:
        raise ValidationError(f"非法状态：{status}")

    drafts = kg_service.list_drafts(current_user.id, status=filter_status, limit=limit)
    return DraftListResponse(total=len(drafts), items=[_draft_out(d) for d in drafts])


@router.get("/reviews/{draft_id}", response_model=DraftOut, summary="草稿详情")
@api_guard
def get_review(
    draft_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DraftOut:
    return _draft_out(_load_draft(draft_id, current_user))


@router.patch("/reviews/{draft_id}", response_model=DraftOut, summary="人工校对草稿")
@api_guard
def update_review(
    draft_id: str,
    payload: DraftUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DraftOut:
    """人工修改 AI 抽取结果（PICO-D / 三元组 / 知识点）。

    这一步是「人工校对」的核心：AI 结果必须先被人工看过或改过，才可能入库。
    """
    draft = _load_draft(draft_id, current_user)
    body = dict(draft.payload or {})

    if payload.pico_d is not None:
        body["pico_d"] = payload.pico_d
    if payload.triples is not None:
        body["triples"] = [t.model_dump() for t in payload.triples]
    if payload.knowledge_points is not None:
        body["knowledge_points"] = [k.model_dump() for k in payload.knowledge_points]

    updated = kg_service.update_draft(
        draft_id, current_user.id, payload=body, note=payload.note
    )
    if updated is None:  # pragma: no cover - 并发删除
        raise NotFoundError(f"草稿不存在：{draft_id}")
    return _draft_out(updated)


@router.post(
    "/reviews/{draft_id}/confirm",
    response_model=ConfirmResponse,
    summary="确认入库（写入 knowledge_graph）",
)
@api_guard
def confirm_review(
    draft_id: str,
    payload: ConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConfirmResponse:
    """验收标准 2：结构化结果需人工确认后才写入数据库。"""
    draft = _load_draft(draft_id, current_user)
    body = dict(draft.payload or {})

    # 人工可在确认时直接给出修正后的三元组
    triples: list[Triple | dict[str, Any]]
    if payload.triples is not None:
        triples = [t.model_dump() for t in payload.triples]
        body["triples"] = triples
        kg_service.update_draft(draft_id, current_user.id, payload=body)
    else:
        triples = body.get("triples") or []

    if not triples:
        raise ValidationError("没有可入库的三元组：请先抽取或人工添加至少一条关系")

    source_id = payload.source_id or draft.pmid

    # 状态机：先置 indexing 再写库。
    # 崩溃点只可能落在 indexing 上，重启后扫描 indexing 重放即可（写库幂等），
    # 不会出现「MySQL 已写、草稿仍 pending」导致重试判断不了的情况。
    kg_service.set_draft_status(draft_id, current_user.id, DRAFT_INDEXING)
    try:
        created, skipped = kg_service.confirm_triples(
            db,
            current_user.id,
            triples,
            source_type=payload.source_type,
            source_id=source_id,
        )
    except Exception:
        # 保持 indexing，等待启动恢复流程重试
        logger.exception("确认入库失败，草稿保持在 indexing 以便重试 draft_id=%s", draft_id)
        raise

    kg_service.set_draft_status(draft_id, current_user.id, DRAFT_INDEXED)

    graph = kg_service.build_graph(kg_service.fetch_triples(db, current_user.id))
    return ConfirmResponse(
        draft_id=draft_id,
        status=DRAFT_INDEXED,
        added=len(created),
        skipped=skipped,
        total_triples=len(triples),
        source_type=payload.source_type,
        source_id=str(source_id),
        graph_stats=kg_service.graph_stats(graph),
    )


@router.post("/reviews/{draft_id}/reject", response_model=DraftOut, summary="拒绝草稿")
@api_guard
def reject_review(
    draft_id: str,
    payload: RejectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DraftOut:
    """人工判定抽取不可用：草稿标记为 rejected，不会有任何数据入库。"""
    _load_draft(draft_id, current_user)
    updated = kg_service.update_draft(draft_id, current_user.id, note=payload.reason)
    if updated is None:  # pragma: no cover
        raise NotFoundError(f"草稿不存在：{draft_id}")
    rejected = kg_service.set_draft_status(draft_id, current_user.id, DRAFT_REJECTED)
    if rejected is None:  # pragma: no cover
        raise NotFoundError(f"草稿不存在：{draft_id}")
    return _draft_out(rejected)


# --- 知识点一键转卡片 -------------------------------------------------------


@router.post(
    "/reviews/{draft_id}/to-cards",
    response_model=ToCardsResponse,
    summary="验收标准 3：知识点一键转为复习卡片",
)
@api_guard
def to_cards(
    draft_id: str,
    payload: ToCardsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ToCardsResponse:
    """把（已人工确认的）知识点转成 FSRS 复习卡片。"""
    draft = _load_draft(draft_id, current_user)
    body = dict(draft.payload or {})
    suggestions = _knowledge_points_out(body)
    if not suggestions:
        raise ValidationError("该草稿没有可转换的知识点")

    wanted = None
    if payload.knowledge_points:
        wanted = {k.strip() for k in payload.knowledge_points if k.strip()}

    card_ids: list[int] = []
    items: list[dict[str, Any]] = []
    # skipped 分两类：请求了但草稿里没有该知识点 / 对应卡片已存在（幂等跳过）
    available_points = {suggestion.knowledge_point for suggestion in suggestions}
    skipped = len(wanted - available_points) if wanted else 0
    existing_fronts = {
        str(front)
        for front in db.execute(
            select(Card.front).where(Card.user_id == current_user.id)
        ).scalars()
    }

    for suggestion in suggestions:
        if wanted is not None and suggestion.knowledge_point not in wanted:
            continue

        # 死分支清理：两种写法都是 suggestion.back，include_answer_in_back 从未生效。
        back = suggestion.back
        tags = payload.tags or {}
        # 知识点本身写入章节维度，便于后续按知识点复习与薄弱点统计
        # 知识点写入章节标签；超长时截断到标签上限，避免整份草稿因标签校验失败
        chapter = suggestion.knowledge_point[:MAX_TAG_LENGTH]
        merged_tags = {
            "subjects": list(tags.get("subjects", [])),
            "chapters": list(tags.get("chapters", [])) + [chapter],
            "point_types": list(tags.get("point_types", [])),
        }

        front = suggestion.front or suggestion.knowledge_point
        # 幂等：同一份草稿重复点击「转卡片」不应重复建卡
        if front in existing_fronts:
            skipped += 1
            continue
        card = card_service.create_card(
            db,
            current_user.id,
            front=front,
            back=back or "见文献原文",
            tags=merged_tags,
        )
        existing_fronts.add(front)
        card_ids.append(card.id)
        items.append({"card_id": card.id, "front": card.front, "knowledge_point": suggestion.knowledge_point})

    logger.info("知识点转卡片 draft_id=%s 生成 %d 张", draft_id, len(card_ids))
    return ToCardsResponse(created=len(card_ids), skipped=skipped, card_ids=card_ids, items=items)


# --- 知识图谱 ---------------------------------------------------------------


@router.get("/graph", response_model=GraphResponse, summary="知识图谱（已确认数据）")
@api_guard
def get_graph(
    source_type: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> GraphResponse:
    """用 NetworkX 构建并返回知识图谱。

    数据只来自人工确认后写入 ``knowledge_graph`` 的三元组。
    """
    records = kg_service.fetch_triples(db, current_user.id, source_type=source_type)
    graph = kg_service.build_graph(records)
    data = kg_service.graph_to_json(graph)
    return GraphResponse(
        nodes=[GraphNode(**node) for node in data["nodes"]],
        edges=[GraphEdge(**edge) for edge in data["edges"]],
        stats=kg_service.graph_stats(graph),
    )


@router.get("/graph/export", summary="导出知识图谱（json / graphml / gexf）")
@api_guard
def export_graph(
    fmt: str = Query("json", description="json / graphml / gexf"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """导出图谱，可导入 Gephi / Cytoscape 等工具。"""
    if fmt.lower() not in {"json", "graphml", "gexf"}:
        raise ValidationError("fmt 仅支持 json / graphml / gexf")

    records = kg_service.fetch_triples(db, current_user.id)
    graph = kg_service.build_graph(records)
    content, media_type = kg_service.export_graph(graph, fmt)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="knowledge_graph.{fmt.lower()}"'},
    )


@router.get(
    "/graph/triples",
    response_model=list[TripleRecordOut],
    summary="已确认的三元组列表",
)
@api_guard
def list_triples(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    source_type: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[KnowledgeGraph]:
    records = kg_service.fetch_triples(db, current_user.id, source_type=source_type)
    return records[offset : offset + limit]


@router.delete("/graph/triples/{record_id}", status_code=204, summary="删除一条已确认的三元组")
@api_guard
def delete_triple(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    record = db.get(KnowledgeGraph, record_id)
    if record is None or record.user_id != current_user.id:
        raise NotFoundError(f"三元组不存在：{record_id}")
    db.delete(record)
    db.flush()
    return Response(status_code=204)


# --- 向量检索 ---------------------------------------------------------------


@router.get("/vector-search", summary="在已入库文献中做向量检索（ChromaDB）")
@api_guard
def vector_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """用本地确定性嵌入在 ChromaDB 中检索相似文献。"""
    return {"query": q, "items": kg_service.search_literature(q, limit=limit)}
