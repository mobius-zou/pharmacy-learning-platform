"""校对页静态资源与 CSP 的回归测试（P1-1 / P1-6）。

这些断言都针对**机器可消费的行为**（状态码、Content-Type、响应头、页面里
是否还残留内联脚本），而不是文案。
"""

from __future__ import annotations

import re

import pytest

UI = "/ui/literature"
CSS = "/static/literature.css"
JS = "/static/literature.js"


# --- 页面可访问 ---


def test_ui_page_served_and_still_mentions_review(client) -> None:
    resp = client.get(UI)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "人工校对" in resp.text


def test_ui_page_has_no_inline_script_or_style(client) -> None:
    """拆出外部文件后必须彻底无内联，否则严格 CSP 会把页面打空。"""
    html = client.get(UI).text

    assert "<script>" not in html, "仍有内联脚本，CSP 会阻止其执行"
    assert not re.search(r"<style[\s>]", html), "仍有内联样式块"
    assert not re.search(r'\sstyle="', html), "仍有内联 style 属性"
    assert not re.search(r"\son[a-z]+=\"", html), "仍有内联事件处理器"


def test_ui_page_references_external_assets(client) -> None:
    html = client.get(UI).text

    assert 'href="/static/literature.css"' in html
    assert 'src="/static/literature.js"' in html


# --- 静态资源 ---


@pytest.mark.parametrize(
    ("path", "media_type"),
    [(CSS, "text/css"), (JS, "text/javascript")],
)
def test_static_assets_served_with_correct_type(client, path: str, media_type: str) -> None:
    resp = client.get(path)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(media_type)
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert len(resp.content) > 1000, "静态资源内容过短，可能是空文件"


def test_css_declares_design_tokens(client) -> None:
    """间距/字号/圆角阶梯是本次观感改造的基础，缺失说明 CSS 被回退。"""
    css = client.get(CSS).text

    for token in ("--sp-1", "--fs-base", "--radius-lg"):
        assert token in css, f"缺少设计变量 {token}"


def test_js_has_no_third_party_imports(client) -> None:
    """零构建约束：不能出现 import/CDN。"""
    js = client.get(JS).text

    assert "from 'http" not in js and 'from "http' not in js
    assert "cdn." not in js
    assert "unpkg" not in js


# --- CSP 只作用于本页 ---


def test_ui_page_sends_strict_csp(client) -> None:
    csp = client.get(UI).headers.get("content-security-policy", "")

    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # 不允许不安全的内联放行
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp


def test_csp_is_not_applied_globally(client) -> None:
    """/docs 依赖 Swagger UI 的 CDN 资源，全局加 CSP 会把它打坏。"""
    assert "content-security-policy" not in client.get("/docs").headers
    assert "content-security-policy" not in client.get("/health").headers


def test_static_assets_do_not_send_csp(client) -> None:
    """CSP 是文档级策略，加在 CSS/JS 上没有意义。"""
    assert "content-security-policy" not in client.get(CSS).headers
    assert "content-security-policy" not in client.get(JS).headers


# --- 关键交互没被拆文件时弄丢 ---


def test_js_wires_logout_and_401_handling(client) -> None:
    """登出与 401 清理是本次认证交互修复的核心，缺一不可。"""
    js = client.get(JS).text

    assert "/auth/logout" in js, "缺少登出请求"
    assert "removeItem(TOKEN_KEY)" in js, "登出后未清理本地 token"
    assert "res.status === 401" in js, "未处理 401"


def test_js_does_not_claim_token_is_validated(client) -> None:
    """只保存本地凭据不能宣称「JWT 有效」（未经服务端校验）。"""
    js = client.get(JS).text

    assert "JWT 有效" not in js
    assert "已保存登录凭据" in js


def test_js_has_request_locking(client) -> None:
    """防连点必须存在，否则会重复评分/重复建卡。"""
    js = client.get(JS).text

    assert "aria-busy" in js
    assert "withBusy" in js


def test_js_escapes_interpolated_values(client) -> None:
    """列表/编辑器里插入服务端数据前必须转义。"""
    js = client.get(JS).text

    assert "function escapeHtml" in js
    assert "escapeAttr(" in js
    # 关键插值点抽查：PMID、状态、抽取来源都必须经过转义函数
    assert "escapeAttr(a.pmid)" in js
    assert "escapeHtml(d.status)" in js
    assert "escapeHtml(draft.extraction_source)" in js


def test_graph_layout_is_chunked_and_bounded(client) -> None:
    """O(n²) 布局必须分批执行并有节点上限，否则会锁死主线程。"""
    js = client.get(JS).text

    assert "requestAnimationFrame" in js
    assert "GRAPH_MAX_NODES" in js
    assert "GRAPH_ITERS_PER_FRAME" in js
