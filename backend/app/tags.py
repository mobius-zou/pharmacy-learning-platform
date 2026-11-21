"""标签体系（三维度）定义与校验。

- 专业课维度：药理学 / 药物化学 / 药剂学 / 药物分析（固定枚举）
- 章节维度：自由文本
- 考点类型维度：机制 / 结构 / 计算 / 应用 / 综合（固定枚举）
"""

from __future__ import annotations

from typing import Any

SUBJECTS: tuple[str, ...] = ("药理学", "药物化学", "药剂学", "药物分析")
POINT_TYPES: tuple[str, ...] = ("机制", "结构", "计算", "应用", "综合")

# 数据库中 tags JSON 字段的三个键
KEY_SUBJECTS = "subjects"
KEY_CHAPTERS = "chapters"
KEY_POINT_TYPES = "point_types"


class TagValidationError(ValueError):
    """标签不符合规范。"""


def empty_tags() -> dict[str, list[str]]:
    return {KEY_SUBJECTS: [], KEY_CHAPTERS: [], KEY_POINT_TYPES: []}


def _clean_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        raise TagValidationError(f"{field} 必须是字符串数组")

    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TagValidationError(f"{field} 的元素必须是字符串")
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return out


def normalize_tags(raw: Any) -> dict[str, list[str]]:
    """把任意输入规范化为 {subjects, chapters, point_types} 三个字符串数组。

    兼容历史/宽松输入：允许直接传字符串或 {"专业课": ...} 之类的键名。
    """
    if raw is None:
        return empty_tags()
    if not isinstance(raw, dict):
        raise TagValidationError("tags 必须是对象")

    aliases = {
        KEY_SUBJECTS: ("subjects", "subject", "专业课", "专业课维度"),
        KEY_CHAPTERS: ("chapters", "chapter", "章节", "章节维度"),
        KEY_POINT_TYPES: ("point_types", "point_type", "考点类型", "考点类型维度"),
    }

    result = empty_tags()
    for canonical, names in aliases.items():
        merged: list[str] = []
        for name in names:
            if name in raw:
                merged.extend(_clean_list(raw[name], canonical))
        # 去重且保序
        deduped: list[str] = []
        for item in merged:
            if item not in deduped:
                deduped.append(item)
        result[canonical] = deduped

    _validate_limits(result[KEY_SUBJECTS], "专业课维度")
    _validate_limits(result[KEY_CHAPTERS], "章节维度")
    _validate_limits(result[KEY_POINT_TYPES], "考点类型维度")
    _validate_enum(result[KEY_SUBJECTS], SUBJECTS, "专业课维度")
    _validate_enum(result[KEY_POINT_TYPES], POINT_TYPES, "考点类型维度")
    return result


# 标签数量与单值长度上限：防止一次塞入上万标签把 JSON 列和索引打爆。
# 长度按 DB 列宽（VARCHAR(200)）取值，而不是凭感觉收紧到 100 ——
# 知识点会被写成章节标签，而文献抽取出的知识点是整句英文（80~150 字符），
# 上限过小会让正常的「文献转卡片」直接失败。
MAX_TAGS_PER_DIMENSION = 50
MAX_TAG_LENGTH = 200


def _validate_limits(values: list[str], label: str) -> None:
    if len(values) > MAX_TAGS_PER_DIMENSION:
        raise TagValidationError(
            f"{label} 最多 {MAX_TAGS_PER_DIMENSION} 个标签，实际 {len(values)} 个"
        )
    for value in values:
        if len(value) > MAX_TAG_LENGTH:
            raise TagValidationError(f"{label} 的标签过长（上限 {MAX_TAG_LENGTH} 字符）：{value[:20]}…")


def _validate_enum(values: list[str], allowed: tuple[str, ...], label: str) -> None:
    invalid = [v for v in values if v not in allowed]
    if invalid:
        raise TagValidationError(
            f"{label} 取值非法：{'、'.join(invalid)}；可选值为 {'、'.join(allowed)}"
        )
