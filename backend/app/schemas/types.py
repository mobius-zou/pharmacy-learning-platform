"""Schema 公共类型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import PlainSerializer

from ..limits import (
    MAX_BULK_TEXT_LENGTH,
    MAX_CONTENT_LENGTH,
    MAX_NAME_LENGTH,
    MAX_SHORT_TEXT_LENGTH,
)
from ..timeutils import to_aware_utc


def _serialize_utc(value: datetime | None) -> str | None:
    """输出带时区偏移的 ISO 8601 字符串，避免前端把 naive 时间当本地时间。"""
    aware = to_aware_utc(value)
    return None if aware is None else aware.isoformat()


# 响应中所有时间字段统一为 ISO 8601（UTC，带 +00:00）
UTCDateTime = Annotated[
    datetime, PlainSerializer(_serialize_utc, return_type=str | None, when_used="json")
]


def tags_json_schema() -> dict[str, Any]:
    """标签对象的 OpenAPI 描述。"""
    return {
        "type": "object",
        "properties": {
            "subjects": {
                "type": "array",
                "items": {"type": "string", "enum": ["药理学", "药物化学", "药剂学", "药物分析"]},
                "title": "专业课维度",
            },
            "chapters": {
                "type": "array",
                "items": {"type": "string"},
                "title": "章节维度（自由文本）",
            },
            "point_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["机制", "结构", "计算", "应用", "综合"]},
                "title": "考点类型维度",
            },
        },
    }
