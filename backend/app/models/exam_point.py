"""考点模型（阶段五：考点匹配）。

字段严格遵循既定数据模型：id, user_id, point_text, matched_knowledge_point,
priority, created_at —— 本阶段**未新增任何列**。

``priority`` 取值：``高危`` / ``巩固`` / ``确认``。
``matched_knowledge_point`` 在知识图谱中找不到对应知识点时为空。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..timeutils import utcnow
from ._common import MYSQL_TABLE_ARGS, UTCDateTime

if TYPE_CHECKING:
    from .user import User

# 复习优先级：高危考点（学生薄弱）/ 巩固考点（掌握一般）/ 确认考点（已掌握）
PRIORITY_HIGH = "高危"
PRIORITY_CONSOLIDATE = "巩固"
PRIORITY_CONFIRM = "确认"

PRIORITIES: tuple[str, ...] = (PRIORITY_HIGH, PRIORITY_CONSOLIDATE, PRIORITY_CONFIRM)

# 排序权重：数字越小越靠前
PRIORITY_ORDER: dict[str, int] = {
    PRIORITY_HIGH: 0,
    PRIORITY_CONSOLIDATE: 1,
    PRIORITY_CONFIRM: 2,
}


class ExamPoint(Base):
    __tablename__ = "exam_points"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 考点原文（已切分出的单条考点）
    point_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 匹配到的知识点（知识图谱中的实体/知识点）；未匹配时为空
    matched_knowledge_point: Mapped[str | None] = mapped_column(
        String(200), nullable=True, index=True
    )
    # 复习优先级：高危 / 巩固 / 确认
    priority: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, index=True
    )

    user: Mapped["User"] = relationship(back_populates="exam_points")

    @property
    def is_matched(self) -> bool:
        return bool(self.matched_knowledge_point)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<ExamPoint id={self.id} priority={self.priority!r} "
            f"kp={self.matched_knowledge_point!r}>"
        )
