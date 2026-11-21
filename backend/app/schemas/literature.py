"""文献工作台 Schema（阶段三）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- 文献 -------------------------------------------------------------------


class ArticleBrief(BaseModel):
    pmid: str
    title: str = ""
    journal: str = ""
    year: str = ""
    url: str = ""
    authors: list[str] = Field(default_factory=list)


class ArticleOut(ArticleBrief):
    abstract: str = ""
    doi: str = ""
    mesh_terms: list[str] = Field(default_factory=list)
    publication_types: list[str] = Field(default_factory=list)


class PubMedSearchResponse(BaseModel):
    query: str
    total: int
    retmax: int
    retstart: int
    items: list[ArticleBrief]


# --- PICO-D -----------------------------------------------------------------


class PicoDFieldOut(BaseModel):
    value: str = ""
    confidence: float = 0.0
    evidence: str = ""


class PicoDOut(BaseModel):
    population: PicoDFieldOut = Field(default_factory=PicoDFieldOut)
    intervention: PicoDFieldOut = Field(default_factory=PicoDFieldOut)
    comparator: PicoDFieldOut = Field(default_factory=PicoDFieldOut)
    outcome: PicoDFieldOut = Field(default_factory=PicoDFieldOut)
    design: PicoDFieldOut = Field(default_factory=PicoDFieldOut)
    source: str = "heuristic"
    llm_error: str | None = None
    filled_count: int = 0


# --- 三元组 / 知识点 --------------------------------------------------------


class TripleIn(BaseModel):
    # 与 knowledge_graph 的列宽（VARCHAR(200)）对齐，避免超长直接 500
    entity: str = Field(min_length=1, max_length=200)
    relation: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)
    confidence: float = 0.0
    evidence: str = ""

    @field_validator("entity", "relation", "target")
    @classmethod
    def _clean(cls, value: str) -> str:
        text = " ".join(value.split()).strip()
        if not text:
            raise ValueError("三元组元素不能为空")
        return text


class TripleOut(TripleIn):
    source: str = "rule"


class KnowledgePointSuggestionOut(BaseModel):
    knowledge_point: str
    front: str
    back: str
    confidence: float = 0.0


# --- 抽取草稿（人工校对对象） -----------------------------------------------


class ExtractRequest(BaseModel):
    pmid: str = Field(min_length=1, description="PubMed ID")

    @field_validator("pmid")
    @classmethod
    def _clean_pmid(cls, value: str) -> str:
        text = value.strip()
        if not text.isdigit():
            raise ValueError("PMID 必须是纯数字")
        return text


class DraftOut(BaseModel):
    """待校对草稿。**尚未写入 MySQL**。"""

    draft_id: str
    user_id: int
    pmid: str
    status: str
    extraction_source: str
    fallback_reason: str | None = None
    note: str = ""
    created_at: str = ""
    updated_at: str = ""
    article: dict[str, Any] = Field(default_factory=dict)
    pico_d: dict[str, Any] = Field(default_factory=dict)
    triples: list[TripleOut] = Field(default_factory=list)
    knowledge_points: list[KnowledgePointSuggestionOut] = Field(default_factory=list)


class DraftListResponse(BaseModel):
    total: int
    items: list[DraftOut]


class DraftUpdateRequest(BaseModel):
    """人工校对：可直接改 PICO-D 与三元组。"""

    pico_d: dict[str, Any] | None = Field(default=None, description="修正后的 PICO-D")
    triples: list[TripleIn] | None = Field(default=None, description="修正后的三元组")
    knowledge_points: list[KnowledgePointSuggestionOut] | None = None
    note: str | None = None


class ConfirmRequest(BaseModel):
    """确认入库。可传入人工修正后的三元组（覆盖草稿）。"""

    triples: list[TripleIn] | None = None
    source_type: str = Field(default="pubmed", max_length=32)
    source_id: str | None = Field(
        default=None, max_length=64, description="默认使用 PMID（列宽 64）"
    )


class ConfirmResponse(BaseModel):
    draft_id: str
    status: str
    added: int
    skipped: int
    total_triples: int
    source_type: str
    source_id: str
    graph_stats: dict[str, Any] = Field(default_factory=dict)


class RejectRequest(BaseModel):
    reason: str = ""


# --- 知识点转卡片 -----------------------------------------------------------


class ToCardsRequest(BaseModel):
    """把草稿中的知识点一键转为复习卡片。

    ``knowledge_points`` 为空时，转换草稿中全部知识点建议。
    """

    knowledge_points: list[str] | None = Field(default=None, description="指定要转换的知识点")
    tags: dict[str, list[str]] | None = Field(default=None, description="附加三维度标签")
    include_answer_in_back: bool = Field(default=True, description="背面是否附上文献依据")


class ToCardsResponse(BaseModel):
    created: int
    skipped: int
    card_ids: list[int] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)


# --- 知识图谱 ---------------------------------------------------------------


class GraphNode(BaseModel):
    id: str
    label: str
    degree: int = 0
    in_degree: int = 0
    out_degree: int = 0
    sources: list[str] = Field(default_factory=list)


class GraphEdge(BaseModel):
    source: str
    target: str
    label: str = ""
    relations: list[str] = Field(default_factory=list)
    weight: int = 1
    source_type: str = ""
    source_id: str = ""


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: dict[str, Any] = Field(default_factory=dict)


class TripleRecordOut(BaseModel):
    """已入库（人工确认后）的三元组。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    source_type: str
    source_id: str
    entity: str
    relation: str
    target: str
