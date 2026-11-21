"""复习日志模型。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, SmallInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._common import MYSQL_TABLE_ARGS, UTCDateTime

if TYPE_CHECKING:
    from .card import Card


class ReviewLog(Base):
    __tablename__ = "review_logs"
    __table_args__ = (
        # 幂等护栏：同一张卡在同一时刻只能落一条评分记录，
        # 双击/重试不会重复推进 FSRS。
        UniqueConstraint("card_id", "reviewed_at", name="uq_review_log_card_time"),
        MYSQL_TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # py-fsrs Rating：1=Again(忘了) 2=Hard(困难) 3=Good(一般) 4=Easy(简单)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    # 距上一次复习的天数（首次复习为 0）。py-fsrs 6.x 的 ReviewLog 不再携带该字段，
    # 故在本平台自行计算后落库，供后续统计/参数优化使用。
    elapsed_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    card: Mapped["Card"] = relationship(back_populates="review_logs")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ReviewLog id={self.id} card_id={self.card_id} rating={self.rating}>"
