"""密码哈希与 JWT 单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.security import (
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


# --- 密码 -------------------------------------------------------------------


def test_hash_and_verify_roundtrip() -> None:
    hashed = hash_password("correct-horse-battery")

    assert hashed != "correct-horse-battery"
    assert verify_password("correct-horse-battery", hashed) is True


def test_wrong_password_rejected() -> None:
    hashed = hash_password("password-123")

    assert verify_password("password-124", hashed) is False
    assert verify_password("", hashed) is False


def test_same_password_produces_different_hashes() -> None:
    """bcrypt 自带随机盐。"""
    assert hash_password("password-123") != hash_password("password-123")


def test_unicode_password_supported() -> None:
    hashed = hash_password("药理学-密码-123")

    assert verify_password("药理学-密码-123", hashed) is True
    assert verify_password("药理学-密码-124", hashed) is False


def test_overlong_password_is_rejected_not_truncated() -> None:
    """bcrypt 只处理前 72 字节，超长必须显式报错，避免「同前缀即同密码」。"""
    too_long = "a" * (settings.password_max_bytes + 1)

    with pytest.raises(ValueError, match="72"):
        hash_password(too_long)
    assert verify_password(too_long, hash_password("a" * 72)) is False


def test_verify_against_malformed_hash_returns_false() -> None:
    assert verify_password("password-123", "not-a-bcrypt-hash") is False


# --- JWT --------------------------------------------------------------------


def test_token_roundtrip_carries_subject() -> None:
    token = create_access_token(42, extra_claims={"username": "alice"})
    payload = decode_access_token(token)

    assert payload["sub"] == "42"
    assert payload["username"] == "alice"
    assert payload["exp"] > payload["iat"]


def test_expired_token_rejected() -> None:
    token = create_access_token(1, expires_minutes=-1)

    with pytest.raises(TokenError, match="过期"):
        decode_access_token(token)


def test_tampered_token_rejected() -> None:
    token = create_access_token(1)
    tampered = token[:-3] + ("abc" if not token.endswith("abc") else "xyz")

    with pytest.raises(TokenError):
        decode_access_token(tampered)


def test_token_signed_with_other_secret_rejected() -> None:
    forged = jwt.encode(
        {
            "sub": "1",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "a-different-secret-that-is-long-enough-32+",
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_expiry_respects_configured_value() -> None:
    token = create_access_token(1, expires_minutes=30)
    payload = decode_access_token(token)

    assert payload["exp"] - payload["iat"] == 30 * 60


def test_jwt_secret_is_not_hardcoded() -> None:
    """密钥必须来自环境变量，或退化为进程内随机值（绝非常量）。"""
    assert len(settings.jwt_secret) >= 32
    assert settings.jwt_secret not in {"secret", "changeme", "your-secret-key"}
