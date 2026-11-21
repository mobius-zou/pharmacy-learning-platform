"""偏差回溯规则引擎单元测试（不需要数据库、不需要 AI）。

这是阶段四的核心交付：**匹配条件 = 偏差类型相同 + 实验类型相同**（必要条件），
再按药物/试剂/仪器相似度排序。全部为纯函数，可离线确定性验证。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import experiment_service as exp

BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def facts(
    experiment_id: int = 1,
    experiment_type: str = "合成",
    drug_name: str = "阿司匹林",
    deviation_type: str = "试剂问题",
    deviation_description: str = "试剂受潮",
    deviation_handling: str = "",
    created_at: datetime | None = None,
    reagents: tuple[exp.ReagentFact, ...] = (),
    instruments: tuple[str, ...] = (),
) -> exp.ExperimentFacts:
    return exp.ExperimentFacts(
        experiment_id=experiment_id,
        experiment_type=experiment_type,
        drug_name=drug_name,
        deviation_type=deviation_type,
        deviation_description=deviation_description,
        deviation_handling=deviation_handling,
        created_at=created_at or BASE_TIME,
        reagents=reagents,
        instruments=instruments,
    )


# --- 归一化 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("阿司匹林", "阿司匹林"),
        ("  阿司匹林  ", "阿司匹林"),
        ("阿司匹林（原料药）", "阿司匹林"),
        ("Aspirin (API)", "aspirin"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_name(raw, expected: str) -> None:
    assert exp.normalize_name(raw) == expected


def test_normalize_reagent_list_accepts_dicts_and_strings() -> None:
    reagents = exp.normalize_reagent_list(
        [
            {"name": "盐酸", "lot": "20240101"},
            "氢氧化钠|20240202",
            "乙醇",
            {"试剂": "丙酮", "批号": "20240303"},
        ]
    )

    assert [(r.name, r.lot) for r in reagents] == [
        ("盐酸", "20240101"),
        ("氢氧化钠", "20240202"),
        ("乙醇", ""),
        ("丙酮", "20240303"),
    ]


def test_normalize_reagent_list_deduplicates_and_skips_blank() -> None:
    reagents = exp.normalize_reagent_list(["盐酸", "盐酸", "  ", {"name": ""}, None])

    assert [r.name for r in reagents] == ["盐酸"]


def test_normalize_reagent_list_handles_single_values() -> None:
    assert [r.name for r in exp.normalize_reagent_list("盐酸")] == ["盐酸"]
    assert [r.name for r in exp.normalize_reagent_list({"name": "盐酸"})] == ["盐酸"]
    assert exp.normalize_reagent_list(None) == []


def test_normalize_instrument_list() -> None:
    names = exp.normalize_instrument_list(
        [{"name": "HPLC", "calibration": "2024-01"}, "天平", None]
    )
    assert names == ["HPLC", "天平"]


# --- 必要条件：实验类型 + 偏差类型 -----------------------------------------


def test_same_type_and_deviation_matches() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")

    match = exp.evaluate_match(facts(), query)

    assert match is not None
    assert "实验类型相同" in match.reasons
    assert "偏差类型相同" in match.reasons


def test_different_experiment_type_does_not_match() -> None:
    """必要条件 1：实验类型必须相同。"""
    query = exp.DeviationQuery(experiment_type="分析", deviation_type="试剂问题")

    assert exp.evaluate_match(facts(experiment_type="合成"), query) is None


def test_different_deviation_type_does_not_match() -> None:
    """必要条件 2：偏差类型必须相同。"""
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="仪器异常")

    assert exp.evaluate_match(facts(deviation_type="试剂问题"), query) is None


@pytest.mark.parametrize(
    "deviation_type", ["操作误差", "试剂问题", "仪器异常", "方案设计缺陷"]
)
def test_each_deviation_type_matches_itself_only(deviation_type: str) -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type=deviation_type)

    assert exp.evaluate_match(facts(deviation_type=deviation_type), query) is not None
    for other in exp.DEVIATION_TYPES:
        if other == deviation_type:
            continue
        assert exp.evaluate_match(facts(deviation_type=other), query) is None


def test_missing_deviation_type_in_query_matches_all_types() -> None:
    """进入模板尚未发生偏差时，只按实验类型召回（用于主动提示）。"""
    query = exp.DeviationQuery(experiment_type="合成", deviation_type=None)

    for deviation_type in exp.DEVIATION_TYPES:
        match = exp.evaluate_match(facts(deviation_type=deviation_type), query)
        assert match is not None
        assert "偏差类型相同" not in match.reasons


def test_excluded_experiment_never_matches_itself() -> None:
    query = exp.DeviationQuery(
        experiment_type="合成", deviation_type="试剂问题", exclude_experiment_id=7
    )

    assert exp.evaluate_match(facts(experiment_id=7), query) is None
    assert exp.evaluate_match(facts(experiment_id=8), query) is not None


def test_normalized_type_comparison() -> None:
    """类型比较前后空白不应影响判定。"""
    query = exp.DeviationQuery(experiment_type=" 合成 ", deviation_type=" 试剂问题 ")

    assert exp.evaluate_match(facts(), query) is not None


# --- 相关度排序 -------------------------------------------------------------


def test_same_drug_scores_higher_than_different_drug() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题", drug_name="阿司匹林")

    same = exp.evaluate_match(facts(experiment_id=1), query)
    other = exp.evaluate_match(facts(experiment_id=2, drug_name="布洛芬"), query)

    assert same is not None and other is not None
    assert same.relevance > other.relevance
    assert same.is_same_drug is True
    assert other.is_same_drug is False


def test_partial_drug_name_scores_between_exact_and_unrelated() -> None:
    query = exp.DeviationQuery(
        experiment_type="合成", deviation_type="试剂问题", drug_name="阿司匹林片"
    )

    exact = exp.evaluate_match(facts(experiment_id=1, drug_name="阿司匹林片"), query)
    partial = exp.evaluate_match(facts(experiment_id=2, drug_name="阿司匹林"), query)
    unrelated = exp.evaluate_match(facts(experiment_id=3, drug_name="布洛芬"), query)

    assert exact and partial and unrelated
    assert exact.relevance > partial.relevance > unrelated.relevance


def test_shared_reagent_adds_relevance() -> None:
    query = exp.DeviationQuery(
        experiment_type="合成",
        deviation_type="试剂问题",
        reagents=(exp.ReagentFact("盐酸", "L1"),),
    )

    shared = exp.evaluate_match(facts(reagents=(exp.ReagentFact("盐酸", "L1"),)), query)
    none_shared = exp.evaluate_match(
        facts(reagents=(exp.ReagentFact("硫酸", "L9"),)), query
    )

    assert shared and none_shared
    assert shared.relevance > none_shared.relevance
    assert any("共用试剂" in reason for reason in shared.reasons)


def test_same_reagent_lot_adds_extra_relevance() -> None:
    query = exp.DeviationQuery(
        experiment_type="合成",
        deviation_type="试剂问题",
        reagents=(exp.ReagentFact("盐酸", "L1"),),
    )

    same_lot = exp.evaluate_match(facts(reagents=(exp.ReagentFact("盐酸", "L1"),)), query)
    other_lot = exp.evaluate_match(facts(reagents=(exp.ReagentFact("盐酸", "L2"),)), query)

    assert same_lot and other_lot
    assert same_lot.relevance > other_lot.relevance
    assert any("批号相同" in reason for reason in same_lot.reasons)
    assert not any("批号相同" in reason for reason in other_lot.reasons)


def test_shared_instrument_adds_relevance() -> None:
    query = exp.DeviationQuery(
        experiment_type="分析", deviation_type="仪器异常", instruments=("HPLC",)
    )

    shared = exp.evaluate_match(
        facts(experiment_type="分析", deviation_type="仪器异常", instruments=("HPLC",)), query
    )
    none_shared = exp.evaluate_match(
        facts(experiment_type="分析", deviation_type="仪器异常", instruments=("天平",)), query
    )

    assert shared and none_shared
    assert shared.relevance > none_shared.relevance


def test_handling_present_adds_relevance() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")

    with_handling = exp.evaluate_match(facts(deviation_handling="更换批号"), query)
    without = exp.evaluate_match(facts(deviation_handling=""), query)

    assert with_handling and without
    assert with_handling.relevance > without.relevance
    assert any("处理方式" in reason for reason in with_handling.reasons)


def test_match_levels() -> None:
    """等级由相关度阈值决定：仅必要条件=low，药物相近=medium，药物相同=high。"""
    query = exp.DeviationQuery(
        experiment_type="合成", deviation_type="试剂问题", drug_name="阿司匹林片"
    )

    low = exp.evaluate_match(facts(experiment_id=1, drug_name="布洛芬"), query)
    medium = exp.evaluate_match(facts(experiment_id=2, drug_name="阿司匹林"), query)
    high = exp.evaluate_match(facts(experiment_id=3, drug_name="阿司匹林片"), query)

    assert low and low.level == "low"
    assert medium and medium.level == "medium"
    assert high and high.level == "high"
    assert low.relevance < medium.relevance < high.relevance


def test_rank_orders_by_relevance_then_recency() -> None:
    query = exp.DeviationQuery(
        experiment_type="合成", deviation_type="试剂问题", drug_name="阿司匹林"
    )
    older_same_drug = facts(experiment_id=1, created_at=BASE_TIME)
    newer_other_drug = facts(
        experiment_id=2, drug_name="布洛芬", created_at=BASE_TIME + timedelta(days=5)
    )

    ranked = exp.rank_deviation_matches([older_same_drug, newer_other_drug], query)

    assert [m.experiment_id for m in ranked] == [1, 2], "相关度优先于时间"


def test_rank_tie_breaks_by_recency_then_id() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")
    old = facts(experiment_id=1, created_at=BASE_TIME)
    new = facts(experiment_id=2, created_at=BASE_TIME + timedelta(days=3))
    same_time_lower_id = facts(experiment_id=3, created_at=BASE_TIME + timedelta(days=3))

    ranked = exp.rank_deviation_matches([old, new, same_time_lower_id], query)

    assert [m.experiment_id for m in ranked] == [3, 2, 1]


def test_rank_is_stable_regardless_of_input_order() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")
    items = [facts(experiment_id=i, created_at=BASE_TIME + timedelta(days=i)) for i in range(5)]

    forward = [m.experiment_id for m in exp.rank_deviation_matches(items, query)]
    backward = [m.experiment_id for m in exp.rank_deviation_matches(list(reversed(items)), query)]

    assert forward == backward


def test_rank_respects_limit_and_zero_means_all() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")
    items = [facts(experiment_id=i) for i in range(1, 8)]

    assert len(exp.rank_deviation_matches(items, query, limit=3)) == 3
    assert len(exp.rank_deviation_matches(items, query, limit=0)) == 7


def test_rank_filters_out_non_matching_entries() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")
    items = [
        facts(experiment_id=1, experiment_type="合成", deviation_type="试剂问题"),
        facts(experiment_id=2, experiment_type="分析", deviation_type="试剂问题"),
        facts(experiment_id=3, experiment_type="合成", deviation_type="仪器异常"),
    ]

    ranked = exp.rank_deviation_matches(items, query)

    assert [m.experiment_id for m in ranked] == [1]


# --- 回溯汇总与提示文案 -----------------------------------------------------


def test_build_traceback_counts_only_same_experiment_type() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")
    items = [
        facts(experiment_id=1, experiment_type="合成", deviation_type="试剂问题"),
        facts(experiment_id=2, experiment_type="合成", deviation_type="仪器异常"),
        facts(experiment_id=3, experiment_type="分析", deviation_type="仪器异常"),
    ]

    result = exp.build_traceback(items, query)

    assert result.total_scanned == 3
    assert result.total == 1, "只有偏差类型与实验类型同时相同的才命中"
    assert result.deviation_type_counts == {"试剂问题": 1, "仪器异常": 1}, "分布只统计同实验类型"
    assert result.top_deviation_type in {"试剂问题", "仪器异常"}


def test_build_traceback_empty() -> None:
    result = exp.build_traceback([], exp.DeviationQuery(experiment_type="合成"))

    assert result.total == 0
    assert result.matches == []
    assert result.top_deviation_type is None


def test_build_advice_without_history() -> None:
    result = exp.TracebackResult()

    text = exp.build_advice(result, "合成")

    assert "暂无" in text
    assert "合成" in text


def test_build_advice_reports_top_deviation_and_reusable_handling() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题")
    items = [
        facts(experiment_id=1, deviation_handling="更换批号后复测正常"),
        facts(experiment_id=2),
    ]
    result = exp.build_traceback(items, query)

    text = exp.build_advice(result, "合成")

    assert "2 条" in text
    assert "试剂问题" in text
    assert "更换批号后复测正常" in text


def test_build_advice_highlights_same_drug() -> None:
    query = exp.DeviationQuery(experiment_type="合成", deviation_type="试剂问题", drug_name="阿司匹林")
    result = exp.build_traceback([facts(drug_name="阿司匹林")], query)

    text = exp.build_advice(result, "合成")

    assert "同药物" in text


# --- 模板 -------------------------------------------------------------------


def test_all_four_experiment_types_have_templates() -> None:
    templates = exp.list_templates()

    assert [t.experiment_type for t in templates] == list(exp.EXPERIMENT_TYPES)
    for template in templates:
        assert template.label
        assert template.procedure_steps
        assert template.key_parameters
        assert template.risk_points, "每个模板都应给出风险点"


def test_get_template_unknown_returns_none() -> None:
    assert exp.get_template("不存在的类型") is None


# --- 输入校验（校验发生在写库之前，故可传 db=None 做纯校验测试） ------------


def test_create_rejects_unknown_experiment_type() -> None:
    with pytest.raises(ValueError, match="实验类型"):
        exp.create_experiment(None, 1, "炼金术", "阿司匹林")  # type: ignore[arg-type]


def test_create_rejects_unknown_deviation_type() -> None:
    with pytest.raises(ValueError, match="偏差类型"):
        exp.create_experiment(
            None, 1, "合成", "阿司匹林", deviation_type="心情不好"  # type: ignore[arg-type]
        )


def test_create_requires_deviation_type_when_description_given() -> None:
    """有偏差描述却没有类型，会导致回溯引擎无法匹配，必须拒绝。"""
    with pytest.raises(ValueError, match="必须选择偏差类型"):
        exp.create_experiment(
            None, 1, "合成", "阿司匹林", deviation_description="试剂受潮"  # type: ignore[arg-type]
        )
