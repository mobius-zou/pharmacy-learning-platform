"""并发安全回归测试（批次 1）。

背景：所有惰性单例（DB 引擎、嵌入后端、ChromaDB 客户端、OCR 后端）原先都是
「检查全局变量 → 初始化」的无锁写法。并发首次请求会各自初始化一份，
其中嵌入模型每份约 0.5GB，N 个并发请求 = N×0.5GB → OOM。

这些测试用「慢加载器 + 多线程 + 屏障」放大竞态窗口，断言**只初始化一次**。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

import app.database as database
import app.embeddings as embeddings
import app.ocr as ocr
from app.services import kg_service

THREADS = 8


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    """每个用例前后清空缓存，避免 Fake 后端泄漏到其他测试。"""
    embeddings.reset_backend_cache()
    yield
    embeddings.reset_backend_cache()
    ocr.reset_ocr_backend()


def _run_concurrently(target, count: int = THREADS) -> list:
    """用屏障让 N 个线程尽可能同时进入目标函数。"""
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(count)

    def worker() -> None:
        barrier.wait()
        value = target()
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


class _FakeBackend:
    name = "fake-semantic"
    model_name = "fake-model"
    is_semantic = True

    def encode(self, texts):  # noqa: ANN001
        return [[1.0] for _ in texts]


# --- 嵌入后端（最严重的 OOM 风险） -----------------------------------------


def test_embedding_backend_loads_once_under_concurrency(monkeypatch) -> None:
    """并发首次请求只能加载一份模型 —— 否则内存翻 N 倍。"""
    load_calls: list[int] = []

    def slow_loader():  # noqa: ANN202
        load_calls.append(1)
        time.sleep(0.2)  # 放大竞态窗口
        return _FakeBackend(), None

    monkeypatch.setattr(embeddings, "_try_sentence_transformer", slow_loader)

    results = _run_concurrently(embeddings.resolve_backend)

    assert len(load_calls) == 1, f"模型应只加载一次，实际加载 {len(load_calls)} 次"
    assert len({id(item) for item in results}) == 1, "所有线程必须拿到同一个后端实例"
    assert results[0].name == "fake-semantic"


def test_embedding_backend_cache_is_used_after_first_load(monkeypatch) -> None:
    """已加载后不再触发任何加载。"""
    load_calls: list[int] = []

    def loader():  # noqa: ANN202
        load_calls.append(1)
        return _FakeBackend(), None

    monkeypatch.setattr(embeddings, "_try_sentence_transformer", loader)

    for _ in range(5):
        embeddings.resolve_backend()

    assert len(load_calls) == 1


def test_degraded_backend_is_also_cached_once(monkeypatch) -> None:
    """降级路径同样只探测一次（避免每次都尝试加载失败的模型）。"""
    calls: list[int] = []

    def failing_loader():  # noqa: ANN202
        calls.append(1)
        time.sleep(0.1)
        return None, "模型不可用"

    monkeypatch.setattr(embeddings, "_try_sentence_transformer", failing_loader)

    results = _run_concurrently(embeddings.resolve_backend)

    assert len(calls) == 1
    assert all(item.name == "hashing-offline" for item in results)


def test_warmup_async_resolves_in_background(monkeypatch) -> None:
    """后台预热应最终完成加载，且不阻塞调用方。"""
    monkeypatch.setattr(
        embeddings, "_try_sentence_transformer", lambda: (_FakeBackend(), None)
    )

    thread = embeddings.warmup_async()

    assert thread is not None
    thread.join(timeout=10)
    assert embeddings.resolve_backend().name == "fake-semantic"


def test_warmup_async_noop_when_already_loaded(monkeypatch) -> None:
    monkeypatch.setattr(
        embeddings, "_try_sentence_transformer", lambda: (_FakeBackend(), None)
    )
    embeddings.resolve_backend()

    assert embeddings.warmup_async() is None


def test_warmup_failure_does_not_raise(monkeypatch) -> None:
    """预热失败不能影响服务启动。"""
    def exploding():  # noqa: ANN202
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(embeddings, "_try_sentence_transformer", exploding)

    thread = embeddings.warmup_async()
    assert thread is not None
    thread.join(timeout=10)  # 不应抛出到主线程


# --- 数据库引擎 -------------------------------------------------------------


def test_get_engine_is_thread_safe(monkeypatch) -> None:
    """并发首次请求只能创建一个引擎（连接池）。"""
    database.reset_engine_cache()
    built: list[int] = []
    real_build = database.build_engine

    def counting_build(*args, **kwargs):  # noqa: ANN002, ANN003
        built.append(1)
        time.sleep(0.15)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(database, "build_engine", counting_build)
    try:
        results = _run_concurrently(database.get_engine)
        assert len(built) == 1, f"引擎应只创建一次，实际 {len(built)} 次"
        assert len({id(item) for item in results}) == 1
    finally:
        database.reset_engine_cache()


def test_get_session_factory_is_thread_safe(monkeypatch) -> None:
    database.reset_engine_cache()
    try:
        results = _run_concurrently(database.get_session_factory)
        assert len({id(item) for item in results}) == 1
    finally:
        database.reset_engine_cache()


# --- ChromaDB / OCR 单例 ----------------------------------------------------


def test_chroma_client_is_thread_safe(monkeypatch) -> None:
    monkeypatch.setattr(kg_service, "_client", None)
    try:
        results = _run_concurrently(kg_service.get_chroma_client)
        assert len({id(item) for item in results}) == 1
    finally:
        kg_service.reset_chroma_client()


def test_ocr_backend_is_thread_safe() -> None:
    ocr.reset_ocr_backend()
    try:
        results = _run_concurrently(ocr.get_ocr_backend)
        assert len({id(item) for item in results}) == 1
    finally:
        ocr.reset_ocr_backend()
