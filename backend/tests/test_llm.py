"""LLM 客户端测试。

用本地 HTTP 服务器真实地跑一遍网络路径，验证：
- 成功解析 OpenAI 兼容响应；
- 请求头/请求体符合预期（不带凭据泄漏）；
- HTTP 错误、结构异常、非 JSON 内容、超时、连接失败都能安全降级（不抛异常）。

这样「LLM 单次调用」这条需求不依赖外部服务也可被验证。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.llm import LLMClient, parse_json_object
from app.services.wrong_question_service import identify_knowledge_point


class _Handler(BaseHTTPRequestHandler):
    """可配置的假 LLM 服务。"""

    status = 200
    body: bytes = b"{}"
    delay = 0.0
    last_request: dict | None = None

    def do_POST(self) -> None:  # noqa: N802 - 标准库命名
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = None
        type(self).last_request = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "payload": payload,
        }

        if self.delay:
            time.sleep(self.delay)

        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args) -> None:  # noqa: D102 - 静音
        pass


def _chat_envelope(content: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode("utf-8")


@pytest.fixture
def llm_server():
    """启动本地假 LLM 服务，返回 (api_base, handler 类)。"""
    _Handler.status = 200
    _Handler.body = _chat_envelope('{"knowledge_point": "阿司匹林的作用机制"}')
    _Handler.delay = 0.0
    _Handler.last_request = None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _Handler
    finally:
        server.shutdown()
        server.server_close()


def _client(api_base: str, timeout: float = 5.0) -> LLMClient:
    return LLMClient(api_base=api_base, api_key="test-key", model="test-model", timeout=timeout)


# --- 成功路径 ---------------------------------------------------------------


def test_complete_json_success(llm_server) -> None:
    api_base, _ = llm_server

    response = _client(api_base).complete_json("system", "user")

    assert response.ok is True
    assert response.data == {"knowledge_point": "阿司匹林的作用机制"}
    assert response.error is None


def test_request_shape_is_openai_compatible(llm_server) -> None:
    api_base, handler = llm_server

    _client(api_base).complete_json("系统提示", "用户提示", temperature=0.3)

    assert handler.last_request is not None
    assert handler.last_request["path"] == "/chat/completions"
    assert handler.last_request["authorization"] == "Bearer test-key"
    assert handler.last_request["content_type"] == "application/json"

    payload = handler.last_request["payload"]
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0.3
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert payload["messages"][0]["content"] == "系统提示"
    assert payload["messages"][1]["content"] == "用户提示"


def test_works_without_api_key(llm_server) -> None:
    """本地/自建模型常常不需要 key，不应因此失败。"""
    api_base, handler = llm_server
    client = LLMClient(api_base=api_base, api_key="", model="m", timeout=5.0)

    assert client.available is True
    assert client.complete_json("s", "u").ok is True
    assert handler.last_request["authorization"] is None


def test_parses_json_wrapped_in_markdown(llm_server) -> None:
    api_base, handler = llm_server
    handler.body = _chat_envelope('```json\n{"knowledge_point": "青霉素"}\n```')

    response = _client(api_base).complete_json("s", "u")

    assert response.ok is True
    assert response.data == {"knowledge_point": "青霉素"}


def test_parses_json_with_surrounding_prose(llm_server) -> None:
    api_base, handler = llm_server
    handler.body = _chat_envelope('好的：{"knowledge_point": "地西泮"} 完成')

    assert _client(api_base).complete_json("s", "u").data == {"knowledge_point": "地西泮"}


def test_trailing_slash_in_api_base_is_tolerated(llm_server) -> None:
    api_base, handler = llm_server

    _client(api_base + "/").complete_json("s", "u")

    assert handler.last_request["path"] == "/chat/completions"


# --- 失败路径：必须降级而不是抛异常 -----------------------------------------


def test_http_error_is_reported_not_raised(llm_server) -> None:
    api_base, handler = llm_server
    handler.status = 500
    handler.body = b'{"error": "boom"}'

    response = _client(api_base).complete_json("s", "u")

    assert response.ok is False
    assert response.error == "HTTP 500"


def test_malformed_envelope_is_reported(llm_server) -> None:
    api_base, handler = llm_server
    handler.body = json.dumps({"unexpected": True}).encode()

    response = _client(api_base).complete_json("s", "u")

    assert response.ok is False
    assert response.error == "响应结构异常"


def test_non_json_content_is_reported(llm_server) -> None:
    api_base, handler = llm_server
    handler.body = _chat_envelope("我无法回答这个问题")

    response = _client(api_base).complete_json("s", "u")

    assert response.ok is False
    assert response.error == "未返回合法 JSON"
    assert response.text == "我无法回答这个问题"


def test_non_json_http_body_is_reported(llm_server) -> None:
    api_base, handler = llm_server
    handler.body = b"<html>gateway error</html>"

    assert _client(api_base).complete_json("s", "u").ok is False


def test_timeout_is_reported(llm_server) -> None:
    api_base, handler = llm_server
    handler.delay = 1.5

    response = _client(api_base, timeout=0.4).complete_json("s", "u")

    assert response.ok is False
    assert "timeout" in (response.error or "").lower() or "timed out" in (response.error or "").lower()


def test_connection_refused_is_reported() -> None:
    """端口无人监听时必须安全返回，而不是把异常抛到调用方。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()  # 立刻关闭，制造 connection refused

    response = _client(f"http://127.0.0.1:{free_port}", timeout=2.0).complete_json("s", "u")

    assert response.ok is False
    assert response.error


def test_unavailable_client_does_not_call_network() -> None:
    """未配置时不发起任何网络请求。"""
    client = LLMClient(api_base="", api_key="", model="")

    assert client.available is False
    response = client.complete_json("s", "u")
    assert response.ok is False
    assert response.error == "LLM 未配置"


# --- 与服务层打通 -----------------------------------------------------------


def test_service_uses_real_llm_when_configured(llm_server) -> None:
    """配置可用时，知识点识别真的走 LLM。"""
    api_base, handler = llm_server

    suggestion = identify_knowledge_point(
        "阿司匹林为什么能解热镇痛", client=_client(api_base)
    )

    assert suggestion.source == "llm"
    assert suggestion.knowledge_point == "阿司匹林的作用机制"
    assert handler.last_request is not None, "应确实发起了 HTTP 调用"


def test_service_falls_back_when_llm_returns_garbage(llm_server) -> None:
    """LLM 返回不可用时，识别自动退化为启发式（降级模式）。"""
    api_base, handler = llm_server
    handler.body = _chat_envelope("抱歉，我不知道")

    suggestion = identify_knowledge_point(
        "阿司匹林的作用机制是什么？", client=_client(api_base)
    )

    assert suggestion.source == "heuristic"
    assert suggestion.knowledge_point == "阿司匹林的作用机制"
    assert suggestion.llm_error == "未返回合法 JSON"


def test_service_sends_known_points_as_hint(llm_server) -> None:
    """已有知识点会作为提示发给 LLM，帮助表述统一。"""
    api_base, handler = llm_server

    identify_knowledge_point(
        "阿司匹林的作用机制", known_points=["阿司匹林的作用机制"], client=_client(api_base)
    )

    user_message = handler.last_request["payload"]["messages"][1]["content"]
    assert "阿司匹林的作用机制" in user_message


def test_parse_json_object_none_for_empty_content(llm_server) -> None:
    api_base, handler = llm_server
    handler.body = _chat_envelope("")

    assert _client(api_base).complete_json("s", "u").ok is False
    assert parse_json_object("") is None
