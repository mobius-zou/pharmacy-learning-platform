"""add last_error_at, kg fingerprint unique, review_log unique

同时处理三件事：

1. ``wrong_questions.last_error_at`` —— 语义修正（原先把 created_at 当最后做错时间）；
2. ``knowledge_graph.fingerprint`` + ``(user_id, fingerprint)`` 唯一约束 ——
   同一用户同一三元组不再重复（直接用三列建唯一索引会超出 MySQL 3072 字节上限，
   故改用 sha1 指纹）；
3. ``review_logs (card_id, reviewed_at)`` 唯一约束 —— 评分幂等护栏。

迁移在**非空表**上必须安全，因此统一采用
「加可空列 → 回填 → 去重 → 收紧约束」，而不是直接 ``ADD COLUMN ... NOT NULL``。

Revision ID: b87c8360a426
Revises: c7be064aaffe
Create Date: 2026-10-25 07:02:55.171006+00:00
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = 'b87c8360a426'
down_revision: Union[str, None] = 'c7be064aaffe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables():
    """轻量 table 定义（不依赖应用模型，避免模型演进影响历史迁移）。"""
    wrong_questions = sa.table(
        "wrong_questions",
        sa.column("id", sa.Integer),
        sa.column("created_at", sa.DateTime),
        sa.column("last_error_at", sa.DateTime),
    )
    knowledge_graph = sa.table(
        "knowledge_graph",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("entity", sa.String),
        sa.column("relation", sa.String),
        sa.column("target", sa.String),
        sa.column("fingerprint", sa.String),
    )
    review_logs = sa.table(
        "review_logs",
        sa.column("id", sa.Integer),
        sa.column("card_id", sa.Integer),
        sa.column("reviewed_at", sa.DateTime),
    )
    return wrong_questions, knowledge_graph, review_logs


def _dedupe(conn, table, key_columns) -> None:
    """按 key_columns 分组，每组只保留最小 id，删除其余重复行。"""
    keep = sa.func.min(table.c.id).label("keep_id")
    stmt = (
        sa.select(*[table.c[col] for col in key_columns], keep)
        .group_by(*[table.c[col] for col in key_columns])
        .having(sa.func.count() > 1)
    )
    for row in conn.execute(stmt).all():
        values = row[:-1]
        keep_id = row[-1]
        conditions = [table.c[col] == value for col, value in zip(key_columns, values)]
        conn.execute(sa.delete(table).where(*conditions, table.c.id != keep_id))


def upgrade() -> None:
    conn = op.get_bind()
    wrong_questions, knowledge_graph, review_logs = _tables()

    # --- 1) last_error_at：加可空列后回填 ---
    op.add_column(
        "wrong_questions",
        sa.Column(
            "last_error_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
    )
    conn.execute(
        wrong_questions.update().values(last_error_at=wrong_questions.c.created_at)
    )

    # --- 2) knowledge_graph.fingerprint：加可空列 → 回填 → 去重 → 收紧 ---
    op.add_column(
        "knowledge_graph", sa.Column("fingerprint", sa.String(length=40), nullable=True)
    )
    conn.execute(
        knowledge_graph.update().values(
            fingerprint=sa.func.sha1(
                sa.func.concat(
                    knowledge_graph.c.entity,
                    "|",
                    knowledge_graph.c.relation,
                    "|",
                    knowledge_graph.c.target,
                )
            )
        )
    )
    _dedupe(conn, knowledge_graph, ["user_id", "fingerprint"])
    op.alter_column(
        "knowledge_graph",
        "fingerprint",
        existing_type=sa.String(length=40),
        nullable=False,
    )
    op.create_index(
        op.f("ix_knowledge_graph_fingerprint"),
        "knowledge_graph",
        ["fingerprint"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_knowledge_graph_user_fingerprint",
        "knowledge_graph",
        ["user_id", "fingerprint"],
    )

    # --- 3) review_logs 去重后加唯一约束（评分幂等） ---
    _dedupe(conn, review_logs, ["card_id", "reviewed_at"])
    op.create_unique_constraint(
        "uq_review_log_card_time", "review_logs", ["card_id", "reviewed_at"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_review_log_card_time", "review_logs", type_="unique")
    op.drop_constraint(
        "uq_knowledge_graph_user_fingerprint", "knowledge_graph", type_="unique"
    )
    op.drop_index(op.f("ix_knowledge_graph_fingerprint"), table_name="knowledge_graph")
    op.drop_column("knowledge_graph", "fingerprint")
    op.drop_column("wrong_questions", "last_error_at")
