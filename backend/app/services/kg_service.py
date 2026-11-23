"""知识图谱服务（阶段三）。

组成
----
1. **离线确定性嵌入**：字符/词 n-gram 哈希，用 ``hashlib`` 保证跨进程稳定
   （Python 内置 ``hash()`` 每进程随机化，若用于持久化向量会导致检索结果漂移）。
2. **ChromaDB**：``literature`` 集合存文献向量；``literature_drafts`` 集合存
   **待人工校对的抽取草稿** —— 这是「AI 提取结果不能自动入库」的落点，
   草稿停留在向量库中，只有确认后才写入 MySQL 的 ``knowledge_graph``。
3. **三元组抽取**：优先 LangChain ``LLMGraphTransformer``；未配置 LLM 或调用失败时
   退化到基于 PICO-D 的规则抽取。
4. **NetworkX**：构建有向图、统计与可视化导出。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import networkx as nx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..embeddings import HASHING_BACKEND_ID, SPACE_LITERATURE
from ..logging_config import get_logger
from ..models import KnowledgeGraph
from ..models.knowledge_graph import triple_fingerprint
from ..timeutils import utcnow
from .literature_service import Article, PicoD

logger = get_logger(__name__)

# 草稿状态机：
#   pending ──人工确认──> indexing ──写库成功──> indexed
#      └────人工拒绝────> rejected
#
# 为什么需要中间的 indexing：Chroma（草稿）与 MySQL（正式三元组）不在同一个
# 事务里。若直接「写完库再改状态」，进程在两者之间崩溃就会留下
# 「MySQL 已写入、草稿仍是 pending」的不一致状态，重试时无法判断该不该重放。
# 先置 indexing 再写库，崩溃后扫描 indexing 重放即可（写库本身是幂等的）。
DRAFT_PENDING = "pending"
DRAFT_INDEXING = "indexing"
DRAFT_INDEXED = "indexed"
DRAFT_REJECTED = "rejected"
DRAFT_STATUSES = (DRAFT_PENDING, DRAFT_INDEXING, DRAFT_INDEXED, DRAFT_REJECTED)

# 历史草稿里存的是 "confirmed"（引入状态机之前）。读取时映射，写入时不再产生。
LEGACY_DRAFT_STATUSES = {"confirmed": DRAFT_INDEXED}


def normalize_draft_status(status: str) -> str:
    """把历史状态名映射为当前状态机的值。"""
    return LEGACY_DRAFT_STATUSES.get(status, status)

# --- 关系词表（同时作为 LLMGraphTransformer 的 allowed_nodes/relationships）--

RELATION_TREATS = "治疗"
RELATION_INTERVENES = "干预"
RELATION_COMPARED_WITH = "对照比较"
RELATION_REPORTS_OUTCOME = "报告结局"
RELATION_STUDIED_IN = "研究对象"
RELATION_USES_DESIGN = "研究设计"
RELATION_HAS_MECHANISM = "作用机制"

ALLOWED_RELATIONS: tuple[str, ...] = (
    RELATION_TREATS,
    RELATION_INTERVENES,
    RELATION_COMPARED_WITH,
    RELATION_REPORTS_OUTCOME,
    RELATION_STUDIED_IN,
    RELATION_USES_DESIGN,
    RELATION_HAS_MECHANISM,
)


# --- 嵌入函数 ---------------------------------------------------------------


def _hash_to_index(token: str, dim: int) -> int:
    """稳定的字符串 -> 桶索引（跨进程一致）。"""
    # 这是「字符串 -> 桶索引」的确定性散列，不用于任何安全用途；
    # 显式标注 usedforsecurity=False 以免被静态扫描误判为弱加密。
    digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest[:8], 16) % dim


def embed_text(text: str, dim: int | None = None) -> list[float]:
    """离线确定性嵌入：字符 n-gram + 词元哈希，L2 归一化。

    对中文按字符 n-gram（2/3 元）取特征，对英文按词取特征，
    因此中英文混排的摘要都能得到可比较的向量。
    """
    size = dim or settings.embedding_dim
    vector = [0.0] * size
    raw = (text or "").strip()
    if not raw:
        return vector

    lowered = raw.lower()
    # 字符 n-gram（对中文有效）
    for n in (2, 3):
        for i in range(max(0, len(lowered) - n + 1)):
            vector[_hash_to_index(lowered[i : i + n], size)] += 1.0

    # 词元（对英文有效）
    for word in re.findall(r"[a-z0-9]+", lowered):
        vector[_hash_to_index(f"w:{word}", size)] += 1.0

    norm = sum(value * value for value in vector) ** 0.5
    if norm:
        vector = [value / norm for value in vector]
    return vector


def _build_embedding_function(dim: int | None = None):
    """构造 ChromaDB 嵌入函数（延迟导入 chromadb，保持应用启动轻量）。"""
    import chromadb
    from chromadb.utils.embedding_functions import register_embedding_function

    size = dim or settings.embedding_dim

    # 必须注册：Chroma 会把嵌入函数的「名字」持久化到集合配置里，
    # 重新打开集合时要按名字解析回来。不注册的话，一旦配置被重新加载
    # （modify/重启/新客户端）就会抛
    # "Embedding function pharmacy-offline-hashing not found"。
    @register_embedding_function
    class OfflineHashingEmbedding(chromadb.EmbeddingFunction):
        """不下载模型、不联网的确定性嵌入。"""

        def __init__(self, dimension: int = size) -> None:
            self.dimension = dimension

        def __call__(self, input):  # noqa: A002 - ChromaDB 约定的参数名
            return [embed_text(text, self.dimension) for text in input]

        # Chroma 1.x 持久化需要这些钩子来重建嵌入函数
        @staticmethod
        def name() -> str:
            return "pharmacy-offline-hashing"

        def get_config(self) -> dict[str, Any]:
            return {"dimension": self.dimension}

        @staticmethod
        def build_from_config(config: dict[str, Any]) -> "OfflineHashingEmbedding":
            return OfflineHashingEmbedding(config.get("dimension", size))

        def default_space(self) -> str:
            return "cosine"

    return OfflineHashingEmbedding(size)


# --- ChromaDB 客户端（惰性单例） --------------------------------------------

_client: Any = None
# 初始化锁：并发首次请求不能各建一个 PersistentClient（同一路径重复实例化会报错）
_client_lock = threading.Lock()


def get_chroma_client():
    """持久化 ChromaDB 客户端（惰性单例，加锁）。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import chromadb

                settings.chroma_dir.mkdir(parents=True, exist_ok=True)
                _client = chromadb.PersistentClient(path=str(settings.chroma_dir))
                logger.info("ChromaDB 已就绪：%s", settings.chroma_dir)
    return _client


