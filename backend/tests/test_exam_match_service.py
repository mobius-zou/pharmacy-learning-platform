"""考点匹配算法与优先级判定单元测试（不需要数据库、不加载真实模型）。

使用 ``HashingBackend``（离线确定性嵌入）作为语义后端，因此测试快速、可复现，
且不会触发 sentence-transformers 的模型下载。
"""

from __future__ import annotations

import pytest

from app.embeddings import HashingBackend, cosine_similarity
from app.models import PRIORITY_CONFIRM, PRIORITY_CONSOLIDATE, PRIORITY_HIGH
from app.services import exam_match_service as ex


@pytest.fixture(scope="module")
def backend() -> HashingBackend:
    return HashingBackend()


# --- 同义词替换 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_fragment"),
    [
        ("aspirin", "阿司匹林"),
        ("乙酰水杨酸", "阿司匹林"),
        ("COX", "环氧化酶"),
        ("环氧合酶", "环氧化酶"),
        ("t1/2", "半衰期"),
        ("half-life", "半衰期"),
        ("HPLC", "高效液相色谱"),
        ("生物利用度", "生物利用度"),
        ("ADR", "不良反应"),
        ("CYP450", "肝药酶"),
    ],
)
def test_apply_synonyms_maps_variants(raw: str, expected_fragment: str) -> None:
    assert expected_fragment in ex.apply_synonyms(raw)


def test_apply_synonyms_is_idempotent() -> None:
    once = ex.apply_synonyms("aspirin 抑制 COX")
    assert ex.apply_synonyms(once) == once


def test_apply_synonyms_handles_empty() -> None:
    assert ex.apply_synonyms("") == ""


def test_synonym_lookup_prefers_longest_variant() -> None:
    """长变体优先替换，避免短串先命中导致残留。"""
    # "acetylsalicylic acid" 不应被 "asa" 之类的短串破坏
    assert "阿司匹林" in ex.apply_synonyms("acetylsalicylic acid")


def test_synonym_dictionary_is_non_trivial() -> None:
    assert len(ex.synonym_dictionary()) >= len(ex.SYNONYMS)


# --- 归一化 -----------------------------------------------------------------


def test_normalize_text_strips_punctuation_and_lowercases() -> None:
    assert ex.normalize_text("阿司匹林的作用机制？") == "阿司匹林的作用机制"
    assert ex.normalize_text("  COX-1 、COX-2  ") == "环氧化酶1环氧化酶2"


def test_normalize_text_empty() -> None:
    assert ex.normalize_text("") == ""
    assert ex.normalize_text("   ") == ""


# --- 分词 -------------------------------------------------------------------


def test_tokenize_english_words() -> None:
    # 顺序为「先英文词元，再中文串」，功能上无影响
    assert ex.tokenize("HPLC and GC") == ["and", "高效液相色谱", "气相色谱"]


def test_tokenize_uses_domain_dictionary() -> None:
    tokens = ex.tokenize("阿司匹林抑制环氧化酶")

    assert "阿司匹林" in tokens
    assert "环氧化酶" in tokens


def test_tokenize_deduplicates_preserving_order() -> None:
    tokens = ex.tokenize("阿司匹林与阿司匹林")
    assert tokens.count("阿司匹林") == 1


def test_tokenize_empty_and_punctuation_only() -> None:
    assert ex.tokenize("") == []
    assert ex.tokenize("，。！") == []


def test_tokenize_custom_dictionary() -> None:
    tokens = ex.tokenize("青蒿素抗疟", dictionary=["青蒿素", "抗疟"])
    assert "青蒿素" in tokens


# --- 编辑距离 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("阿司匹林", "阿司匹林", 0),
        ("阿司匹林", "阿司匹林片", 1),
        ("abc", "", 3),
        ("", "abc", 3),
        ("kitten", "sitting", 3),
        ("", "", 0),
    ],
)
def test_levenshtein_distance(a: str, b: str, expected: int) -> None:
    assert ex.levenshtein_distance(a, b) == expected


def test_edit_similarity_bounds() -> None:
    assert ex.edit_similarity("阿司匹林", "阿司匹林") == 1.0
    assert ex.edit_similarity("", "") == 1.0
    assert ex.edit_similarity("abc", "") == 0.0
    assert 0.0 <= ex.edit_similarity("阿司匹林", "青霉素") <= 1.0


