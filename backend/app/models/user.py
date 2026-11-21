"""用户模型。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..timeutils import utcnow
from ._common import MYSQL_TABLE_ARGS, UTCDateTime

if TYPE_CHECKING:
    from .card import Card
    from .exam_point import ExamPoint
    from .experiment import Experiment
    from .knowledge_graph import KnowledgeGraph
    from .wrong_question import WrongQuestion


class User(Base):
    __tablename__ = "users"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 令牌版本：签发 JWT 时写入，校验时比对。登出/改密时 +1 即可让旧令牌全部失效
    # （无状态 JWT 本身无法撤销，需要这样一个服务端锚点）。
    # server_default 必须与迁移一致（否则 alembic check 会报 drift）
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    cards: Mapped[list["Card"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )
    wrong_questions: Mapped[list["WrongQuestion"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )
    knowledge_graph: Mapped[list["KnowledgeGraph"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )
    experiments: Mapped[list["Experiment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )
    exam_points: Mapped[list["ExamPoint"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="select"
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<User id={self.id} username={self.username!r}>"
