"""幂等记录（基础设施表，非业务数据）。

用途：解决「网络超时 / 客户端重发」导致的重复副作用。
典型场景是评分接口 —— 客户端没收到响应就重发了一次，若没有幂等键，
FSRS 会被推进两次，复习间隔与统计都会失真。

设计
----
- ``(user_id, key)`` 唯一：同一个键只会成功执行一次；
- 记录 ``fingerprint``（请求体哈希）：同键不同体视为误用，直接 409，
  而不是把上一次的响应张冠李戴地返回；
- 记录 ``status_code`` 与 ``payload``：重放时原样返回，客户端无需区分首次/重放；
- 记录 ``created_at``：过期记录会被清理（默认 24 小时，``IDEMPOTENCY_TTL_SECONDS``）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy import JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..timeutils import utcnow
from ._common import MYSQL_TABLE_ARGS, UTCDateTime


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),
        MYSQL_TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 客户端提供的幂等键（限长 200，避免被当成大字段滥用）
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    # 接口标识，例如 "POST /review/{id}/grade"
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    # 请求体指纹（sha256），用于识别「同键不同请求」
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<IdempotencyRecord user={self.user_id} key={self.key!r} {self.status_code}>"
