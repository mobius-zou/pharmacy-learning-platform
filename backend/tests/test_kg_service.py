"""知识图谱服务单元测试（ChromaDB 用临时目录；不需要 MySQL）。

重点覆盖：
- 离线嵌入的**跨进程确定性**（持久化向量的正确性前提）
- 草稿区（待人工校对）的完整生命周期与归属隔离
- 三元组规则抽取与去重
- NetworkX 建图、统计与导出
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest

from app.services import kg_service
from app.services.literature_service import Article, PicoD, PicoDField


@pytest.fixture
def pico() -> PicoD:
    return PicoD(
        population=PicoDField(value="2 型糖尿病患者", confidence=0.55, evidence="adults with T2DM"),
        intervention=PicoDField(value="二甲双胍", confidence=0.6, evidence="received metformin"),
        comparator=PicoDField(value="安慰剂", confidence=0.6, evidence="or placebo"),
        outcome=PicoDField(value="HbA1c 下降", confidence=0.5, evidence="primary outcome"),
        design=PicoDField(value="随机对照试验（RCT）", confidence=0.95, evidence="RCT"),
    )


@pytest.fixture
def article() -> Article:
    return Article(
        pmid="12345678",
        title="Metformin in type 2 diabetes: a randomized trial",
        abstract="Adults with T2DM received metformin or placebo. The primary outcome was HbA1c.",
        publication_types=["Randomized Controlled Trial"],
    )


# --- 嵌入 -------------------------------------------------------------------


def test_embedding_is_deterministic_and_normalized() -> None:
    first = kg_service.embed_text("阿司匹林抑制环氧化酶")
    second = kg_service.embed_text("阿司匹林抑制环氧化酶")

    assert first == second
    assert len(first) == 256
    norm = sum(value * value for value in first) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-9)


def test_embedding_empty_text_is_zero_vector() -> None:
    vector = kg_service.embed_text("")
    assert len(vector) == 256
    assert all(value == 0.0 for value in vector)


def test_embedding_similar_text_scores_higher_than_unrelated() -> None:
    """检索可用性的最低要求：相关文本的余弦相似度更高。"""
    query = kg_service.embed_text("阿司匹林的作用机制")

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    related = kg_service.embed_text("阿司匹林的作用机制是抑制环氧化酶")
    unrelated = kg_service.embed_text("青霉素的合成路线与结构修饰")

    assert cosine(query, related) > cosine(query, unrelated)


def test_embedding_is_stable_across_processes() -> None:
    """回归测试：绝不能使用 Python 内置 hash()（每进程随机化）。

    若嵌入随进程变化，持久化到 ChromaDB 的向量会在重启后失去可比性。
    """
    script = (
        "from app.services.kg_service import embed_text;"
        "v=embed_text('阿司匹林');"
        "print(sum(int(x*1e9) for x in v))"
    )
    backend_dir = Path(__file__).resolve().parent.parent
    results = []
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=backend_dir,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert completed.returncode == 0, completed.stderr
        results.append(completed.stdout.strip())

    assert results[0] == results[1]
    # 与当前进程内计算的结果也一致
    local = sum(int(x * 1e9) for x in kg_service.embed_text("阿司匹林"))
    assert results[0] == str(local)


def test_hash_to_index_is_in_range() -> None:
    for token in ("a", "阿司匹林", "w:aspirin", ""):
        assert 0 <= kg_service._hash_to_index(token, 64) < 64


# --- 草稿区（人工校对关卡） -------------------------------------------------


def test_draft_lifecycle() -> None:
    payload = {"article": {"pmid": "1", "title": "T", "abstract": "A"}, "pico_d": {}, "triples": []}
    draft = kg_service.save_draft(101, "1", payload, extraction_source="heuristic")

    assert draft.status == kg_service.DRAFT_PENDING
    assert draft.draft_id

    fetched = kg_service.get_draft(draft.draft_id, 101)
    assert fetched is not None
    assert fetched.pmid == "1"
    assert fetched.payload["article"]["title"] == "T"

    listed = kg_service.list_drafts(101, status=kg_service.DRAFT_PENDING)
    assert draft.draft_id in [d.draft_id for d in listed]

    updated = kg_service.update_draft(draft.draft_id, 101, payload={**payload, "edited": True})
    assert updated is not None
    assert updated.payload["edited"] is True

    confirmed = kg_service.set_draft_status(draft.draft_id, 101, kg_service.DRAFT_INDEXED)
    assert confirmed is not None and confirmed.status == kg_service.DRAFT_INDEXED
    assert draft.draft_id not in [
        d.draft_id for d in kg_service.list_drafts(101, status=kg_service.DRAFT_PENDING)
    ]

    assert kg_service.delete_draft(draft.draft_id, 101) is True
    assert kg_service.get_draft(draft.draft_id, 101) is None


def test_draft_is_isolated_between_users() -> None:
    """草稿必须按用户隔离：他人既读不到也改不了。"""
    payload = {"article": {"pmid": "9", "title": "Secret", "abstract": "x"}}
    draft = kg_service.save_draft(201, "9", payload)

    other_id = draft.draft_id + "-does-not-matter"
    assert kg_service.get_draft(draft.draft_id, 202) is None
    assert kg_service.update_draft(draft.draft_id, 202, note="hack") is None
    assert kg_service.set_draft_status(draft.draft_id, 202, kg_service.DRAFT_INDEXED) is None
    assert kg_service.delete_draft(draft.draft_id, 202) is False
    assert other_id  # 仅避免未使用告警

    # 原用户仍然可用
    assert kg_service.get_draft(draft.draft_id, 201) is not None
    kg_service.delete_draft(draft.draft_id, 201)


def test_draft_listing_filters_by_status() -> None:
    payload = {"article": {"pmid": "77", "title": "T", "abstract": "A"}}
    first = kg_service.save_draft(301, "77", payload)
    second = kg_service.save_draft(301, "78", payload)

    kg_service.set_draft_status(second.draft_id, 301, kg_service.DRAFT_REJECTED)

    pending_ids = [d.draft_id for d in kg_service.list_drafts(301, status=kg_service.DRAFT_PENDING)]
    rejected_ids = [d.draft_id for d in kg_service.list_drafts(301, status=kg_service.DRAFT_REJECTED)]
    all_ids = [d.draft_id for d in kg_service.list_drafts(301, status=None)]

    assert pending_ids == [first.draft_id]
    assert rejected_ids == [second.draft_id]
    assert set(all_ids) == {first.draft_id, second.draft_id}

    kg_service.delete_draft(first.draft_id, 301)
    kg_service.delete_draft(second.draft_id, 301)


def test_invalid_draft_status_rejected() -> None:
    payload = {"article": {"pmid": "1", "title": "T", "abstract": "A"}}
    draft = kg_service.save_draft(401, "1", payload)

    with pytest.raises(ValueError):
        kg_service.set_draft_status(draft.draft_id, 401, "not-a-status")

    kg_service.delete_draft(draft.draft_id, 401)


def test_draft_note_is_persisted() -> None:
    payload = {"article": {"pmid": "2", "title": "T", "abstract": "A"}}
    draft = kg_service.save_draft(501, "2", payload)

    kg_service.update_draft(draft.draft_id, 501, note="人工修正了干预措施")

    assert kg_service.get_draft(draft.draft_id, 501).note == "人工修正了干预措施"
    kg_service.delete_draft(draft.draft_id, 501)


def test_literature_index_and_vector_search() -> None:
    article = Article(
        pmid="555", title="Aspirin and COX inhibition", abstract="Aspirin irreversibly inhibits COX."
    )
    kg_service.index_article(article)

    hits = kg_service.search_literature("COX inhibition aspirin", limit=3)

    assert hits
    assert any("555" in hit["id"] for hit in hits)


# --- 规则抽取三元组 ---------------------------------------------------------


def test_extract_triples_rule_based(pico: PicoD, article: Article) -> None:
    triples = kg_service.extract_triples_rule_based(pico, article)
    keys = {t.key() for t in triples}

    assert ("二甲双胍", kg_service.RELATION_INTERVENES, "2 型糖尿病患者") in keys
    assert ("二甲双胍", kg_service.RELATION_COMPARED_WITH, "安慰剂") in keys
    assert ("二甲双胍", kg_service.RELATION_REPORTS_OUTCOME, "HbA1c 下降") in keys
    assert ("2 型糖尿病患者", kg_service.RELATION_USES_DESIGN, "随机对照试验（RCT）") in keys
    assert all(t.source == "rule" for t in triples)


def test_extract_triples_returns_empty_for_empty_pico() -> None:
    assert kg_service.extract_triples_rule_based(PicoD()) == []


def test_extract_triples_skips_partial_pairs() -> None:
    pico = PicoD(intervention=PicoDField(value="阿司匹林"))
    # 只有干预、没有人群/对照/结局 → 不应产生任何三元组
    assert kg_service.extract_triples_rule_based(pico) == []


def test_dedupe_triples_keeps_highest_confidence() -> None:
    triples = [
        kg_service.Triple("A", "治疗", "B", 0.3, "rule"),
        kg_service.Triple("A", "治疗", "B", 0.9, "llm"),
        kg_service.Triple("A", "治疗", "C", 0.5, "rule"),
    ]

    deduped = kg_service.dedupe_triples(triples)

    assert len(deduped) == 2
    strongest = next(t for t in deduped if t.target == "B")
    assert strongest.confidence == 0.9
    assert strongest.source == "llm"


def test_dedupe_triples_drops_empty_parts() -> None:
    triples = [
        kg_service.Triple("", "治疗", "B", 0.9, "rule"),
        kg_service.Triple("A", "", "B", 0.9, "rule"),
        kg_service.Triple("A", "治疗", "  ", 0.9, "rule"),
    ]
    assert kg_service.dedupe_triples(triples) == []


def test_normalize_term_strips_and_limits() -> None:
    assert kg_service.normalize_term("  阿司匹林 ，") == "阿司匹林"
    assert len(kg_service.normalize_term("x" * 500)) == 200


def test_extract_triples_falls_back_to_rules_without_llm(pico: PicoD, article: Article) -> None:
    """无 Agent 降级模式：未配置 LLM 时仍能产出三元组。"""
    triples, source, reason = kg_service.extract_triples(article, pico)

    assert source == "rule"
    assert triples
    assert reason and "LLM" in reason


def test_extract_triples_uses_llm_when_available(
    monkeypatch, pico: PicoD, article: Article
) -> None:
    class FakeRelationship:
        def __init__(self) -> None:
            self.source = "阿司匹林"
            self.target = "环氧化酶"
            self.type = "作用机制"
            self.confidence = 0.8

    class FakeGraphDocument:
        relationships = [FakeRelationship()]

    class FakeTransformer:
        def convert_to_graph_documents(self, documents):  # noqa: ANN001
            return [FakeGraphDocument()]

    monkeypatch.setattr(
        "app.langchain_adapter.build_graph_transformer",
        lambda **kwargs: (FakeTransformer(), None),
    )

    triples, source, reason = kg_service.extract_triples(article, pico)

    assert source == "llm"
    assert reason is None
    assert any(t.entity == "阿司匹林" and t.target == "环氧化酶" for t in triples)
    assert all(t.source == "llm" for t in triples)


def test_extract_triples_llm_failure_falls_back(monkeypatch, pico: PicoD, article: Article) -> None:
    class ExplodingTransformer:
        def convert_to_graph_documents(self, documents):  # noqa: ANN001
            raise RuntimeError("配额用尽")

    monkeypatch.setattr(
        "app.langchain_adapter.build_graph_transformer",
        lambda **kwargs: (ExplodingTransformer(), None),
    )

    triples, source, reason = kg_service.extract_triples(article, pico)

    assert source == "rule"
    assert triples
    assert "失败" in (reason or "")


def test_extract_triples_when_transformer_unavailable(
    monkeypatch, pico: PicoD, article: Article
) -> None:
    monkeypatch.setattr(
        "app.langchain_adapter.build_graph_transformer",
        lambda **kwargs: (None, "未配置 LLM"),
    )

    _triples, source, reason = kg_service.extract_triples(article, pico)

    assert source == "rule"
    assert reason == "未配置 LLM"


# --- 知识点建议 -------------------------------------------------------------


def test_suggest_knowledge_points(pico: PicoD, article: Article) -> None:
    triples = kg_service.extract_triples_rule_based(pico, article)
    suggestions = kg_service.suggest_knowledge_points(pico, triples, article)

    assert suggestions
    for item in suggestions:
        assert item["knowledge_point"]
        assert item["front"].endswith(("？", "?"))
        assert item["back"]

    points = [s["knowledge_point"] for s in suggestions]
    assert len(points) == len(set(points)), "知识点不应重复"


def test_suggest_knowledge_points_empty_for_empty_pico() -> None:
    assert kg_service.suggest_knowledge_points(PicoD(), []) == []


# --- NetworkX ---------------------------------------------------------------


class _Record:
    """模拟 KnowledgeGraph 行对象。"""

    def __init__(self, entity: str, relation: str, target: str, source_type="pubmed", source_id="1"):
        self.entity = entity
        self.relation = relation
        self.target = target
        self.source_type = source_type
        self.source_id = source_id


def test_build_graph_aggregates_parallel_edges() -> None:
    graph = kg_service.build_graph(
        [
            _Record("A", "治疗", "B"),
            _Record("A", "报告结局", "B"),
            _Record("B", "研究对象", "C"),
        ]
    )

    assert isinstance(graph, nx.DiGraph)
    assert graph.number_of_nodes() == 3
    assert graph.number_of_edges() == 2
    assert graph["A"]["B"]["weight"] == 2
    assert set(graph["A"]["B"]["relations"]) == {"治疗", "报告结局"}


def test_build_graph_ignores_blank_terms() -> None:
    graph = kg_service.build_graph([_Record("", "治疗", "B"), _Record("A", "治疗", "")])
    assert graph.number_of_nodes() == 0


def test_graph_to_json_shape() -> None:
    graph = kg_service.build_graph([_Record("A", "治疗", "B"), _Record("B", "治疗", "C")])
    data = kg_service.graph_to_json(graph)

    assert {node["id"] for node in data["nodes"]} == {"A", "B", "C"}
    assert len(data["edges"]) == 2
    # 度数最高的节点排在前面
    assert data["nodes"][0]["id"] == "B"
    assert data["nodes"][0]["degree"] == 2
    assert data["nodes"][0]["in_degree"] == 1
    assert data["nodes"][0]["out_degree"] == 1
    assert all("label" in edge for edge in data["edges"])


def test_graph_stats() -> None:
    graph = kg_service.build_graph(
        [_Record("A", "治疗", "B"), _Record("C", "治疗", "D")]
    )
    stats = kg_service.graph_stats(graph)

    assert stats["node_count"] == 4
    assert stats["edge_count"] == 2
    assert stats["component_count"] == 2
    assert stats["is_dag"] is True
    assert stats["relation_counts"] == {"治疗": 2}
    assert stats["density"] > 0


def test_graph_stats_on_empty_graph() -> None:
    stats = kg_service.graph_stats(nx.DiGraph())

    assert stats["node_count"] == 0
    assert stats["edge_count"] == 0
    assert stats["top_nodes"] == []
    assert stats["component_count"] == 0


def test_graph_stats_detects_cycle() -> None:
    graph = kg_service.build_graph([_Record("A", "治疗", "B"), _Record("B", "治疗", "A")])
    assert kg_service.graph_stats(graph)["is_dag"] is False


def test_export_json_roundtrip() -> None:
    graph = kg_service.build_graph([_Record("阿司匹林", "作用机制", "环氧化酶")])
    content, media_type = kg_service.export_graph(graph, "json")

    assert media_type == "application/json"
    payload = json.loads(content)
    assert payload["edges"][0]["source"] == "阿司匹林"


def test_export_graphml_and_gexf_are_wellformed() -> None:
    graph = kg_service.build_graph([_Record("A", "治疗", "B")])

    graphml, media_type = kg_service.export_graph(graph, "graphml")
    assert media_type == "application/xml"
    assert "<graphml" in graphml
    assert "治疗" in graphml

    # GraphML 可被 networkx 重新读入（往返验证）
    import io

    reloaded = nx.read_graphml(io.BytesIO(graphml.encode("utf-8")))
    assert set(reloaded.nodes) == {"A", "B"}

    gexf, media_type = kg_service.export_graph(graph, "gexf")
    assert media_type == "application/gexf+xml"
    assert "<gexf" in gexf


def test_export_unknown_format_falls_back_to_json() -> None:
    graph = kg_service.build_graph([_Record("A", "治疗", "B")])
    content, media_type = kg_service.export_graph(graph, "bogus")

    assert media_type == "application/json"
    assert "nodes" in json.loads(content)


def test_graph_terms_ranked_by_degree() -> None:
    # Hub 出度 3、总度数 3，唯一最高（degree 计入度+出度，故需拉开差距）
    graph = kg_service.build_graph(
        [
            _Record("Hub", "治疗", "A"),
            _Record("Hub", "治疗", "B"),
            _Record("Hub", "治疗", "C"),
            _Record("A", "治疗", "B"),
        ]
    )
    terms = kg_service.graph_terms(graph)

    assert terms[0] == "Hub"
    assert kg_service.graph_stats(graph)["top_nodes"][0]["node"] == "Hub"
