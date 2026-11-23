"""add cards.chapters_json generated column and multivalued index

背景（H6）：卡片→知识点的关联查询按 ``tags.chapters`` 做成员匹配，
原谓词是 ``JSON_CONTAINS`` + ``CAST(...) LIKE`` + 正文 ``LIKE``，
在 2 万张卡片上实测 32.7ms（全表扫描，``key=ix_cards_user_id``）。

实测结论（``scripts/bench_exam_match_index.py``，同一数据集）：

| 方案 | 中位耗时 | 是否命中索引 |
| --- | --- | --- |
| baseline（JSON_EXTRACT 写法） | 32.7 ms | 否 |
| 生成列 ``chapters_text`` + LIKE ``%x%`` | 21.4 ms | 否（前导通配符无法用索引）|
| 生成列（JSON）+ 多值索引 + ``JSON_CONTAINS`` | **5.7 ms** | **是** |

因此本迁移只加「生成列 + 多值索引」：生成列让谓词退化为普通列上的
``JSON_CONTAINS``，从而被优化器识别的多值索引命中。

兼容性：多值索引需要 MySQL >= 8.0.17；更低版本会跳过建索引
（查询仍可用，只是退回全表扫描），并在日志中说明。

Revision ID: b7067745305e
Revises: 37865494d573
Create Date: 2026-11-23 12:18:00.000000+00:00
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7067745305e'
down_revision: Union[str, None] = '37865494d573'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

INDEX_NAME = "ix_cards_chapters_mv"

# 多值索引在 8.0.17 引入
_MIN_VERSION = (8, 0, 17)


def _supports_multivalued_index(conn) -> bool:
    raw = conn.execute(sa.text("SELECT VERSION()")).scalar() or ""
    # 形如 8.4.13-0ubuntu0.26.04.2 或 8.0.17
    core = raw.split("-")[0].split("+")[0]
    parts = []
    for piece in core.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts) >= _MIN_VERSION


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "cards",
        sa.Column(
            "chapters_json",
            sa.JSON(),
            sa.Computed("JSON_EXTRACT(tags,'$.chapters')", persisted=True),
            nullable=True,
        ),
    )

    if not _supports_multivalued_index(conn):
        logger.warning(
            "MySQL 版本低于 8.0.17，跳过多值索引 %s（查询仍可用，但会退回全表扫描）",
            INDEX_NAME,
        )
        return

    # MySQL 的多值索引只能通过 ALTER TABLE 建（表达式索引）。
    op.execute(
        f"ALTER TABLE cards ADD INDEX {INDEX_NAME} "
        "((CAST(chapters_json AS CHAR(200) ARRAY)))"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if _supports_multivalued_index(conn):
        conn.execute(sa.text(f"ALTER TABLE cards DROP INDEX {INDEX_NAME}"))
    op.drop_column("cards", "chapters_json")
