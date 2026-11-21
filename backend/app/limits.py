"""文本长度上限（批次 4）。

为什么单独成模块
----------------
长度上限同时被 **API Schema**（校验请求）与 **服务层解析器**（批量导入按行切分）
使用。放在 schemas 里会让 service 反向依赖 schemas，因此抽成中立模块。

字符数 vs 字节数
----------------
MySQL ``TEXT`` 的上限是 **65,535 字节**，不是字符。utf8mb4 下中文按 3 字节、
emoji 按 4 字节，所以 20,000 字符的中文最坏情况约 80KB，**会超**。
这里的上限按「最坏 4 字节/字符」保守折算，确保不会撞上 TEXT 的字节墙。
"""

from __future__ import annotations

# MySQL TEXT = 65535 字节；按最坏 4 字节/字符折算 => 约 16,383 字符。
# 取 10,000 留足余量（含转义/索引开销）。
MAX_CONTENT_LENGTH = 10_000        # 单条题干、操作步骤、结果、偏差描述等
MAX_SHORT_TEXT_LENGTH = 4_000      # 单条考点/知识点等短文本
MAX_NAME_LENGTH = 200              # 名称类字段
# 批量粘贴文本本身不落库（会被切分成多行），可以放宽
MAX_BULK_TEXT_LENGTH = 100_000


def clamp_text(value: str | None, limit: int) -> tuple[str, bool]:
    """把文本裁剪到上限内，返回 ``(结果, 是否发生了裁剪)``。

    用于批量导入：单行过长时截断而不是让整条插入失败
    （MySQL 会直接抛 DataError，最终变成 500）。
    """
    if not value:
        return "", False
    if len(value) <= limit:
        return value, False
    return value[:limit], True


# LIKE 里的通配符：用户输入若含 % 或 _，不转义会变成「匹配全部」
LIKE_ESCAPE_CHAR = "\\"


def escape_like(value: str) -> str:
    """转义 LIKE 通配符，防止用户输入的 ``%``/``_`` 扩大匹配范围。

    配合 ``Column.like(pattern, escape=LIKE_ESCAPE_CHAR)`` 使用。
    例：知识点输入 ``%`` 时，未转义会匹配**所有**卡片，
    从而伪造出一个「关联全部卡片」的假薄弱点，并触发全表扫描。
    """
    if not value:
        return ""
    return (
        value.replace(LIKE_ESCAPE_CHAR, LIKE_ESCAPE_CHAR * 2)
        .replace("%", f"{LIKE_ESCAPE_CHAR}%")
        .replace("_", f"{LIKE_ESCAPE_CHAR}_")
    )


def like_contains(value: str) -> tuple[str, str]:
    """返回 (pattern, escape char)，用于「包含」查询。"""
    return f"%{escape_like(value)}%", LIKE_ESCAPE_CHAR


__all__ = [
    "LIKE_ESCAPE_CHAR",
    "escape_like",
    "like_contains",
    "MAX_BULK_TEXT_LENGTH",
    "MAX_CONTENT_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_SHORT_TEXT_LENGTH",
    "clamp_text",
]
