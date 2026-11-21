"""安全相关：密码哈希（bcrypt）与 JWT 签发/校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from .config import settings
from .logging_config import get_logger

logger = get_logger(__name__)


class TokenError(Exception):
    """JWT 无效或过期。"""


def hash_password(password: str) -> str:
    """bcrypt 哈希。超过 72 字节的部分会被 bcrypt 丢弃，这里显式拒绝以防误用。"""
    raw = password.encode("utf-8")
    if len(raw) > settings.password_max_bytes:
        raise ValueError(
            f"密码长度超过 bcrypt 上限 {settings.password_max_bytes} 字节，请缩短密码"
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码；任何异常都视为校验失败。"""
    try:
        raw = password.encode("utf-8")
        if len(raw) > settings.password_max_bytes:
            return False
        return bcrypt.checkpw(raw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str | int,
    expires_minutes: int | None = None,
    extra_claims: dict[str, Any] | None = None,
    token_version: int = 0,
) -> str:
    """签发 HS256 access token。

    ``ver`` 是服务端的令牌版本锚点：无状态 JWT 本身无法撤销，
    登出时把用户身上的版本 +1，旧令牌校验就会失败。
    """
    now = datetime.now(timezone.utc)
    minutes = settings.jwt_expire_minutes if expires_minutes is None else expires_minutes
    payload: dict[str, Any] = {
        "sub": str(subject),
        "ver": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """解码并校验 token，失败抛 TokenError。"""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("登录状态已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("无效的访问令牌") from exc
