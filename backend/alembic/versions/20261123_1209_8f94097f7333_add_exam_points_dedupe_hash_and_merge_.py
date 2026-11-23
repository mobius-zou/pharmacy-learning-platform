"""add exam_points.dedupe_hash and merge duplicate imports

同一条考点被重复导入时不应产生第二行。本迁移：

1. 增加 ``dedupe_hash``（``dedupe_key`` 的 sha256，64 位十六进制）；
2. 回填历史行（按与 ``exam_match_service.dedupe_key`` **完全相同**的规则）；
3. **先合并**已有的重复行（保留 id 最小的一行，采用最新行的匹配结果），
   否则第 4 步的唯一约束会因现存重复而创建失败；
4. 收紧为 NOT NULL 并建 ``(user_id, dedupe_hash)`` 唯一约束。

规范化规则（与运行时一致）：NFKC 统一全半角 → 去首尾空白 → 小写 → 压缩连续空白。
迁移文件不导入应用代码，因此在本文件内保留一份实现；
``tests/test_exam_dedupe.py`` 会断言两份实现逐字符一致。

Revision ID: 8f94097f7333
Revises: b87c8360a426
Create Date: 2026-11-23 12:09:00.739297+00:00
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8f94097f7333'
down_revision: Union[str, None] = 'b87c8360a426'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _dedupe_key(text: str) -> str:
    """与 ``app.services.exam_match_service.dedupe_key`` 保持一致。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", normalized.strip().lower())


def _dedupe_hash(text: str) -> str:
    return hashlib.sha256(_dedupe_key(text).encode("utf-8")).hexdigest()


def upgrade() -> None:
    conn = op.get_bind()
    exam_points = sa.table(
        "exam_points",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("point_text", sa.Text),
        sa.column("matched_knowledge_point", sa.String),
        sa.column("priority", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("dedupe_hash", sa.String),
    )

    op.add_column("exam_points", sa.Column("dedupe_hash", sa.String(length=64), nullable=True))

    # --- 1) 回填：逐行计算哈希 ---
    rows = conn.execute(
        sa.select(exam_points.c.id, exam_points.c.point_text)
    ).all()
    for row_id, point_text in rows:
        conn.execute(
            exam_points.update()
            .where(exam_points.c.id == row_id)
            .values(dedupe_hash=_dedupe_hash(point_text or ""))
        )

    # --- 2) 合并重复：同 (user_id, hash) 只保留 id 最小的一行 ---
    # 采用 created_at 最新那行的匹配结果与原文（更贴近「最近一次导入」的事实）
    dup_rows = conn.execute(
        sa.select(exam_points.c.user_id, exam_points.c.dedupe_hash)
        .group_by(exam_points.c.user_id, exam_points.c.dedupe_hash)
        .having(sa.func.count() > 1)
    ).all()
    for user_id, digest in dup_rows:
        group = conn.execute(
            sa.select(
                exam_points.c.id,
                exam_points.c.point_text,
                exam_points.c.matched_knowledge_point,
                exam_points.c.priority,
            )
            .where(
                exam_points.c.user_id == user_id,
                exam_points.c.dedupe_hash == digest,
            )
            .order_by(exam_points.c.id)
        ).all()
        keep = group[0]
        latest = group[-1]
        conn.execute(
            exam_points.update()
            .where(exam_points.c.id == keep.id)
            .values(
                point_text=latest.point_text,
                matched_knowledge_point=latest.matched_knowledge_point,
                priority=latest.priority,
            )
        )
        conn.execute(
            sa.delete(exam_points).where(
                exam_points.c.user_id == user_id,
                exam_points.c.dedupe_hash == digest,
                exam_points.c.id != keep.id,
            )
        )

    # --- 3) 收紧约束 ---
    op.alter_column(
        "exam_points",
        "dedupe_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_exam_points_user_dedupe", "exam_points", ["user_id", "dedupe_hash"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_exam_points_user_dedupe", "exam_points", type_="unique")
    op.drop_column("exam_points", "dedupe_hash")
