"""卡片模型。

说明（对既定数据模型的必要扩展）：
既定字段 fsrs_stability / fsrs_difficulty / fsrs_due / fsrs_last_review 不足以还原
py-fsrs 的 Card —— FSRS-6 的调度分支依赖 card.state（Learning/Review/Relearning）
与 card.step（学习步进）。若丢弃这两项，一张处于 Review 态的卡片会被误当作新卡
重建，due 计算将完全错误。因此在本表内增列 fsrs_state、fsrs_step（表数量仍为三张）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Computed, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..tags import empty_tags
from ..timeutils import utcnow
from ._common import MYSQL_TABLE_ARGS, UTCDateTime

if TYPE_CHECKING:
    from .review_log import ReviewLog
    from .user import User


class Card(Base):
    __tablename__ = "cards"
    __table_args__ = MYSQL_TABLE_ARGS

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=empty_tags)
    # 生成列：把 chapters 数组抽出来，供**多值索引**（MySQL 8.0.17+）使用。
    # 为什么需要它：卡片→知识点的关联查询要按 chapters 做成员匹配，
    # 而 JSON_EXTRACT 的常规写法用不上索引（实测见 scripts/bench_exam_match_index.py：
    # 命中索引 5.7ms vs 全表扫描 32.7ms）。生成列让谓词退化为普通列上的
    # JSON_CONTAINS，从而能被优化器识别。
    chapters_json: Mapped[Any | None] = mapped_column(
        JSON,
        Computed("JSON_EXTRACT(tags,'$.chapters')", persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    # --- FSRS 调度状态（naive UTC 存储）---
    fsrs_stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    fsrs_difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    fsrs_due: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    fsrs_last_review: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # py-fsrs State 整数值：1=Learning 2=Review 3=Relearning；新卡为 Learning
    fsrs_state: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 学习/重学习步进索引
    fsrs_step: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped["User"] = relationship(back_populates="cards")
    review_logs: Mapped[list["ReviewLog"]] = relationship(
        back_populates="card",
        cascade="all, delete-orphan",
        order_by="ReviewLog.reviewed_at",
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Card id={self.id} user_id={self.user_id} front={self.front[:20]!r}>"
