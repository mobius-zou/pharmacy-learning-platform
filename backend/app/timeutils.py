"""时间工具。

约定：**数据库中一律存储 naive UTC**，跨出服务层时一律为 aware UTC。
MySQL 的 DATETIME 不保存时区信息，py-fsrs 又强制要求 aware UTC，
因此所有读写边界都必须经过这里的转换函数，否则 FSRS 会抛 ValueError。
"""

from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    """当前 aware UTC 时间。"""
    return datetime.now(UTC)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """aware/naive -> naive UTC（写入数据库前调用）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        # 无时区信息时按 UTC 解释
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def to_aware_utc(value: datetime | None) -> datetime | None:
    """naive/aware -> aware UTC（从数据库读出后调用）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def days_between(start: datetime | None, end: datetime) -> float:
    """两个时间点之间的天数（非负）。start 为 None 时返回 0.0。"""
    if start is None:
        return 0.0
    s = to_aware_utc(start)
    e = to_aware_utc(end)
    if s is None or e is None:  # pragma: no cover - 已在上方判空
        return 0.0
    delta = (e - s).total_seconds() / 86400.0
    return delta if delta > 0 else 0.0
