"""知识图谱模型（阶段三：文献工作台）。

**本表只存放人工确认后的三元组。** AI 抽取结果的草稿保存在 ChromaDB
（``literature_drafts`` 集合）中，只有经过人工校对并调用确认接口后才会写入这里 ——
这是「AI 提取结果不能自动入库」这一硬性要求的落点：表内数据天然全部可信。

字段严格遵循既定数据模型：id, user_id, source_type, source_id, entity, relation, target。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._common import MYSQL_TABLE_ARGS

if TYPE_CHECKING:
    from .user import User

# 关系/实体名的长度上限（MySQL 索引与 utf8mb4 的折中）
MAX_TERM_LENGTH = 200


class KnowledgeGraph(Base):
    __tablename__ = "knowledge_graph"
    __table_args__ = (
        # 同一用户下同一三元组只能存在一条（并发/重试不再产生重复）
        UniqueConstraint("user_id", "fingerprint", name="uq_knowledge_graph_user_fingerprint"),
        MYSQL_TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 来源类型：pubmed / card / wrong_question / manual
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 来源标识：PubMed 的 PMID、卡片 id 等（统一按字符串存，便于跨来源扩展）
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # 三元组指纹（sha1），用于 (user_id, fingerprint) 唯一索引。
    # 直接用 entity/relation/target 建复合唯一索引会超出 MySQL 3072 字节上限。
    fingerprint: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    entity: Mapped[str] = mapped_column(String(MAX_TERM_LENGTH), nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String(MAX_TERM_LENGTH), nullable=False)
    target: Mapped[str] = mapped_column(String(MAX_TERM_LENGTH), nullable=False, index=True)

    user: Mapped["User"] = relationship(back_populates="knowledge_graph")

    def as_triple(self) -> tuple[str, str, str]:
        return (self.entity, self.relation, self.target)


def triple_fingerprint(entity: str, relation: str, target: str) -> str:
    """三元组指纹：sha1(entity|relation|target)。

    模型与迁移都要写这一列，因此放在模型模块里避免循环依赖。
    """
    import hashlib

    raw = f"{entity}|{relation}|{target}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


@event.listens_for(KnowledgeGraph, "before_insert")
def _fill_fingerprint(mapper, connection, target: KnowledgeGraph) -> None:  # noqa: ANN001
    """插入前自动补 fingerprint。

    这一列是 NOT NULL 且参与唯一约束，但调用方（测试、脚本、未来的新代码）
    不一定记得手动算。用事件统一兜底，避免「忘了填就 IntegrityError」。
    """
    if not target.fingerprint:
        target.fingerprint = triple_fingerprint(
            target.entity or "", target.relation or "", target.target or ""
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<KnowledgeGraph {self.entity} -{self.relation}-> {self.target}>"
