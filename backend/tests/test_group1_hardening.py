"""组 1 加固回归测试（H1 / H2 / H3 / H7）。

对应外部审计报告的四项 P1：

- **H1**：``.env`` 写 ``JWT_SECRET_KEY``，代码只读 ``JWT_SECRET``
  → 静默回退进程内随机密钥（重启全掉线、多 worker 互相 401）。
- **H2**：``.env`` 权限 644，同机其他用户可读数据库密码。
- **H3**：请求体限制只看 Content-Length 且跳过 multipart
  → chunked 大包绕过；上传先 ``file.read()`` 再校验。
- **H7**：启动 ``create_all`` 与 Alembic 双轨 → 新库跑 ``upgrade head`` 会失败。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.config import Settings
from app.middleware import MaxBodySizeMiddleware, read_upload_limited

API = "/api/v1"


# --- H1：JWT 密钥键名兼容 ---------------------------------------------------


@pytest.mark.parametrize("env_name", ["JWT_SECRET", "JWT_SECRET_KEY", "SECRET_KEY"])
def test_jwt_secret_accepted_under_all_supported_names(monkeypatch, env_name: str) -> None:
    """三种常见键名都必须被识别，避免「配置了却静默失效」。"""
    for name in ("JWT_SECRET", "JWT_SECRET_KEY", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(env_name, "x" * 48)

    settings = Settings()

    assert settings.jwt_secret_is_ephemeral is False, f"{env_name} 未被识别"
    assert settings.jwt_secret == "x" * 48


def test_jwt_secret_falls_back_to_ephemeral_when_absent(monkeypatch) -> None:
    for name in ("JWT_SECRET", "JWT_SECRET_KEY", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.jwt_secret_is_ephemeral is True
    assert len(settings.jwt_secret) >= 32


def test_production_refuses_ephemeral_jwt_secret(monkeypatch) -> None:
    """生产环境用临时密钥必须**启动即失败**，而不是悄悄降级。"""
    monkeypatch.setattr(app_main.settings, "environment", "production")
    monkeypatch.setattr(app_main.settings, "jwt_secret_is_ephemeral", True)

    with pytest.raises(RuntimeError, match="生产环境必须显式配置 JWT 密钥"):
        with TestClient(app_main.app):
            pass


def test_development_allows_ephemeral_secret_with_warning(monkeypatch) -> None:
    monkeypatch.setattr(app_main.settings, "environment", "development")
    monkeypatch.setattr(app_main.settings, "jwt_secret_is_ephemeral", True)

    # 开发环境只告警，不应抛异常
    with TestClient(app_main.app) as client:
        assert client.get("/health").status_code == 200


def test_current_env_actually_has_a_stable_secret() -> None:
    """回归：项目 .env 的键名必须能被识别，否则重启后 token 全部失效。"""
    from app.config import settings

    assert settings.jwt_secret_is_ephemeral is False, (
        "JWT 密钥未被识别 —— 检查 .env 的键名（支持 JWT_SECRET / JWT_SECRET_KEY / SECRET_KEY）"
    )


# --- H2：.env 权限 ----------------------------------------------------------


def test_env_permission_check_warns_on_group_or_other_read(tmp_path, caplog) -> None:
    """权限过宽（0644）时必须告警并给出 chmod 600 建议。

    用临时文件注入路径，避免测试结果依赖本机 .env 的当前权限。
    """
    import logging
    import os

    env_file = tmp_path / ".env"
    env_file.write_text("PLACEHOLDER=1\n", encoding="utf-8")
    os.chmod(env_file, 0o644)

    with caplog.at_level(logging.WARNING):
        app_main._check_env_permissions(env_file)

    messages = [record.message for record in caplog.records]
    assert any("chmod 600" in message for message in messages), (
        f"0644 时应告警，实际告警：{messages}"
    )
    # 只 stat，不应把内容读进日志
    assert "PLACEHOLDER" not in caplog.text


def test_env_permission_check_is_silent_when_owner_only(tmp_path, caplog) -> None:
    """0600 时不应告警（已修复的状态不该继续刷警告）。"""
    import logging
    import os

    env_file = tmp_path / ".env"
    env_file.write_text("PLACEHOLDER=1\n", encoding="utf-8")
    os.chmod(env_file, 0o600)

    with caplog.at_level(logging.WARNING):
        app_main._check_env_permissions(env_file)

    assert not [r for r in caplog.records if "chmod" in r.message]


def test_env_permission_check_silent_when_missing(tmp_path) -> None:
    app_main._check_env_permissions(tmp_path / "does-not-exist.env")  # 不存在 → 直接返回


# --- H3：chunked 绕过 + 上传前置校验 ----------------------------------------


def test_chunked_request_without_content_length_is_rejected(client, auth_headers) -> None:
    """**核心回归**：不带 Content-Length 的分块大包也必须被拦下。

    旧实现只看 Content-Length，chunked 请求可以完全绕过 2MiB 限制。
    """
    limit = app_main.settings.max_request_body_bytes
    chunk = b"x" * (limit // 2)
    payload = chunk * 6  # 3 倍上限

    resp = client.post(
        f"{API}/cards/bulk",
        content=iter([payload[i : i + 65536] for i in range(0, len(payload), 65536)]),
        headers={**auth_headers, "Content-Type": "application/json"},
    )

    assert resp.status_code == 413, f"chunked 大包应被拒绝，实际 {resp.status_code}"
    assert resp.json()["error"] == "payload_too_large"


def test_declared_length_over_limit_rejected_before_read(client, auth_headers) -> None:
    """带 Content-Length 的超限请求应在读取前就被拒。"""
    limit = app_main.settings.max_request_body_bytes

    resp = client.post(
        f"{API}/cards/bulk",
        json={"text": "x" * (limit + 1024)},
        headers=auth_headers,
    )

    assert resp.status_code == 413


class _FakeUpload:
    """模拟 UploadFile：记录 read 是否被调用。"""

    def __init__(self, size: int | None, chunks: list[bytes]) -> None:
        self.size = size
        self._chunks = list(chunks)
        self.read_called = False

    async def read(self, n: int = -1) -> bytes:
        self.read_called = True
        return self._chunks.pop(0) if self._chunks else b""


def test_read_upload_limited_rejects_by_declared_size_without_reading() -> None:
    """有了声明大小就不该把文件读进内存 —— 这是 H3 的关键修复点。"""
    upload = _FakeUpload(size=10_000, chunks=[b"x" * 10_000])

    with pytest.raises(Exception) as excinfo:
        asyncio.run(read_upload_limited(upload, limit=1_000))

    assert getattr(excinfo.value, "status_code", None) == 413
    assert upload.read_called is False, "声明大小已超限时不应再读取内容"


def test_read_upload_limited_enforces_during_streaming() -> None:
    """声明大小未知（None）时，边读边计数并在超限时中断。"""
    upload = _FakeUpload(size=None, chunks=[b"x" * 600, b"y" * 600])

    with pytest.raises(Exception) as excinfo:
        asyncio.run(read_upload_limited(upload, limit=1_000))

    assert getattr(excinfo.value, "status_code", None) == 413


def test_read_upload_limited_returns_full_content_when_within_limit() -> None:
    upload = _FakeUpload(size=None, chunks=[b"abc", b"def"])

    assert asyncio.run(read_upload_limited(upload, limit=100)) == b"abcdef"


def test_multipart_limit_is_higher_than_json_limit() -> None:
    """multipart 上限必须高于 JSON 上限，否则课件截图会被误伤。"""
    from app.config import settings

    assert settings.max_multipart_bytes > settings.max_request_body_bytes
    assert settings.max_multipart_bytes >= 8 * 1024 * 1024  # 覆盖 OCR 的 8MiB


# --- H7：建表双轨 -----------------------------------------------------------


def test_auto_create_schema_is_disabled_by_default(monkeypatch) -> None:
    """默认不再 create_all —— 表结构只由 Alembic 决定。"""
    monkeypatch.delenv("AUTO_CREATE_SCHEMA", raising=False)

    assert Settings().auto_create_schema is False


def test_startup_does_not_create_all_when_disabled(monkeypatch) -> None:
    """AUTO_CREATE_SCHEMA=false 时启动绝不能调用 create_all。"""
    called: list[int] = []

    def fake_create_all() -> None:
        called.append(1)

    monkeypatch.setattr(app_main.settings, "auto_create_schema", False)
    monkeypatch.setattr(app_main, "create_all", fake_create_all)

    with TestClient(app_main.app) as client:
        assert client.get("/health").status_code == 200

    assert called == [], "关闭时不应对数据库做 create_all"


def test_startup_uses_create_all_only_when_explicitly_enabled(monkeypatch) -> None:
    called: list[int] = []

    def fake_create_all() -> None:
        called.append(1)

    monkeypatch.setattr(app_main.settings, "auto_create_schema", True)
    monkeypatch.setattr(app_main, "create_all", fake_create_all)

    with TestClient(app_main.app):
        pass

    assert called == [1], "显式开启时才允许 create_all"