def test_similarity_with_containment_rewards_inclusion() -> None:
    """包含关系应比纯编辑距离得分更高（短考点常是长知识点的一部分）。"""
    a, b = "阿司匹林的作用机制", "阿司匹林的作用机制与临床应用"
    assert a in b, "本用例要求真正构成包含关系"

    plain = ex.edit_similarity(a, b)
    boosted = ex.similarity_with_containment(a, b)

    assert boosted > plain
    assert boosted >= ex.CONTAINMENT_SIMILARITY


def test_similarity_with_containment_without_inclusion() -> None:
    a, b = "青霉素", "阿司匹林"
    assert ex.similarity_with_containment(a, b) == ex.edit_similarity(a, b)


# --- 考点切分 ---------------------------------------------------------------


def test_split_exam_points_numbered_list() -> None:
    text = "1. 阿司匹林的作用机制\n2. COX-1 与 COX-2 的区别\n3. 半衰期计算"
    assert ex.split_exam_points(text) == [
        "阿司匹林的作用机制",
        "COX-1 与 COX-2 的区别",
        "半衰期计算",
    ]


def test_split_exam_points_chinese_numbering_and_bullets() -> None:
    text = "一、阿司匹林的作用机制\n• COX 抑制\n(2) 半衰期"
    assert ex.split_exam_points(text) == ["阿司匹林的作用机制", "COX 抑制", "半衰期"]


def test_split_exam_points_semicolons_and_blank_lines() -> None:
    text = "阿司匹林的作用机制；COX 的分类\n\n\n半衰期计算"
    assert ex.split_exam_points(text) == ["阿司匹林的作用机制", "COX 的分类", "半衰期计算"]


def test_split_exam_points_deduplicates() -> None:
    text = "阿司匹林的作用机制\n阿司匹林的作用机制\n aspirin 的作用机制"
    # 第三行经同义词归一化后与第一行相同，应被去重
    assert ex.split_exam_points(text) == ["阿司匹林的作用机制"]


def test_split_exam_points_respects_max_points() -> None:
    text = "\n".join(f"{i}. 考点{i}" for i in range(1, 11))
    assert len(ex.split_exam_points(text, max_points=3)) == 3


def test_split_exam_points_empty() -> None:
    assert ex.split_exam_points("") == []
    assert ex.split_exam_points("   \n  ") == []


# --- 候选构建与初筛 ---------------------------------------------------------


def test_build_candidates_deduplicates_and_skips_blank() -> None:
    candidates = ex.build_candidates(
        [("阿司匹林", "knowledge_graph"), ("阿司匹林", "card_tag"), ("", "card_tag"), ("  ", "x")]
    )
    assert len(candidates) == 1
    assert candidates[0].source == "knowledge_graph", "同知识点保留来源权重更高者"


def test_prefilter_orders_by_similarity_and_limits() -> None:
    candidates = ex.build_candidates(
        [("阿司匹林的作用机制", "knowledge_graph"), ("青霉素的抗菌机制", "knowledge_graph")]
    )
    shortlist = ex.prefilter_candidates("阿司匹林作用机制", candidates, limit=1)

    assert len(shortlist) == 1
    assert shortlist[0][1].knowledge_point == "阿司匹林的作用机制"


# --- 匹配（初筛 + 精筛） ----------------------------------------------------


def test_match_point_exact_text_matches(backend: HashingBackend) -> None:
    candidates = ex.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])

    result = ex.match_point("阿司匹林的作用机制", candidates, backend=backend)

    assert result.matched is True
    assert result.knowledge_point == "阿司匹林的作用机制"
    assert result.score > 0.9
    assert result.backend == backend.name


def test_match_point_via_synonym_matches(backend: HashingBackend) -> None:
    """同义词替换后应能匹配上不同写法的考点。"""
    candidates = ex.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])

    result = ex.match_point("aspirin 的作用机制", candidates, backend=backend)

    assert result.matched is True
    assert result.knowledge_point == "阿司匹林的作用机制"


def test_match_point_unrelated_text_not_matched(backend: HashingBackend) -> None:
    candidates = ex.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])

    result = ex.match_point("色谱柱的保养与柱效评价方法", candidates, backend=backend)

    assert result.matched is False
    assert result.knowledge_point == ""
    assert result.alternatives, "未匹配时也应给出最接近的候选供人工确认"


def test_match_point_with_no_candidates(backend: HashingBackend) -> None:
    result = ex.match_point("任意考点", [], backend=backend)

    assert result.matched is False
    assert result.score == 0.0


