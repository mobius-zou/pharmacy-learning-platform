"""错题模型（阶段二：错题诊断）。

说明（对既定数据模型的一处扩展）：
既定字段中没有「苏格拉底问题类型」的位置，但问题类型决定了交互行为 ——
程序性问题/判断性问题**有答案**（答完才能显示解析），偏好性问题**无答案**。
前端在读取错题时必须能拿到该类型，否则无法决定是否展示解析，因此在本表内
增列 ``socratic_question_type``（表数量仍为一张新表，未新增其他表）。
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


class WrongQuestion(Base):
    __tablename__ = "wrong_questions"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 知识点：薄弱点聚合与卡片关联都以它为准
    knowledge_point: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # 最近一次「做错」的时间。此前用 created_at 顶替，语义错误：
    # 一条错题被反复做错时，创建时间不会变，薄弱点排序与展示都会失真。
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # 苏格拉底式问答
    socratic_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    socratic_question_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    user_answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="wrong_questions")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<WrongQuestion id={self.id} kp={self.knowledge_point!r} "
            f"errors={self.error_count}>"
        )