def reset_chroma_client() -> None:
    """释放缓存（测试用）。"""
    global _client
    _client = None


def _embedding_signature() -> dict[str, Any]:
    """当前向量集合使用的嵌入器标识。

    文献向量库（本模块，离线哈希嵌入，256 维）与考点匹配
    （sentence-transformers，384 维）用的是**两套不同的向量空间**。
    一旦有人把其中一处换成另一套，已有集合的维度会对不上，Chroma 会直接报错
    或静默混用不同语义的向量。这里把标识写进集合元数据，切换时显式重建。
    """
    return {
        "embedding_backend": HASHING_BACKEND_ID,
        "embedding_dim": settings.embedding_dim,
        # 空间标识：文献向量与考点匹配向量不可互相比较（见 app/embeddings.py）
        "embedding_space": SPACE_LITERATURE,
    }


SIGNATURE_KEEP = "keep"
SIGNATURE_ADOPT = "adopt"
SIGNATURE_REBUILD = "rebuild"


def _signature_action(stored: dict[str, Any], signature: dict[str, Any]) -> str:
    """决定集合该如何处理嵌入签名（纯函数，便于测试）。

    - ``keep``：签名一致，直接用；
    - ``adopt``：历史集合没有签名（说明一直是同一套嵌入）→ 补写签名，**保留数据**；
    - ``rebuild``：签名明确不一致 → 向量语义已不同，必须重建。
    """
    stored_backend = stored.get("embedding_backend")
    stored_dim = stored.get("embedding_dim")

    if stored_backend is None and stored_dim is None:
        return SIGNATURE_ADOPT
    if (
        stored_backend != signature["embedding_backend"]
        or stored_dim != signature["embedding_dim"]
    ):
        return SIGNATURE_REBUILD
    return SIGNATURE_KEEP