def test_match_point_empty_text(backend: HashingBackend) -> None:
    candidates = ex.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])

    assert ex.match_point("   ", candidates, backend=backend).matched is False


def test_match_point_picks_best_among_many(backend: HashingBackend) -> None:
    candidates = ex.build_candidates(
        [
            ("阿司匹林的作用机制", "knowledge_graph"),
            ("青霉素的抗菌机制", "knowledge_graph"),
            ("半衰期的计算", "knowledge_graph"),
        ]
    )

    result = ex.match_point("半衰期的计算方法", candidates, backend=backend)

    assert result.knowledge_point == "半衰期的计算"
    assert result.alternatives, "应给出其他候选"


def test_match_point_respects_min_score(backend: HashingBackend) -> None:
    candidates = ex.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])

    result = ex.match_point(
        "阿司匹林的作用机制", candidates, backend=backend, min_score=1.5
    )

    assert result.matched is False, "阈值高于最高可能分时应视为未匹配"


def test_match_point_falls_back_to_edit_distance_when_backend_fails() -> None:
    """语义后端异常时必须退化为纯编辑距离，而不是整体失败。"""

    class BrokenBackend:
        name = "broken"
        is_semantic = True

        def encode(self, texts):  # noqa: ANN001
            raise RuntimeError("模型加载失败")

    candidates = ex.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])

    result = ex.match_point("阿司匹林的作用机制", candidates, backend=BrokenBackend())

    assert result.backend == "broken"
    assert result.semantic_similarity == 0.0
    assert result.matched is True, "编辑距离相似度为 1.0，仍应匹配成功"


def test_cosine_similarity_bounds() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_match_result_confidence_label(backend: HashingBackend) -> None:
    candidates = ex.build_candidates([("阿司匹林的作用机制", "knowledge_graph")])

    high = ex.match_point("阿司匹林的作用机制", candidates, backend=backend)
    assert high.confidence_label == "高置信"

    unmatched = ex.match_point("完全不相关的内容啊", candidates, backend=backend)
    if not unmatched.matched:
        assert unmatched.confidence_label == "未匹配"


# --- 掌握度判定（纯规则） ---------------------------------------------------


def _evidence(**kwargs) -> ex.MasteryEvidence:
    base = {"knowledge_point": "阿司匹林的作用机制"}
    base.update(kwargs)
    return ex.MasteryEvidence(**base)  # type: ignore[arg-type]


def test_mastery_weak_when_weak_point_hit() -> None:
    assert ex.compute_mastery(_evidence(weak_point_hit=True)) == ex.MASTERY_WEAK


def test_mastery_weak_when_no_cards() -> None:
    assert ex.compute_mastery(_evidence(card_count=0)) == ex.MASTERY_WEAK


def test_mastery_weak_when_all_cards_new() -> None:
    evidence = _evidence(card_count=3, new_card_count=3, reviewed_card_count=0)
    assert ex.compute_mastery(evidence) == ex.MASTERY_WEAK


def test_mastery_weak_when_errors_reach_threshold() -> None:
    evidence = _evidence(card_count=1, reviewed_card_count=1, avg_stability=50.0, total_errors=2)
    assert ex.compute_mastery(evidence, weak_threshold=2) == ex.MASTERY_WEAK


def test_mastery_mastered_when_stable_and_no_errors() -> None:
    evidence = _evidence(
        card_count=3,
        new_card_count=0,
        reviewed_card_count=3,
        avg_stability=40.0,
        max_stability=60.0,
    )
    assert ex.compute_mastery(evidence, mastered_stability=21.0) == ex.MASTERY_MASTERED


def test_mastery_moderate_when_stability_below_threshold() -> None:
    evidence = _evidence(
        card_count=2, new_card_count=0, reviewed_card_count=2, avg_stability=5.0
    )
    assert ex.compute_mastery(evidence, mastered_stability=21.0) == ex.MASTERY_MODERATE


def test_mastery_moderate_when_errors_below_threshold() -> None:
    evidence = _evidence(
        card_count=2,
        new_card_count=0,
        reviewed_card_count=2,
        avg_stability=40.0,
        total_errors=1,
    )
    assert ex.compute_mastery(evidence, weak_threshold=2, mastered_stability=21.0) == ex.MASTERY_MODERATE


