"""HttpOnly Cookie 会话的回归测试。

正式前端不再把 token 放进 localStorage，而是依赖 Cookie：
- 登录（`/auth/session`）后令牌只在 Cookie 里，响应体**不含** token；
- `/auth/me` 等接口不带头也能凭 Cookie 认证；
- 登出会清除 Cookie 并吊销版本；
- Bearer 方式保持可用（兼容 /ui/literature 单页与脚本）。
"""

from __future__ import annotations

import pytest

from app.config import settings
from tests.conftest import requires_db

API = "/api/v1"
COOKIE = settings.cookie_name


def _register_and_login(client, credentials) -> None:
    client.post(f"{API}/auth/register", json=credentials)
    resp = client.post(f"{API}/auth/session", json=credentials)
    assert resp.status_code == 200, resp.text


@pytest.fixture
def fresh_client(client):
    """保证 Cookie 罐是干净的（避免上一条用例的会话泄漏）。"""
    client.cookies.clear()
    yield client
    client.cookies.clear()


@requires_db
@pytest.mark.db
def test_session_login_returns_user_without_token(fresh_client, user_credentials) -> None:
    fresh_client.post(f"{API}/auth/register", json=user_credentials)

    resp = fresh_client.post(f"{API}/auth/session", json=user_credentials)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"user"}, "Cookie 会话登录不得在响应体里返回 token"
    assert "access_token" not in body
    assert body["user"]["username"] == user_credentials["username"]


@requires_db
@pytest.mark.db
def test_session_sets_hardened_cookie(fresh_client, user_credentials) -> None:
    fresh_client.post(f"{API}/auth/register", json=user_credentials)

    resp = fresh_client.post(f"{API}/auth/session", json=user_credentials)
    set_cookie = resp.headers.get("set-cookie", "").lower()

    assert COOKIE.lower() in set_cookie
    assert "httponly" in set_cookie, "会话 Cookie 必须是 HttpOnly（JS 读不到）"
    assert "samesite=lax" in set_cookie, "需要 SameSite 防 CSRF"
    assert "path=/" in set_cookie
    assert "max-age=" in set_cookie, "Cookie 生命期应与 token 过期一致"


@requires_db
@pytest.mark.db
def test_me_works_with_cookie_only(fresh_client, user_credentials) -> None:
    _register_and_login(fresh_client, user_credentials)

    # 不带 Authorization 头，仅靠 Cookie
    resp = fresh_client.get(f"{API}/auth/me")

    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == user_credentials["username"]


@requires_db
@pytest.mark.db
def test_non_auth_endpoints_work_with_cookie(fresh_client, user_credentials) -> None:
    """/review/due 这类业务接口也必须能用 Cookie 访问。"""
    _register_and_login(fresh_client, user_credentials)

    resp = fresh_client.get(f"{API}/review/due")

    assert resp.status_code == 200, resp.text
    assert "items" in resp.json()


@requires_db
@pytest.mark.db
def test_missing_cookie_and_header_is_401(fresh_client) -> None:
    fresh_client.cookies.clear()

    resp = fresh_client.get(f"{API}/auth/me")

    assert resp.status_code == 401


@requires_db
@pytest.mark.db
def test_tampered_cookie_is_401(fresh_client, user_credentials) -> None:
    _register_and_login(fresh_client, user_credentials)
    fresh_client.cookies.set(COOKIE, "not-a-valid-jwt")

    resp = fresh_client.get(f"{API}/auth/me")

    assert resp.status_code == 401


@requires_db
@pytest.mark.db
def test_logout_clears_cookie_and_revokes(fresh_client, user_credentials) -> None:
    _register_and_login(fresh_client, user_credentials)

    logout = fresh_client.post(f"{API}/auth/logout")
    assert logout.status_code == 204
    cleared = logout.headers.get("set-cookie", "").lower()
    assert COOKIE.lower() in cleared
    assert 'max-age=0' in cleared or "expires=" in cleared, "登出必须让浏览器删除 Cookie"

    # 即使旧 Cookie 被重放，令牌版本已 +1 → 401
    fresh_client.cookies.set(COOKIE, "stale")
    assert fresh_client.get(f"{API}/auth/me").status_code == 401


@requires_db
@pytest.mark.db
def test_bearer_still_works(fresh_client, user_credentials) -> None:
    """兼容性：既有单页与脚本仍用 Bearer。"""
    fresh_client.post(f"{API}/auth/register", json=user_credentials)
    token = fresh_client.post(f"{API}/auth/login", json=user_credentials).json()["access_token"]
    fresh_client.cookies.clear()

    resp = fresh_client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200


@requires_db
@pytest.mark.db
def test_login_also_sets_cookie_for_legacy_page(fresh_client, user_credentials) -> None:
    """/auth/login 仍返回 token（兼容），但也顺手写 Cookie。"""
    fresh_client.post(f"{API}/auth/register", json=user_credentials)

    resp = fresh_client.post(f"{API}/auth/login", json=user_credentials)

    assert "access_token" in resp.json()
    assert COOKIE.lower() in resp.headers.get("set-cookie", "").lower()


@requires_db
@pytest.mark.db
def test_cookie_secure_flag_follows_config(fresh_client, user_credentials, monkeypatch) -> None:
    """生产开启 AUTH_COOKIE_SECURE 后，Cookie 必须带 Secure。"""
    monkeypatch.setattr(settings, "cookie_secure", True)
    fresh_client.post(f"{API}/auth/register", json=user_credentials)

    resp = fresh_client.post(f"{API}/auth/session", json=user_credentials)

    assert "secure" in resp.headers.get("set-cookie", "").lower()