def _collection(name: str):
    """取得集合；若嵌入签名与集合中记录的不一致则重建。

    向量是**派生数据**（可由原文重新计算），因此签名变化时安全重建，
    而不是继续往语义不一致的集合里写入。
    """
    client = get_chroma_client()
    signature = _embedding_signature()

    # 必须**无条件**构造一次嵌入函数：它的副作用是向 Chroma 注册，
    # 否则重新打开已存在的集合时按名字解析会失败
    # （"Embedding function pharmacy-offline-hashing not found"）。
    # 同时显式传入，避免依赖持久化配置里的名字解析。
    embedding_function = _build_embedding_function()

    existing = None
    try:
        existing = client.get_collection(name=name, embedding_function=embedding_function)
    except Exception:  # 集合不存在
        existing = None

    if existing is not None:
        stored = dict(existing.metadata or {})
        stored_backend = stored.get("embedding_backend")
        stored_dim = stored.get("embedding_dim")
        action = _signature_action(stored, signature)

        if action == SIGNATURE_ADOPT:
            # 历史集合（引入签名之前创建）：一直就是这套离线哈希嵌入，
            # 只补写签名，**不删数据**，避免升级时白白丢失已索引的文献。
            # hnsw:* 是创建期参数，modify() 不接受修改；只补写业务元数据
            business = {
                key: value for key, value in stored.items() if not key.startswith("hnsw:")
            }
            try:
                existing.modify(metadata={**business, **signature})
                logger.info("集合 %s 补写嵌入签名（保留原有 %d 条向量）", name, existing.count())
            except Exception:
                logger.warning("集合 %s 补写签名失败，忽略（不影响检索）", name)
        elif action == SIGNATURE_REBUILD:
            logger.warning(
                "集合 %s 的嵌入签名已变化（%s/%s → %s/%s），将重建（向量可重算）",
                name,
                stored_backend,
                stored_dim,
                signature["embedding_backend"],
                signature["embedding_dim"],
            )
            client.delete_collection(name)
            existing = None

    if existing is not None:
        return existing

    return client.get_or_create_collection(
        name=name,
        embedding_function=embedding_function,
        metadata={**signature, "hnsw:space": "cosine"},
    )


def literature_collection():
    return _collection(settings.chroma_collection_literature)


def draft_collection():
    return _collection(settings.chroma_collection_drafts)


# --- 文献向量存储 -----------------------------------------------------------


def index_article(article: Article) -> None:
    """把文献存入 ChromaDB（摘要向量化，便于后续检索）。"""
    document = f"{article.title}\n\n{article.abstract}".strip()
    if not document:
        return
    literature_collection().upsert(
        ids=[f"pubmed:{article.pmid}"],
        documents=[document],
        metadatas=[
            {
                "pmid": article.pmid,
                "title": article.title[:500],
                "journal": article.journal[:200],
                "year": article.year,
                "url": article.url,
            }
        ],
    )
    logger.info("文献已向量化入库 pmid=%s", article.pmid)