def test_mastery_moderate_when_stability_missing() -> None:
    evidence = _evidence(card_count=1, new_card_count=0, reviewed_card_count=1, avg_stability=None)
    assert ex.compute_mastery(evidence) == ex.MASTERY_MODERATE


# --- 优先级映射 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("mastery", "expected"),
    [
        (ex.MASTERY_WEAK, PRIORITY_HIGH),
        (ex.MASTERY_MODERATE, PRIORITY_CONSOLIDATE),
        (ex.MASTERY_MASTERED, PRIORITY_CONFIRM),
    ],
)
def test_classify_priority_mapping(mastery: str, expected: str) -> None:
    assert ex.classify_priority(True, mastery) == expected


def test_unmatched_point_is_high_risk() -> None:
    """未匹配到知识点 => 缺少复习材料，按高危处理。"""
    assert ex.classify_priority(False, ex.MASTERY_MODERATE) == PRIORITY_HIGH
    assert ex.classify_priority(False, ex.MASTERY_MASTERED) == PRIORITY_HIGH


def test_priority_reason_for_unmatched() -> None:
    reason = ex.build_priority_reason(False, ex.MASTERY_WEAK, _evidence())
    assert "暂无对应知识点" in reason


def test_priority_reason_for_weak_point() -> None:
    reason = ex.build_priority_reason(
        True, ex.MASTERY_WEAK, _evidence(weak_point_hit=True, total_errors=3)
    )
    assert "薄弱点" in reason
    assert "3" in reason


def test_priority_reason_for_mastered() -> None:
    evidence = _evidence(reviewed_card_count=3, avg_stability=42.0)
    reason = ex.build_priority_reason(True, ex.MASTERY_MASTERED, evidence)
    assert "稳定性" in reason


# --- 排序 -------------------------------------------------------------------


def _finding(point: str, priority: str, score: float) -> ex.ExamFinding:
    match = ex.MatchResult(
        point_text=point,
        knowledge_point="KP",
        matched=True,
        score=score,
        edit_similarity=score,
        semantic_similarity=score,
        backend="fake",
    )
    return ex.ExamFinding(
        exam_point_id=0,
        point_text=point,
        match=match,
        evidence=_evidence(),
        mastery=ex.MASTERY_WEAK,
        priority=priority,
        reason="",
    )


def test_sort_findings_orders_by_priority_then_score() -> None:
    findings = [
        _finding("确认1", PRIORITY_CONFIRM, 0.99),
        _finding("高危低分", PRIORITY_HIGH, 0.30),
        _finding("巩固1", PRIORITY_CONSOLIDATE, 0.80),
        _finding("高危高分", PRIORITY_HIGH, 0.90),
    ]

    ordered = ex.sort_findings(findings)

    assert [f.point_text for f in ordered] == ["高危高分", "高危低分", "巩固1", "确认1"]


# --- 语义模型的价值（未安装 sentence-transformers 时自动跳过） --------------


@pytest.mark.semantic
def test_sentence_transformer_matches_paraphrase_hashing_misses() -> None:
    """sentence-transformers 的核心价值：救回**没有字面重叠**的考点表述。

    纯编辑距离/哈希嵌入依赖字面重合，而「aspirin 是怎么起效的」与
    「阿司匹林的作用机制」几乎没有共同字符，只有真正的语义模型能匹配上。
    """
    from app.embeddings import SentenceTransformerBackend

    try:
        semantic = SentenceTransformerBackend()
        semantic._load()  # noqa: SLF001 - 显式触发加载，失败即跳过
    except Exception as exc:  # 未安装 / 模型不可用
        pytest.skip(f"sentence-transformers 不可用：{type(exc).__name__}: {exc}")

    candidates = ex.build_candidates(
        [
            ("阿司匹林的作用机制", "knowledge_graph"),
            ("高效液相色谱", "knowledge_graph"),
        ]
    )
    paraphrase = "aspirin 是怎么起效的"

    semantic_result = ex.match_point(paraphrase, candidates, backend=semantic)
    hashing_result = ex.match_point(paraphrase, candidates, backend=HashingBackend())

    assert semantic_result.matched is True, "语义模型应能匹配该同义表述"
    assert semantic_result.knowledge_point == "阿司匹林的作用机制"
    assert semantic_result.semantic_similarity > hashing_result.semantic_similarity, (
        "语义相似度应显著高于离线哈希嵌入"
    )
    assert semantic_result.score > hashing_result.score
