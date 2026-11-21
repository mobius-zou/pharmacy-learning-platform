"""错题诊断纯逻辑单元测试（不需要数据库）。

重点覆盖阶段二验收要求：**薄弱点触发逻辑**（同一知识点错 2 次触发警报），
以及知识点识别与苏格拉底式提问在「无 Agent 降级模式」下的行为。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.llm import LLMResponse, parse_json_object
from app.services.wrong_question_service import (
    MAX_KNOWLEDGE_POINT_LENGTH,
    SOCRATIC_JUDGMENT,
    SOCRATIC_PREFERENCE,
    SOCRATIC_PROCEDURAL,
    SOCRATIC_TYPE_LABELS,
    UNCLASSIFIED_KNOWLEDGE_POINT,
    VALID_SOCRATIC_TYPES,
    SocraticSuggestion,
    WeakPoint,
    analyze_question,
    classify_socratic_type,
    generate_socratic_question,
    heuristic_knowledge_point,
    heuristic_socratic_question,
    identify_knowledge_point,
    infer_subject,
    is_weak_point,
    normalize_knowledge_point,
    socratic_has_answer,
    socratic_type_label,
    summarize_weak_points,
)


# --- 薄弱点触发逻辑（阶段二核心验收） ---------------------------------------


def test_threshold_default_is_two() -> None:
    from app.config import settings

    assert settings.weak_point_threshold == 2


def test_one_error_does_not_trigger_weak_point() -> None:
    """错 1 次不算薄弱点。"""
    points = summarize_weak_points({"阿司匹林的作用机制": 1}, threshold=2)

    assert len(points) == 1
    assert points[0].is_weak is False
    assert points[0].errors_to_threshold == 1
    assert points[0].severity == "normal"


def test_two_errors_on_same_knowledge_point_triggers_alert() -> None:
    """验收标准：同一知识点错 2 次后触发薄弱点警报。"""
    points = summarize_weak_points({"阿司匹林的作用机制": 2}, threshold=2)

    assert len(points) == 1
    point = points[0]
    assert point.is_weak is True
    assert point.errors_to_threshold == 0
    assert point.severity == "warning"
    assert point.total_errors == 2


def test_weak_point_uses_default_threshold_when_omitted() -> None:
    from app.config import settings

    assert is_weak_point(settings.weak_point_threshold, settings.weak_point_threshold)


def test_more_errors_escalates_severity() -> None:
    """错误次数达到阈值两倍时升级为 critical。"""
    warning = summarize_weak_points({"A": 2}, threshold=2)[0]
    critical = summarize_weak_points({"A": 4}, threshold=2)[0]

    assert warning.severity == "warning"
    assert critical.severity == "critical"


def test_custom_threshold_respected() -> None:
    """阈值可配置。"""
    assert summarize_weak_points({"A": 2}, threshold=3)[0].is_weak is False
    assert summarize_weak_points({"A": 3}, threshold=3)[0].is_weak is True
    assert summarize_weak_points({"A": 1}, threshold=1)[0].is_weak is True


@pytest.mark.parametrize(
    ("errors", "threshold", "expected"),
    [(1, 2, False), (2, 2, True), (3, 2, True), (0, 2, False), (5, 5, True), (4, 5, False)],
)
def test_is_weak_point_boundaries(errors: int, threshold: int, expected: bool) -> None:
    assert is_weak_point(errors, threshold) is expected


def test_weak_only_filters_out_normal_points() -> None:
    points = summarize_weak_points(
        {"弱A": 3, "弱B": 2, "正常C": 1}, threshold=2, weak_only=True
    )

    assert [p.knowledge_point for p in points] == ["弱A", "弱B"]


def test_summary_sorted_by_error_count_desc() -> None:
    points = summarize_weak_points({"A": 1, "B": 5, "C": 3}, threshold=2)

    assert [p.knowledge_point for p in points] == ["B", "C", "A"]
    assert [p.total_errors for p in points] == [5, 3, 1]


def test_summary_is_stable_for_equal_counts() -> None:
    """错误次数相同时按名称升序，保证输出稳定（与字典插入顺序无关）。"""
    first = summarize_weak_points({"乙": 2, "甲": 2}, threshold=2)
    second = summarize_weak_points({"甲": 2, "乙": 2}, threshold=2)

    names = [p.knowledge_point for p in first]
    assert names == [p.knowledge_point for p in second]
    # 升序按 Unicode 码点比较，不臆断中文的「字典序」
    assert names == sorted(["甲", "乙"])


def test_summary_carries_question_counts_and_related_cards() -> None:
    points = summarize_weak_points(
        {"A": 4},
        threshold=2,
        question_counts={"A": 2},
        related_card_ids={"A": [9, 3, 5]},
        last_error_at={"A": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    )

    point = points[0]
    assert point.question_count == 2
    assert point.related_card_ids == (3, 5, 9), "关联卡片 id 应排序去重"
    assert point.last_error_at is not None


def test_summary_handles_empty_input() -> None:
    assert summarize_weak_points({}, threshold=2) == []
    assert summarize_weak_points({}, threshold=2, weak_only=True) == []


def test_weak_point_dataclass_is_immutable() -> None:
    point = WeakPoint(knowledge_point="A", total_errors=2, question_count=1, threshold=2)
    with pytest.raises(Exception):
        point.total_errors = 5  # type: ignore[misc]


def test_knowledge_point_normalized_in_summary() -> None:
    """带空白的知识点名称会被规范化，避免同名不同写法拆散统计。"""
    points = summarize_weak_points({"  阿司匹林  ": 2}, threshold=2)

    assert points[0].knowledge_point == "阿司匹林"


def test_blank_knowledge_point_falls_back_to_unclassified() -> None:
    points = summarize_weak_points({"   ": 2}, threshold=2)

    assert points[0].knowledge_point == UNCLASSIFIED_KNOWLEDGE_POINT


# --- 知识点规范化与启发式提取 -----------------------------------------------


def test_normalize_knowledge_point_strips_and_caps() -> None:
    assert normalize_knowledge_point("  阿司匹林 的作用机制 ") == "阿司匹林 的作用机制"
    assert normalize_knowledge_point("，、阿司匹林。；") == "阿司匹林"
    assert normalize_knowledge_point(None) == ""

    long_text = "药" * (MAX_KNOWLEDGE_POINT_LENGTH + 50)
    assert len(normalize_knowledge_point(long_text)) == MAX_KNOWLEDGE_POINT_LENGTH


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("阿司匹林的作用机制是什么？", "阿司匹林的作用机制"),
        ("青霉素的抗菌机制如何？", "青霉素的抗菌机制"),
        ("简述地西泮的作用机制。", "地西泮的作用机制"),
        ("为什么青霉素要现配现用？", "青霉素要现配现用"),
        ("阿托品阻断什么受体", "阿托品阻断什么受体"),
    ],
)
def test_heuristic_knowledge_point_extraction(question: str, expected: str) -> None:
    assert heuristic_knowledge_point(question) == expected


def test_heuristic_reuses_known_knowledge_point() -> None:
    """命中已有知识点时复用同一字符串，薄弱点才能正确聚合。"""
    result = heuristic_knowledge_point(
        "请再讲讲阿司匹林的作用机制", known_points=["阿司匹林的作用机制"]
    )
    assert result == "阿司匹林的作用机制"


def test_heuristic_prefers_longest_known_point() -> None:
    result = heuristic_knowledge_point(
        "肾上腺素受体的分类", known_points=["受体", "肾上腺素受体"]
    )
    assert result == "肾上腺素受体"


def test_heuristic_falls_back_for_empty_question() -> None:
    assert heuristic_knowledge_point("") == UNCLASSIFIED_KNOWLEDGE_POINT
    assert heuristic_knowledge_point("   ") == UNCLASSIFIED_KNOWLEDGE_POINT


def test_infer_subject() -> None:
    assert infer_subject("药理学：阿司匹林的作用机制") == "药理学"
    assert infer_subject("这道题怎么做") is None


# --- 苏格拉底式问题类型 -----------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("阿司匹林的作用机制是什么？", SOCRATIC_PROCEDURAL),
        ("如何计算该药物的半衰期？", SOCRATIC_PROCEDURAL),
        ("写出合成步骤", SOCRATIC_PROCEDURAL),
        ("哪种剂型更好？", SOCRATIC_PREFERENCE),
        ("这两个方案哪个更值得优先选择？", SOCRATIC_PREFERENCE),
        ("比较阿司匹林与布洛芬的区别", SOCRATIC_JUDGMENT),
        ("分析该结构的性质", SOCRATIC_JUDGMENT),
    ],
)
def test_classify_socratic_type(question: str, expected: str) -> None:
    assert classify_socratic_type(question) == expected


def test_preference_takes_priority_over_procedural() -> None:
    """"更好"这类偏好措辞比"如何"更具体，应判为偏好性问题。"""
    assert classify_socratic_type("如何选择更好？") == SOCRATIC_PREFERENCE


def test_all_types_have_labels() -> None:
    assert set(VALID_SOCRATIC_TYPES) == {
        SOCRATIC_PROCEDURAL,
        SOCRATIC_PREFERENCE,
        SOCRATIC_JUDGMENT,
    }
    for qtype in VALID_SOCRATIC_TYPES:
        assert SOCRATIC_TYPE_LABELS[qtype]
        assert socratic_type_label(qtype)


def test_only_preference_has_no_standard_answer() -> None:
    """程序性问题与判断性问题有答案，偏好性问题没有。"""
    assert socratic_has_answer(SOCRATIC_PROCEDURAL) is True
    assert socratic_has_answer(SOCRATIC_JUDGMENT) is True
    assert socratic_has_answer(SOCRATIC_PREFERENCE) is False
    assert socratic_has_answer(None) is True
    assert socratic_type_label("unknown") is None


def test_socratic_templates_mention_knowledge_point_and_withhold_answer() -> None:
    kp = "阿司匹林的作用机制"

    procedural = heuristic_socratic_question("如何推导？", kp, SOCRATIC_PROCEDURAL)
    preference = heuristic_socratic_question("哪个更好？", kp, SOCRATIC_PREFERENCE)
    judgment = heuristic_socratic_question("比较两者", kp, SOCRATIC_JUDGMENT)

    for text in (procedural, preference, judgment):
        assert kp in text
        assert text.endswith(("？", "?")), "引导问题必须是问句"
    assert "没有唯一正确答案" in preference


def test_socratic_suggestion_helpers() -> None:
    suggestion = SocraticSuggestion(
        question="你如何推导？", question_type=SOCRATIC_PROCEDURAL, source="heuristic"
    )
    assert suggestion.has_answer is True
    assert suggestion.type_label == "程序性问题"

    preference = SocraticSuggestion(
        question="你会怎么选？", question_type=SOCRATIC_PREFERENCE, source="llm"
    )
    assert preference.has_answer is False


# --- LLM 结果解析（无 Agent 降级的关键环节） --------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"knowledge_point": "阿司匹林"}',
        '```json\n{"knowledge_point": "阿司匹林"}\n```',
        '好的，结果如下：{"knowledge_point": "阿司匹林"} 希望有帮助',
        '```\n{"knowledge_point": "阿司匹林"}\n```',
    ],
)
def test_parse_json_object_handles_common_shapes(text: str) -> None:
    assert parse_json_object(text) == {"knowledge_point": "阿司匹林"}


@pytest.mark.parametrize(
    "text", [None, "", "完全没有 JSON", "[1, 2, 3]", "{不是合法 JSON}"]
)
def test_parse_json_object_returns_none_for_invalid(text) -> None:
    assert parse_json_object(text) is None


# --- 知识点识别的降级路径 ---------------------------------------------------


class FakeLLM:
    """测试替身：模拟 LLM 客户端。"""

    def __init__(self, response: LLMResponse | None = None, available: bool = True) -> None:
        self._response = response or LLMResponse(ok=False, error="未配置")
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    def complete_json(self, system_prompt, user_prompt, **kwargs) -> LLMResponse:  # noqa: ANN001
        return self._response


def test_identify_uses_llm_when_available() -> None:
    client = FakeLLM(
        LLMResponse(
            ok=True,
            data={"knowledge_point": "阿司匹林的作用机制", "confidence": 0.9,
                  "alternatives": ["COX 抑制", "阿司匹林的作用机制"]},
        )
    )

    suggestion = identify_knowledge_point("阿司匹林为什么能解热镇痛", client=client)

    assert suggestion.source == "llm"
    assert suggestion.knowledge_point == "阿司匹林的作用机制"
    assert suggestion.confidence == 0.9
    assert suggestion.alternatives == ("COX 抑制",), "应去重并剔除与主选相同的项"
    assert suggestion.requires_confirmation is True


def test_identify_falls_back_when_llm_unavailable() -> None:
    """无 Agent 降级模式：LLM 未配置时仍能识别知识点。"""
    client = FakeLLM(available=False)

    suggestion = identify_knowledge_point("阿司匹林的作用机制是什么？", client=client)

    assert suggestion.source == "heuristic"
    assert suggestion.knowledge_point == "阿司匹林的作用机制"
    assert suggestion.llm_error is None, "未配置不算错误"


def test_identify_falls_back_when_llm_fails() -> None:
    client = FakeLLM(LLMResponse(ok=False, error="HTTP 500"))

    suggestion = identify_knowledge_point("阿司匹林的作用机制是什么？", client=client)

    assert suggestion.source == "heuristic"
    assert suggestion.knowledge_point == "阿司匹林的作用机制"
    assert suggestion.llm_error == "HTTP 500"


def test_identify_falls_back_when_llm_returns_empty_point() -> None:
    client = FakeLLM(LLMResponse(ok=True, data={"knowledge_point": "   "}))

    suggestion = identify_knowledge_point("阿司匹林的作用机制是什么？", client=client)

    assert suggestion.source == "heuristic"
    assert suggestion.knowledge_point == "阿司匹林的作用机制"


def test_identify_clamps_confidence() -> None:
    client = FakeLLM(
        LLMResponse(ok=True, data={"knowledge_point": "A", "confidence": 5})
    )
    assert identify_knowledge_point("A 是什么", client=client).confidence == 1.0

    client = FakeLLM(
        LLMResponse(ok=True, data={"knowledge_point": "A", "confidence": "不是数字"})
    )
    assert identify_knowledge_point("A 是什么", client=client).confidence == 0.8


def test_identify_empty_question_returns_unclassified() -> None:
    suggestion = identify_knowledge_point("   ", client=FakeLLM(available=False))

    assert suggestion.knowledge_point == UNCLASSIFIED_KNOWLEDGE_POINT
    assert suggestion.confidence == 0.0


# --- 引导问题生成的降级路径 -------------------------------------------------


def test_generate_socratic_uses_llm() -> None:
    client = FakeLLM(
        LLMResponse(
            ok=True,
            data={"socratic_question": "你觉得第一步应该做什么？", "question_type": "procedural"},
        )
    )

    result = generate_socratic_question("如何计算半衰期", "半衰期计算", client=client)

    assert result.source == "llm"
    assert result.question == "你觉得第一步应该做什么？"
    assert result.question_type == SOCRATIC_PROCEDURAL


def test_generate_socratic_falls_back_and_fixes_bad_type() -> None:
    """LLM 返回非法类型时，用本地规则判定，而不是把非法值写库。"""
    client = FakeLLM(
        LLMResponse(ok=True, data={"socratic_question": "你会怎么选？", "question_type": "bogus"})
    )

    result = generate_socratic_question("哪个更好？", "剂型选择", client=client)

    assert result.question == "你会怎么选？", "合法的问题文本应保留"
    assert result.question_type == SOCRATIC_PREFERENCE
    assert result.question_type in VALID_SOCRATIC_TYPES


def test_generate_socratic_falls_back_on_failure() -> None:
    client = FakeLLM(LLMResponse(ok=False, error="timeout"))

    result = generate_socratic_question("阿司匹林的作用机制是什么？", "阿司匹林的作用机制", client=client)

    assert result.source == "heuristic"
    assert result.question_type == SOCRATIC_PROCEDURAL
    assert "阿司匹林的作用机制" in result.question


# --- 组合分析（确认前的完整建议） -------------------------------------------


def test_analyze_question_returns_confirmable_suggestion() -> None:
    client = FakeLLM(available=False)

    analysis = analyze_question("阿司匹林的作用机制是什么？", client=client)

    assert analysis.question_text == "阿司匹林的作用机制是什么？"
    assert analysis.suggestion.knowledge_point == "阿司匹林的作用机制"
    assert analysis.requires_confirmation is True
    assert analysis.socratic.question_type == SOCRATIC_PROCEDURAL
    assert analysis.socratic.has_answer is True
