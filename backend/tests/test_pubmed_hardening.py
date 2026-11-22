"""M16 回归：PubMed 响应体积上限 + 检索词日志脱敏。"""

from __future__ import annotations

import json
import logging
import urllib.request

import pytest

import app.services.literature_service as lit
from app.config import settings
from app.services.literature_service import PubMedClient, PubMedError


class _FakeResponse:
    """模拟 urllib 的响应对象（上下文管理器 + 受限 read）。"""

    def __init__(self, payload: bytes, declared: str | None = None) -> None:
        self._payload = payload
        self.headers = {"Content-Length": declared} if declared is not None else {}
        self.read_sizes: list[int] = []

    def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        return self._payload if n < 0 else self._payload[:n]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_urlopen(monkeypatch, response: _FakeResponse) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response)


def test_oversized_response_is_rejected_after_read(monkeypatch) -> None:
    """没有 Content-Length 时也要靠「多读 1 字节」发现超限。"""
    limit = settings.pubmed_max_response_bytes
    monkeypatch.setattr(settings, "pubmed_max_response_bytes", 1024)
    _patch_urlopen(monkeypatch, _FakeResponse(b"x" * 2048))

    client = PubMedClient()

    with pytest.raises(PubMedError, match="超过上限"):
        client._request("efetch.fcgi", {"db": "pubmed"})
    monkeypatch.setattr(settings, "pubmed_max_response_bytes", limit)


def test_declared_content_length_is_checked_before_reading(monkeypatch) -> None:
    """有 Content-Length 时先判后读，不能白读一遍大响应。"""
    monkeypatch.setattr(settings, "pubmed_max_response_bytes", 1024)
    response = _FakeResponse(b"x" * 2048, declared="2048")
    _patch_urlopen(monkeypatch, response)

    client = PubMedClient()

    with pytest.raises(PubMedError, match="响应过大"):
        client._request("efetch.fcgi", {"db": "pubmed"})
    assert response.read_sizes == [], "声明长度已超限时不应读取内容"


def test_normal_response_passes(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, _FakeResponse(b"<xml>ok</xml>"))

    client = PubMedClient()

    assert client._request("efetch.fcgi", {"db": "pubmed"}) == "<xml>ok</xml>"


def test_search_log_does_not_contain_raw_query(monkeypatch, caplog) -> None:
    """检索词可能含受试者信息/未公开课题名，日志只留长度与短哈希。"""
    secret = "张三 罕见病 未公开课题"
    payload = json.dumps({"esearchresult": {"idlist": ["1", "2"], "count": "2"}})

    client = PubMedClient()
    monkeypatch.setattr(client, "_request", lambda endpoint, params: payload)

    with caplog.at_level(logging.INFO, logger="app.services.literature_service"):
        pmids, total = client.search(secret)

    assert pmids == ["1", "2"]
    assert total == 2
    assert secret not in caplog.text, "检索词不应以明文进日志"
    assert "关键词" in caplog.text
    # 仍可用短哈希关联重复检索
    assert "摘要" in caplog.text


def test_search_still_logs_hit_counts(monkeypatch, caplog) -> None:
    """脱敏不能把可观测性一起丢掉：命中数仍要记录。"""
    payload = json.dumps({"esearchresult": {"idlist": ["7"], "count": "42"}})
    client = PubMedClient()
    monkeypatch.setattr(client, "_request", lambda endpoint, params: payload)

    with caplog.at_level(logging.INFO, logger="app.services.literature_service"):
        client.search("aspirin")

    assert "42" in caplog.text


def test_response_cap_config_is_sane() -> None:
    assert settings.pubmed_max_response_bytes >= 1024
    # 正常 efetch 只有几十 KB，上限不应低到误伤
    assert settings.pubmed_max_response_bytes >= 1024 * 1024
