"""标签体系单元测试（三维度）。"""

from __future__ import annotations

import pytest

from app.tags import (
    KEY_CHAPTERS,
    KEY_POINT_TYPES,
    KEY_SUBJECTS,
    POINT_TYPES,
    SUBJECTS,
    TagValidationError,
    empty_tags,
    normalize_tags,
)


def test_empty_tags_has_three_dimensions() -> None:
    assert empty_tags() == {KEY_SUBJECTS: [], KEY_CHAPTERS: [], KEY_POINT_TYPES: []}


def test_none_becomes_empty_tags() -> None:
    assert normalize_tags(None) == empty_tags()


def test_normalizes_full_three_dimension_input() -> None:
    result = normalize_tags(
        {
            "subjects": ["药理学"],
            "chapters": ["第三章 受体"],
            "point_types": ["机制", "综合"],
        }
    )

    assert result[KEY_SUBJECTS] == ["药理学"]
    assert result[KEY_CHAPTERS] == ["第三章 受体"]
    assert result[KEY_POINT_TYPES] == ["机制", "综合"]


def test_accepts_single_string_values() -> None:
    result = normalize_tags({"subjects": "药剂学", "point_types": "计算"})

    assert result[KEY_SUBJECTS] == ["药剂学"]
    assert result[KEY_POINT_TYPES] == ["计算"]


def test_accepts_chinese_alias_keys() -> None:
    result = normalize_tags({"专业课": ["药物分析"], "章节": ["绪论"], "考点类型": ["应用"]})

    assert result[KEY_SUBJECTS] == ["药物分析"]
    assert result[KEY_CHAPTERS] == ["绪论"]
    assert result[KEY_POINT_TYPES] == ["应用"]


def test_deduplicates_and_strips_whitespace() -> None:
    result = normalize_tags({"subjects": [" 药理学 ", "药理学", "药剂学"]})

    assert result[KEY_SUBJECTS] == ["药理学", "药剂学"]


def test_missing_dimensions_are_filled_with_empty_lists() -> None:
    result = normalize_tags({"subjects": ["药理学"]})

    assert set(result) == {KEY_SUBJECTS, KEY_CHAPTERS, KEY_POINT_TYPES}
    assert result[KEY_CHAPTERS] == []
    assert result[KEY_POINT_TYPES] == []


def test_chapters_are_free_text() -> None:
    result = normalize_tags({"chapters": ["随便什么章节名", "第 2 章 - 药动学"]})

    assert result[KEY_CHAPTERS] == ["随便什么章节名", "第 2 章 - 药动学"]


def test_all_subject_values_are_accepted() -> None:
    result = normalize_tags({"subjects": list(SUBJECTS)})
    assert result[KEY_SUBJECTS] == list(SUBJECTS)


def test_all_point_type_values_are_accepted() -> None:
    result = normalize_tags({"point_types": list(POINT_TYPES)})
    assert result[KEY_POINT_TYPES] == list(POINT_TYPES)


def test_invalid_subject_rejected() -> None:
    with pytest.raises(TagValidationError, match="专业课维度"):
        normalize_tags({"subjects": ["内科学"]})


def test_invalid_point_type_rejected() -> None:
    with pytest.raises(TagValidationError, match="考点类型维度"):
        normalize_tags({"point_types": ["背诵"]})


def test_non_dict_rejected() -> None:
    with pytest.raises(TagValidationError):
        normalize_tags(["药理学"])


def test_non_string_element_rejected() -> None:
    with pytest.raises(TagValidationError):
        normalize_tags({"chapters": [123]})
