"""幂等键服务：同一次逻辑操作重发多次只产生一次副作用。

只用于**有副作用的写操作**（目前是评分）。读操作不需要幂等键。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..logging_config import get_logger
from ..models import IdempotencyRecord
from ..timeutils import utcnow

logger = get_logger(__name__)


def fingerprint_of(body: Any) -> str:
    """请求体指纹：用于识别「同一个键被用于不同请求」。"""
    serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def get_record(db: Session, user_id: int, key: str) -> IdempotencyRecord | None:
    """取未过期的幂等记录。"""
    cutoff = utcnow() - timedelta(seconds=settings.idempotency_ttl_seconds)
    return db.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.user_id == user_id,
            IdempotencyRecord.key == key,
            IdempotencyRecord.created_at >= cutoff,
        )
    ).scalar_one_or_none()


def remember(
    db: Session,
    user_id: int,
    key: str,
    endpoint: str,
    fingerprint: str,
    status_code: int,
    payload: dict[str, Any],
) -> None:
    """记录一次成功的写操作结果。

    并发下同一键可能同时到达：唯一约束会让其中一个失败，
    此时把该记录视为「已被别人写入」，直接忽略而不是让请求失败。
    """
    record = IdempotencyRecord(
        user_id=user_id,
        key=key,
        endpoint=endpoint,
        fingerprint=fingerprint,
        status_code=status_code,
        payload=payload,
        created_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(record)
    except IntegrityError:
        logger.info("幂等键并发命中，忽略重复写入 key=%s", key)


def purge_expired(db: Session, user_id: int | None = None) -> int:
    """清理过期记录（写路径顺带调用，避免表无限增长）。"""
    cutoff = utcnow() - timedelta(seconds=settings.idempotency_ttl_seconds)
    stmt = delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff)
    if user_id is not None:
        stmt = stmt.where(IdempotencyRecord.user_id == user_id)
    result = db.execute(stmt)
    return int(result.rowcount or 0)