def search_literature(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """在已入库文献中做向量检索。"""
    if not query.strip():
        return []
    result = literature_collection().query(query_texts=[query], n_results=limit)
    out: list[dict[str, Any]] = []
    ids = (result.get("ids") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    for index, doc_id in enumerate(ids):
        out.append(
            {
                "id": doc_id,
                "document": documents[index] if index < len(documents) else "",
                "metadata": metadatas[index] if index < len(metadatas) else {},
                "distance": distances[index] if index < len(distances) else None,
            }
        )
    return out


# --- 待校对草稿（ChromaDB） -------------------------------------------------


@dataclass(slots=True)
class Draft:
    """一份待人工校对的文献抽取草稿。"""

    draft_id: str
    user_id: int
    pmid: str
    status: str
    payload: dict[str, Any]
    created_at: str
    updated_at: str
    extraction_source: str = "heuristic"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _draft_document(payload: dict[str, Any]) -> str:
    article = payload.get("article") or {}
    return f"{article.get('title', '')}\n\n{article.get('abstract', '')}".strip() or "(空文献)"


def save_draft(
    user_id: int,
    pmid: str,
    payload: dict[str, Any],
    extraction_source: str = "heuristic",
) -> Draft:
    """保存草稿（**不写入 MySQL**）。"""
    now = utcnow().isoformat()
    draft_id = uuid.uuid4().hex
    draft = Draft(
        draft_id=draft_id,
        user_id=user_id,
        pmid=str(pmid),
        status=DRAFT_PENDING,
        payload=payload,
        created_at=now,
        updated_at=now,
        extraction_source=extraction_source,
    )
    draft_collection().add(
        ids=[draft_id],
        documents=[_draft_document(payload)],
        metadatas=[_draft_metadata(draft)],
    )
    logger.info(
        "保存待校对草稿 draft_id=%s user_id=%s pmid=%s 来源=%s",
        draft_id,
        user_id,
        pmid,
        extraction_source,
    )
    return draft


def _draft_metadata(draft: Draft, status: str | None = None) -> dict[str, Any]:
    """Chroma 元数据只接受标量，复杂结构序列化为 JSON 字符串。"""
    return {
        "user_id": int(draft.user_id),
        "pmid": str(draft.pmid),
        "status": status or draft.status,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
        "extraction_source": draft.extraction_source,
        "note": draft.note,
        "payload": json.dumps(draft.payload, ensure_ascii=False),
    }


def _draft_from_record(record_id: str, metadata: dict[str, Any]) -> Draft:
    raw_payload = metadata.get("payload") or "{}"
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        payload = {}
    return Draft(
        draft_id=record_id,
        user_id=int(metadata.get("user_id", 0)),
        pmid=str(metadata.get("pmid", "")),
        status=normalize_draft_status(str(metadata.get("status", DRAFT_PENDING))),
        payload=payload,
        created_at=str(metadata.get("created_at", "")),
        updated_at=str(metadata.get("updated_at", "")),
        extraction_source=str(metadata.get("extraction_source", "heuristic")),
        note=str(metadata.get("note", "")),
    )


def get_draft(draft_id: str, user_id: int) -> Draft | None:
    """按 id 取草稿（校验归属用户）。"""
    result = draft_collection().get(ids=[draft_id])
    ids = result.get("ids") or []
    if not ids:
        return None
    metadata = (result.get("metadatas") or [{}])[0] or {}
    draft = _draft_from_record(ids[0], metadata)
    if draft.user_id != int(user_id):
        logger.warning("草稿归属校验失败 draft_id=%s", draft_id)
        return None
    return draft


def list_drafts(
    user_id: int, status: str | None = DRAFT_PENDING, limit: int = 50
) -> list[Draft]:
    """列出某用户的草稿（默认只看待校对）。"""
    where: dict[str, Any] = {"user_id": {"$eq": int(user_id)}}
    if status:
        wanted = normalize_draft_status(status)
        # 历史草稿存的是旧状态名，过滤时要把它们一起算进来
        values = [wanted] + [
            legacy for legacy, current in LEGACY_DRAFT_STATUSES.items() if current == wanted
        ]
        clause = (
            {"status": {"$eq": values[0]}}
            if len(values) == 1
            else {"status": {"$in": values}}
        )
        where = {"$and": [where, clause]}

    result = draft_collection().get(where=where, limit=limit)
    ids = result.get("ids") or []
    metadatas = result.get("metadatas") or []
    drafts = [
        _draft_from_record(record_id, metadatas[index] or {})
        for index, record_id in enumerate(ids)
    ]
    drafts.sort(key=lambda d: d.updated_at, reverse=True)
    return drafts


def update_draft(
    draft_id: str,
    user_id: int,
    payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> Draft | None:
    """人工校对：覆盖草稿载荷（AI 结果只有经此步骤才可能进入正式库）。"""
    draft = get_draft(draft_id, user_id)
    if draft is None:
        return None

    if payload is not None:
        draft.payload = payload
    if note is not None:
        draft.note = note
    draft.updated_at = utcnow().isoformat()

    draft_collection().update(
        ids=[draft_id],
        documents=[_draft_document(draft.payload)],
        metadatas=[_draft_metadata(draft)],
    )
    logger.info("草稿已人工校对 draft_id=%s", draft_id)
    return draft


def set_draft_status(draft_id: str, user_id: int, status: str) -> Draft | None:
    """更新草稿状态（pending / indexing / indexed / rejected）。

    只接受状态机里的值；历史名 ``confirmed`` 不再写入（读取时才映射）。
    """
    if status not in DRAFT_STATUSES:
        raise ValueError(f"非法草稿状态：{status}")

    draft = get_draft(draft_id, user_id)
    if draft is None:
        return None
    draft.status = status
    draft.updated_at = utcnow().isoformat()
    draft_collection().update(
        ids=[draft_id],
        metadatas=[_draft_metadata(draft, status=status)],
    )
    logger.info("草稿状态更新 draft_id=%s status=%s", draft_id, status)
    return draft


def recover_indexing_drafts(db: Session, limit: int = 100) -> int:
    """扫描卡在 ``indexing`` 的草稿并重试写库，返回成功恢复的条数。

    崩溃点只可能在「已置 indexing、尚未写完 MySQL」或
    「MySQL 已写完、尚未置 indexed」之间，两种情况重放都安全：
    ``confirm_triples`` 靠应用层去重 + ``(user_id, fingerprint)`` 唯一约束幂等。
    """
    result = draft_collection().get(where={"status": {"$eq": DRAFT_INDEXING}}, limit=limit)
    ids = result.get("ids") or []
    metadatas = result.get("metadatas") or []
    recovered = 0
    for index, draft_id in enumerate(ids):
        metadata = metadatas[index] or {}
        user_id = int(metadata.get("user_id") or 0)
        if not user_id:
            continue
        draft = get_draft(draft_id, user_id)
        if draft is None or not draft.payload:
            continue
        triples = draft.payload.get("triples") or []
        if not triples:
            # 没有可重放的载荷：置回 pending，交回人工处理
            set_draft_status(draft_id, user_id, DRAFT_PENDING)
            logger.warning("草稿 %s 卡在 indexing 且无可重放三元组，已退回 pending", draft_id)
            continue
        try:
            confirm_triples(
                db,
                user_id,
                triples,
                source_type=draft.payload.get("source_type") or "pubmed",
                source_id=str(draft.pmid),
            )
        except Exception:  # pragma: no cover - 单条失败不影响其余
            logger.exception("重放草稿失败 draft_id=%s（保持 indexing 以便下次重试）", draft_id)
            continue
        set_draft_status(draft_id, user_id, DRAFT_INDEXED)
        recovered += 1
    if recovered:
        logger.warning("已恢复 %d 份卡在 indexing 的草稿", recovered)
    return recovered


def delete_draft(draft_id: str, user_id: int) -> bool:
    if get_draft(draft_id, user_id) is None:
        return False
    draft_collection().delete(ids=[draft_id])
    return True


# --- 三元组抽取 -------------------------------------------------------------


@dataclass(slots=True, frozen=False)
class Triple:
    """一条待校对/已确认的三元组。"""

    entity: str
    relation: str
    target: str
    confidence: float = 0.0
    source: str = "rule"
    evidence: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.entity.strip(), self.relation.strip(), self.target.strip())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_term(value: str, limit: int = 200) -> str:
    text = " ".join((value or "").split()).strip(" ，,、。:：;；")
    return text[:limit]


def extract_triples_rule_based(pico: PicoD, article: Article | None = None) -> list[Triple]:
    """基于 PICO-D 的规则抽取（无 Agent 降级模式，确定性可单测）。

    关系语义：
    - 干预 --干预--> 人群
    - 干预 --对照比较--> 对照
    - 干预 --报告结局--> 结局
    - 人群 --研究设计--> 设计
    """
    triples: list[Triple] = []

    population = normalize_term(pico.population.value)
    intervention = normalize_term(pico.intervention.value)
    comparator = normalize_term(pico.comparator.value)
    outcome = normalize_term(pico.outcome.value)
    design = normalize_term(pico.design.value)

    if intervention and population:
        triples.append(
            Triple(intervention, RELATION_INTERVENES, population, 0.5, "rule", population)
        )
    if intervention and comparator:
        triples.append(
            Triple(intervention, RELATION_COMPARED_WITH, comparator, 0.5, "rule", comparator)
        )
    if intervention and outcome:
        triples.append(
            Triple(intervention, RELATION_REPORTS_OUTCOME, outcome, 0.45, "rule", outcome)
        )
    if population and design:
        triples.append(
            Triple(population, RELATION_USES_DESIGN, design, 0.55, "rule", design)
        )
    if article and intervention and outcome:
        # 用标题作为补充证据，便于人工判断
        triples.append(
            Triple(
                intervention,
                RELATION_TREATS,
                outcome,
                0.35,
                "rule",
                article.title[:200],
            )
        )

    return dedupe_triples(triples)


def extract_triples_llm(text: str, article: Article | None = None) -> tuple[list[Triple], str | None]:
    """用 LangChain ``LLMGraphTransformer`` 抽取三元组。

    返回 ``(triples, 失败原因)``；失败原因非空表示需要退化到规则抽取。
    """
    # 延迟导入：未安装 langchain 时应用仍可正常启动并退化到规则抽取
    try:
        from ..langchain_adapter import build_graph_transformer
    except ImportError as exc:  # pragma: no cover - 依赖缺失
        return [], f"未安装 langchain，使用规则抽取（{exc}）"

    transformer, reason = build_graph_transformer(
        allowed_nodes=[], allowed_relationships=list(ALLOWED_RELATIONS)
    )
    if transformer is None:
        return [], reason

    try:
        from langchain_core.documents import Document

        documents = [Document(page_content=text)]
        graph_documents = transformer.convert_to_graph_documents(documents)
    except Exception as exc:  # 网络/配额/输出格式等各种失败
        logger.warning("LLMGraphTransformer 抽取失败：%s: %s", type(exc).__name__, exc)
        return [], f"LLMGraphTransformer 调用失败：{type(exc).__name__}"

    triples: list[Triple] = []
    for graph_document in graph_documents:
        for relationship in getattr(graph_document, "relationships", []) or []:
            entity = normalize_term(getattr(relationship, "source", "") or "")
            target = normalize_term(getattr(relationship, "target", "") or "")
            relation = normalize_term(getattr(relationship, "type", "") or "")
            if entity and relation and target:
                triples.append(
                    Triple(
                        entity,
                        relation,
                        target,
                        float(getattr(relationship, "confidence", 0.0) or 0.0) or 0.6,
                        "llm",
                    )
                )

    if not triples:
        return [], "LLMGraphTransformer 未产出任何关系"

    logger.info("LLMGraphTransformer 抽取 %d 条三元组", len(triples))
    return dedupe_triples(triples), None


def dedupe_triples(triples: Iterable[Triple]) -> list[Triple]:
    """按 (entity, relation, target) 去重，保留置信度最高的一条。"""
    best: dict[tuple[str, str, str], Triple] = {}
    for triple in triples:
        key = triple.key()
        if not all(key):
            continue
        current = best.get(key)
        if current is None or triple.confidence > current.confidence:
            best[key] = triple
    return sorted(best.values(), key=lambda t: (-t.confidence, t.entity, t.relation, t.target))


def extract_triples(
    article: Article, pico: PicoD
) -> tuple[list[Triple], str, str | None]:
    """抽取三元组：LLM 优先，失败/未配置时退化到规则抽取。

    返回 ``(triples, source, fallback_reason)``。
    """
    text = f"{article.title}\n\n{article.abstract}".strip()
    if text:
        triples, reason = extract_triples_llm(text, article)
        if triples:
            return triples[: settings.kg_max_triples], "llm", None
    else:
        reason = "文献正文为空"

    rule_triples = extract_triples_rule_based(pico, article)
    return rule_triples[: settings.kg_max_triples], "rule", reason


# --- 知识点建议（一键转卡片用） ---------------------------------------------


def suggest_knowledge_points(
    pico: PicoD, triples: Sequence[Triple], article: Article | None = None
) -> list[dict[str, Any]]:
    """从结构化结果生成知识点建议。

    每个知识点带一个可直接转成复习卡片的「正面问题 + 背面答案」。
    """
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(point: str, front: str, back: str, confidence: float) -> None:
        cleaned = normalize_term(point, 200)
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        suggestions.append(
            {
                "knowledge_point": cleaned,
                "front": front,
                "back": back,
                "confidence": confidence,
            }
        )

    intervention = normalize_term(pico.intervention.value)
    population = normalize_term(pico.population.value)
    outcome = normalize_term(pico.outcome.value)
    design = normalize_term(pico.design.value)

    if intervention and outcome:
        add(
            f"{intervention}对{outcome}的影响",
            f"{intervention}对{outcome}有何影响？",
            f"来自文献：{article.title}" if article else "见文献原文",
            min(0.8, pico.intervention.confidence + 0.1),
        )
    if intervention and population:
        add(
            f"{intervention}在{population}中的应用",
            f"{intervention}用于{population}时的要点是什么？",
            pico.intervention.evidence or "见文献原文",
            pico.intervention.confidence,
        )
    if design:
        add(
            f"{design}",
            f"{design}的特点与适用条件是什么？",
            pico.design.evidence or "见文献原文",
            pico.design.confidence,
        )

    # 三元组中的实体也作为候选知识点
    for triple in triples[:10]:
        add(triple.entity, f"「{triple.entity}」在药学中的要点是什么？", triple.evidence or "见文献原文", triple.confidence)

    return suggestions


# --- 确认入库 ---------------------------------------------------------------


def existing_triples(
    db: Session, user_id: int, source_type: str, source_id: str
) -> set[tuple[str, str, str]]:
    stmt = select(KnowledgeGraph.entity, KnowledgeGraph.relation, KnowledgeGraph.target).where(
        KnowledgeGraph.user_id == user_id,
        KnowledgeGraph.source_type == source_type,
        KnowledgeGraph.source_id == source_id,
    )
    return {(row[0], row[1], row[2]) for row in db.execute(stmt).all()}


def confirm_triples(
    db: Session,
    user_id: int,
    triples: Sequence[Triple | dict[str, Any]],
    source_type: str = "pubmed",
    source_id: str = "",
) -> tuple[list[KnowledgeGraph], int]:
    """把**人工确认后**的三元组写入 MySQL。

    返回 ``(新增记录, 跳过的重复数)``。重复判定基于
    (user_id, source_type, source_id, entity, relation, target)。
    """
    known = existing_triples(db, user_id, source_type, source_id)
    created: list[KnowledgeGraph] = []
    skipped = 0

    for item in triples:
        if isinstance(item, dict):
            entity = normalize_term(str(item.get("entity", "")))
            relation = normalize_term(str(item.get("relation", "")))
            target = normalize_term(str(item.get("target", "")))
        else:
            entity, relation, target = (normalize_term(part) for part in item.key())

        if not (entity and relation and target):
            skipped += 1
            continue
        if (entity, relation, target) in known:
            skipped += 1
            continue

        known.add((entity, relation, target))
        record = KnowledgeGraph(
            user_id=user_id,
            source_type=source_type,
            source_id=str(source_id),
            fingerprint=triple_fingerprint(entity, relation, target),
            entity=entity,
            relation=relation,
            target=target,
        )
        # 每条一个 SAVEPOINT：并发/重试撞上唯一约束时只跳过这一条，
        # 不会让整批失败（应用层去重之外的第二道防线）。
        try:
            with db.begin_nested():
                db.add(record)
        except IntegrityError:
            skipped += 1
            logger.info("跳过重复三元组（唯一约束拦截）: %s -%s-> %s", entity, relation, target)
            continue
        created.append(record)

    db.flush()
    logger.info(
        "确认入库 source=%s/%s 新增 %d 条，跳过 %d 条",
        source_type,
        source_id,
        len(created),
        skipped,
    )
    return created, skipped


# --- NetworkX 知识图谱 ------------------------------------------------------


def fetch_triples(
    db: Session,
    user_id: int,
    source_type: str | None = None,
    source_id: str | None = None,
) -> list[KnowledgeGraph]:
    stmt = select(KnowledgeGraph).where(KnowledgeGraph.user_id == user_id)
    if source_type:
        stmt = stmt.where(KnowledgeGraph.source_type == source_type)
    if source_id:
        stmt = stmt.where(KnowledgeGraph.source_id == str(source_id))
    return list(db.execute(stmt.order_by(KnowledgeGraph.id.asc())).scalars().all())


def build_graph(records: Sequence[KnowledgeGraph]) -> nx.DiGraph:
    """用 NetworkX 构建有向知识图谱。

    同一对 (entity, relation, target) 出现多次时累加 ``weight``。
    """
    graph = nx.DiGraph()
    for record in records:
        entity, target = record.entity, record.target
        if not entity or not target:
            continue
        if graph.has_edge(entity, target):
            graph[entity][target]["weight"] += 1
            relations = graph[entity][target]["relations"]
            if record.relation not in relations:
                relations.append(record.relation)
        else:
            graph.add_edge(
                entity,
                target,
                weight=1,
                relation=record.relation,
                relations=[record.relation],
                source_type=record.source_type,
                source_id=record.source_id,
            )
        graph.nodes[entity].setdefault("sources", set()).add(record.source_type)
        graph.nodes[target].setdefault("sources", set()).add(record.source_type)

    return graph


def graph_to_json(graph: nx.DiGraph) -> dict[str, Any]:
    """导出为前端可视化所需的 {nodes, edges} 结构。"""
    degrees = dict(graph.degree())
    nodes = [
        {
            "id": node,
            "label": node,
            "degree": int(degrees.get(node, 0)),
            "in_degree": int(graph.in_degree(node)),
            "out_degree": int(graph.out_degree(node)),
            "sources": sorted(graph.nodes[node].get("sources", set())),
        }
        for node in graph.nodes
    ]
    edges = [
        {
            "source": source,
            "target": target,
            "label": " / ".join(data.get("relations", []) or [data.get("relation", "")]),
            "relations": data.get("relations", []),
            "weight": int(data.get("weight", 1)),
            "source_type": data.get("source_type", ""),
            "source_id": data.get("source_id", ""),
        }
        for source, target, data in graph.edges(data=True)
    ]
    nodes.sort(key=lambda n: (-n["degree"], n["id"]))
    return {"nodes": nodes, "edges": edges}


def graph_stats(graph: nx.DiGraph) -> dict[str, Any]:
    """图谱统计信息。"""
    if graph.number_of_nodes() == 0:
        return {
            "node_count": 0,
            "edge_count": 0,
            "density": 0.0,
            "component_count": 0,
            "top_nodes": [],
            "relation_counts": {},
        }

    degrees = dict(graph.degree())
    top_nodes = sorted(degrees.items(), key=lambda item: (-item[1], item[0]))[:10]
    relation_counts: dict[str, int] = {}
    for _source, _target, data in graph.edges(data=True):
        for relation in data.get("relations", []) or [data.get("relation", "")]:
            if relation:
                relation_counts[relation] = relation_counts.get(relation, 0) + 1

    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "density": round(nx.density(graph), 4),
        "component_count": nx.number_weakly_connected_components(graph),
        "is_dag": nx.is_directed_acyclic_graph(graph),
        "top_nodes": [{"node": node, "degree": degree} for node, degree in top_nodes],
        "relation_counts": dict(
            sorted(relation_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
    }


def export_graph(graph: nx.DiGraph, fmt: str = "json") -> tuple[str, str]:
    """导出图谱，返回 (内容, 媒体类型)。"""
    fmt = (fmt or "json").lower()
    if fmt == "graphml":
        return _graphml_string(graph), "application/xml"
    if fmt == "gexf":
        return _gexf_string(graph), "application/gexf+xml"
    return json.dumps(graph_to_json(graph), ensure_ascii=False, indent=2), "application/json"


def _graphml_string(graph: nx.DiGraph) -> str:
    """GraphML 需要标量属性，这里把列表属性字符串化。"""
    clean = _scalarize(graph)
    import io

    buffer = io.BytesIO()
    nx.write_graphml(clean, buffer)
    return buffer.getvalue().decode("utf-8")


def _gexf_string(graph: nx.DiGraph) -> str:
    clean = _scalarize(graph)
    import io

    buffer = io.BytesIO()
    nx.write_gexf(clean, buffer)
    return buffer.getvalue().decode("utf-8")


def _scalarize(graph: nx.DiGraph) -> nx.DiGraph:
    """把 set/list 属性转成字符串，便于 GraphML/GEXF 序列化。"""
    clean = nx.DiGraph()
    for node, data in graph.nodes(data=True):
        clean.add_node(
            node,
            **{
                key: ("、".join(sorted(value)) if isinstance(value, (set, list)) else value)
                for key, value in data.items()
            },
        )
    for source, target, data in graph.edges(data=True):
        clean.add_edge(
            source,
            target,
            **{
                key: ("、".join(value) if isinstance(value, list) else value)
                for key, value in data.items()
            },
        )
    return clean


def graph_terms(graph: nx.DiGraph, limit: int = 20) -> list[str]:
    """按度数排序返回图谱中的实体名（可供「转为卡片」选用）。"""
    degrees = dict(graph.degree())
    ranked = sorted(degrees.items(), key=lambda item: (-item[1], item[0]))
    return [node for node, _degree in ranked[:limit]]
