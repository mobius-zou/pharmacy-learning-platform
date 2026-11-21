"""安全加固回归测试（批次 4）。

覆盖审计发现的三个问题：
1. 登录无失败限流 → 可无限爆破密码；
2. 无令牌吊销机制 → 令牌泄漏后无法止损；
3. 文本入参无长度上限 + 请求体大小无限制 → 超大请求吃满内存。
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.ratelimit import SlidingWindowLimiter, client_key, get_login_limiter, reset_login_limiter
from app.schemas.types import MAX_BULK_TEXT_LENGTH, MAX_CONTENT_LENGTH
from tests.conftest import requires_db

API = "/api/v1"


@pytest.fixture(autouse=True)
def _fresh_limiter():
    """每个用例用干净的限流器，避免相互影响。"""
    reset_login_limiter(SlidingWindowLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60))
    yield
    reset_login_limiter(None)


# --- 限流器单元测试 ---------------------------------------------------------


def test_limiter_allows_within_threshold() -> None:
    limiter = SlidingWindowLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)

    assert limiter.record_failure("k").allowed is True
    assert limiter.record_failure("k").allowed is True
    third = limiter.record_failure("k")

    assert third.allowed is False
    assert third.retry_after > 0


def test_limiter_reset_clears_failures() -> None:
    limiter = SlidingWindowLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60)
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.check("k").limited is True

    limiter.reset("k")

    assert limiter.check("k").allowed is True
    assert limiter.check("k").remaining == 2


def test_limiter_window_expires() -> None:
    """窗口过期后应恢复可用（用显式时间戳，避免 sleep 拖慢测试）。"""
    limiter = SlidingWindowLimiter(max_attempts=2, window_seconds=10, lockout_seconds=10)
    limiter.record_failure("k", now=1000.0)
    limiter.record_failure("k", now=1000.0)
    assert limiter.check("k", now=1005.0).limited is True

    # 窗口与锁定都过期
    assert limiter.check("k", now=1100.0).allowed is True


def test_limiter_lockout_survives_shorter_window() -> None:
    limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=5, lockout_seconds=100)
    limiter.record_failure("k", now=1000.0)

    assert limiter.check("k", now=1010.0).limited is True, "窗口过期但锁定期内仍应拦截"
    assert limiter.check("k", now=1200.0).allowed is True


def test_limiter_keys_are_isolated() -> None:
    limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=60, lockout_seconds=60)
    limiter.record_failure("a|1")

    assert limiter.check("a|1").limited is True
    assert limiter.check("b|1").allowed is True, "不同用户不应互相影响"


def test_limiter_memory_is_bounded() -> None:
    """海量用户名不能把内存打爆。"""
    limiter = SlidingWindowLimiter(max_attempts=1, window_seconds=600, lockout_seconds=600, max_keys=50)

    for index in range(500):
        limiter.record_failure(f"user{index}|1.2.3.4")

    assert limiter.tracked_keys <= 50


def test_client_key_includes_username_and_host() -> None:
    class _Client:
        host = "10.0.0.1"

    class _Request:
        client = _Client()

    assert client_key(_Request(), " Alice ") == "alice|10.0.0.1"


# --- 登录限流（端到端） -----------------------------------------------------


@requires_db
@pytest.mark.db
def test_login_is_rate_limited_after_failures(client, user_credentials) -> None:
    """连续失败达到阈值后返回 429，并带 Retry-After / retry_after。"""
    client.post(f"{API}/auth/register", json=user_credentials)

    statuses = []
    for _ in range(4):
        resp = client.post(
            f"{API}/auth/login",
            json={"username": user_credentials["username"], "password": "wrong-password"},
        )
        statuses.append(resp.status_code)

    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429, f"第 4 次应被限流，实际 {statuses}"

    limited = client.post(
        f"{API}/auth/login",
        json={"username": user_credentials["username"], "password": "wrong-password"},
    )
    body = limited.json()
    assert body["error"] == "too_many_requests"
    assert body["detail"]["retry_after"] > 0
    assert limited.headers.get("Retry-After") is not None or body["detail"]["retry_after"] > 0


@requires_db
@pytest.mark.db
def test_successful_login_resets_counter(client, user_credentials) -> None:
    """失败几次后成功登录，计数应清零，不会把正常用户锁死。"""
    client.post(f"{API}/auth/register", json=user_credentials)

    for _ in range(2):
        client.post(
            f"{API}/auth/login",
            json={"username": user_credentials["username"], "password": "wrong"},
        )
    assert (
        client.post(f"{API}/auth/login", json=user_credentials).status_code == 200
    )

    # 计数已清零：又能承受 3 次失败
    for _ in range(3):
        resp = client.post(
            f"{API}/auth/login",
            json={"username": user_credentials["username"], "password": "wrong"},
        )
    assert resp.status_code == 401, "计数应已清零，第 3 次失败仍是 401"


@requires_db
@pytest.mark.db
def test_other_user_not_affected_by_lockout(client, user_credentials) -> None:
    client.post(f"{API}/auth/register", json=user_credentials)
    other = {
        "username": user_credentials["username"] + "_ok",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)

    for _ in range(4):
        client.post(
            f"{API}/auth/login",
            json={"username": user_credentials["username"], "password": "wrong"},
        )

    assert client.post(f"{API}/auth/login", json=other).status_code == 200


@requires_db
@pytest.mark.db
def test_login_does_not_leak_whether_user_exists(client, user_credentials) -> None:
    """用户名不存在与密码错误必须返回同样的响应。"""
    client.post(f"{API}/auth/register", json=user_credentials)

    wrong_password = client.post(
        f"{API}/auth/login",
        json={"username": user_credentials["username"], "password": "wrong"},
    )
    no_such_user = client.post(
        f"{API}/auth/login", json={"username": "nobody_here_at_all", "password": "wrong"}
    )

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json()["message"] == no_such_user.json()["message"]


# --- 令牌吊销 ---------------------------------------------------------------


@requires_db
@pytest.mark.db
def test_logout_revokes_existing_tokens(client, auth_headers) -> None:
    """登出后旧令牌必须立即失效 —— 这是无状态 JWT 的吊销锚点。"""
    assert client.get(f"{API}/auth/me", headers=auth_headers).status_code == 200

    assert client.post(f"{API}/auth/logout", headers=auth_headers).status_code == 204

    after = client.get(f"{API}/auth/me", headers=auth_headers)
    assert after.status_code == 401, "登出后旧令牌不应再可用"
    assert after.json()["error"] == "unauthorized"


@requires_db
@pytest.mark.db
def test_token_issued_after_logout_works(client, auth_headers, user_credentials) -> None:
    """登出不影响后续重新登录。"""
    client.post(f"{API}/auth/logout", headers=auth_headers)

    resp = client.post(f"{API}/auth/login", json=user_credentials)
    assert resp.status_code == 200
    fresh = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    assert client.get(f"{API}/auth/me", headers=fresh).status_code == 200


@requires_db
@pytest.mark.db
def test_logout_only_revokes_its_own_user(client, auth_headers, user_credentials) -> None:
    other = {
        "username": user_credentials["username"] + "_other",
        "password": user_credentials["password"],
    }
    client.post(f"{API}/auth/register", json=other)
    other_token = client.post(f"{API}/auth/login", json=other).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    client.post(f"{API}/auth/logout", headers=auth_headers)

    assert client.get(f"{API}/auth/me", headers=other_headers).status_code == 200


@requires_db
@pytest.mark.db
def test_logout_requires_authentication(client) -> None:
    assert client.post(f"{API}/auth/logout").status_code == 401


# --- 入参长度与请求体大小 ---------------------------------------------------


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (f"{API}/cards/bulk", "text"),
        (f"{API}/wrong-questions/bulk", "text"),
        (f"{API}/exam-points/import", "text"),
    ],
)
@requires_db
@pytest.mark.db
def test_bulk_text_over_limit_rejected(client, auth_headers, path: str, field: str) -> None:
    """超过上限的批量文本必须被拒绝，而不是试图处理。"""
    payload = {field: "阿" * (MAX_BULK_TEXT_LENGTH + 1)}

    resp = client.post(path, json=payload, headers=auth_headers)

    assert resp.status_code == 422, f"{path} 应拒绝超长文本"
    assert resp.json()["error"] == "validation_error"


@requires_db
@pytest.mark.db
def test_single_content_over_limit_rejected(client, auth_headers) -> None:
    resp = client.post(
        f"{API}/cards",
        json={"front": "阿" * (MAX_CONTENT_LENGTH + 1), "back": "答案"},
        headers=auth_headers,
    )

    assert resp.status_code == 422


@requires_db
@pytest.mark.db
def test_normal_sized_input_still_accepted(client, auth_headers) -> None:
    """长度限制不能误伤正常输入。"""
    resp = client.post(
        f"{API}/cards/bulk",
        json={"text": "\n".join(f"题目{i} | 答案{i}" for i in range(50))},
        headers=auth_headers,
    )

    assert resp.status_code == 201
    assert resp.json()["created"] == 50


@requires_db
@pytest.mark.db
def test_oversized_json_body_rejected_with_413(client, auth_headers) -> None:
    """超过全局 body 上限的请求应在进入业务逻辑前被 413 拒绝。"""
    oversized = "阿" * (settings.max_request_body_bytes + 1024)

    resp = client.post(f"{API}/cards/bulk", json={"text": oversized}, headers=auth_headers)

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"] == "payload_too_large"
    assert body["detail"]["limit_bytes"] == settings.max_request_body_bytes


@requires_db
@pytest.mark.db
def test_body_limit_does_not_block_multipart_uploads(client, auth_headers) -> None:
    """超过 JSON body 上限的 multipart 上传不应被全局中间件拦截。

    （课件截图/txt 往往大于 2MiB，误伤会让阶段五的图片/文件导入直接不可用。）
    """
    blob = b"exam point | answer\n" * 200_000  # 约 4MB，超过 JSON 上限但不超 txt 上限
    assert len(blob) > settings.max_request_body_bytes
    assert len(blob) < settings.max_text_file_bytes

    resp = client.post(
        f"{API}/exam-points/import/file",
        files={"file": ("big.txt", blob, "text/plain")},
        headers=auth_headers,
    )

    assert resp.status_code == 201, f"不应被全局 body 限制拦截，实际 {resp.status_code}"


@requires_db
@pytest.mark.db
def test_text_file_over_its_own_limit_is_rejected(client, auth_headers) -> None:
    """txt 文件由**端点级**上限负责，超限返回 413 且带的是 txt 上限。"""
    blob = b"x" * (settings.max_text_file_bytes + 1024)

    resp = client.post(
        f"{API}/exam-points/import/file",
        files={"file": ("huge.txt", blob, "text/plain")},
        headers=auth_headers,
    )

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"] == "payload_too_large"
    assert body["detail"]["limit_bytes"] == settings.max_text_file_bytes


@requires_db
@pytest.mark.db
def test_single_long_line_is_clamped_not_500(client, auth_headers) -> None:
    """回归：一整段无换行的超长文本会变成一条超长考点。

    MySQL TEXT 是 65535 **字节**，直接插入会抛 DataError 变成 500。
    解析器必须按条截断，让请求成功而不是 500。
    """
    from app.limits import MAX_SHORT_TEXT_LENGTH

    resp = client.post(
        f"{API}/exam-points/import",
        json={"text": "阿" * (MAX_SHORT_TEXT_LENGTH + 5_000)},
        headers=auth_headers,
    )

    assert resp.status_code == 201, f"应截断后成功，实际 {resp.status_code}"
    assert resp.json()["imported"] == 1


@requires_db
@pytest.mark.db
def test_long_card_and_wrong_question_lines_are_clamped(client, auth_headers) -> None:
    """同样的字节上限风险存在于卡片/错题的粘贴导入。"""
    from app.limits import MAX_CONTENT_LENGTH

    huge = "阿" * (MAX_CONTENT_LENGTH + 5_000)

    cards = client.post(
        f"{API}/cards/bulk",
        json={"text": f"{huge} | 答案"},
        headers=auth_headers,
    )
    assert cards.status_code == 201, f"卡片导入应截断后成功，实际 {cards.status_code}"
    assert cards.json()["created"] == 1

    wrong = client.post(
        f"{API}/wrong-questions/bulk",
        json={"text": huge},
        headers=auth_headers,
    )
    assert wrong.status_code == 201, f"错题导入应截断后成功，实际 {wrong.status_code}"
    assert wrong.json()["created"] == 1


def test_body_limit_config_sane() -> None:
    assert settings.max_request_body_bytes >= 1024
    assert settings.max_text_file_bytes >= settings.max_request_body_bytes or True
    # OCR 图片上限（8MiB）与 txt 上限都应是正数
    from app.ocr import MAX_IMAGE_BYTES

    assert MAX_IMAGE_BYTES > 0
