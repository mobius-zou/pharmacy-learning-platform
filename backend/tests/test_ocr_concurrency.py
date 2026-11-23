"""OCR 并发闸门的回归测试（并发图片导入增强）。

每次 OCR 都会起一个 tesseract 子进程。图片批量导入时若不限流，
几十个并发请求会打满 CPU，让所有接口一起变慢。闸门要求：
  · 同时识别数不超过 ``OCR_MAX_CONCURRENCY``；
  · 排队超时后返回「繁忙」而不是无限等待；
  · 「繁忙」与「识别失败」在接口层要区分（503 vs 422）。
"""

from __future__ import annotations

import threading
import time

import pytest

from app import ocr


@pytest.fixture(autouse=True)
def _reset_slots():
    yield
    ocr.reset_ocr_backend()
    ocr.reset_ocr_slots(1)


class _SlowBackend:
    """记录并发峰值的假后端。"""

    name = "fake"

    def __init__(self, delay: float = 0.12) -> None:
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.calls = 0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return True

    @property
    def reason(self) -> str | None:
        return None

    def installed_languages(self) -> list[str]:
        return ["eng"]

    def extract_text(self, image_bytes: bytes, languages: str = "eng"):  # noqa: ANN001
        with self._lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return ocr.OCRResult(ok=True, text="识别结果", backend=self.name, languages=languages)
        finally:
            with self._lock:
                self.active -= 1


def _run_concurrently(backend, count: int) -> list:
    results: list = [None] * count

    def worker(index: int) -> None:
        results[index] = ocr.extract_text_from_image(b"fake-image-bytes", languages="eng")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_concurrency_is_capped(monkeypatch) -> None:
    """并发上限为 2 时，峰值并发不得超过 2。"""
    from app.config import settings

    monkeypatch.setattr(settings, "ocr_max_concurrency", 2)
    monkeypatch.setattr(settings, "ocr_queue_timeout_seconds", 30.0)
    ocr.reset_ocr_slots(2)
    backend = _SlowBackend(delay=0.08)
    monkeypatch.setattr(ocr, "get_ocr_backend", lambda: backend)

    results = _run_concurrently(backend, count=6)

    assert all(result is not None and result.ok for result in results), "全部应成功"
    assert backend.peak <= 2, f"峰值并发 {backend.peak} 超过上限 2"
    assert backend.calls == 6


def test_busy_result_when_queue_times_out(monkeypatch) -> None:
    """排队超时返回「繁忙」，且标记 busy=True（接口层据此回 503）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "ocr_max_concurrency", 1)
    monkeypatch.setattr(settings, "ocr_queue_timeout_seconds", 0.0)
    ocr.reset_ocr_slots(1)
    backend = _SlowBackend(delay=0.4)
    monkeypatch.setattr(ocr, "get_ocr_backend", lambda: backend)

    # 先占住唯一名额
    holder = threading.Thread(
        target=lambda: ocr.extract_text_from_image(b"img", languages="eng")
    )
    holder.start()
    time.sleep(0.05)

    busy = ocr.extract_text_from_image(b"img", languages="eng")

    holder.join(timeout=10)
    assert busy.ok is False
    assert busy.busy is True
    assert "繁忙" in (busy.error or "")


def test_slot_is_released_when_backend_raises(monkeypatch) -> None:
    """后端抛异常也必须释放名额，否则后续请求永久堵塞。"""
    from app.config import settings

    monkeypatch.setattr(settings, "ocr_max_concurrency", 1)
    monkeypatch.setattr(settings, "ocr_queue_timeout_seconds", 0.0)
    ocr.reset_ocr_slots(1)

    class _Boom:
        name = "boom"

        @property
        def available(self) -> bool:
            return True

        def installed_languages(self) -> list[str]:
            return ["eng"]

        def extract_text(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("模拟崩溃")

    monkeypatch.setattr(ocr, "get_ocr_backend", lambda: _Boom())

    with pytest.raises(RuntimeError):
        ocr.extract_text_from_image(b"img", languages="eng")

    # 名额已释放：下一次调用不会因为「繁忙」而失败（仍会抛同样的异常）
    with pytest.raises(RuntimeError):
        ocr.extract_text_from_image(b"img", languages="eng")


def test_load_is_reported(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "ocr_max_concurrency", 3)

    load = ocr.ocr_load()

    assert load["max_concurrency"] == 3
    assert load["waiting"] == 0


def test_capabilities_exposes_concurrency(client, auth_headers) -> None:
    """能力上报要带上并发上限，前端据此提示「请逐个上传」。"""
    resp = client.get("/api/v1/exam-points/capabilities", headers=auth_headers)

    assert resp.status_code == 200
    ocr_block = resp.json()["ocr"]
    assert "concurrency" in ocr_block
    assert ocr_block["concurrency"]["max_concurrency"] >= 1


def test_install_hint_still_reported_when_unavailable(monkeypatch, client, auth_headers) -> None:
    """OCR 不可用时仍要给出安装指引（不因并发改造而丢失）。"""
    class _Missing:
        name = "missing"

        @property
        def available(self) -> bool:
            return False

        @property
        def reason(self) -> str:
            return "缺少 tesseract 系统二进制"

        def installed_languages(self) -> list[str]:
            return []

    monkeypatch.setattr(ocr, "get_ocr_backend", lambda: _Missing())

    resp = client.get("/api/v1/exam-points/capabilities", headers=auth_headers)

    body = resp.json()["ocr"]
    assert body["available"] is False
    assert "apt-get install tesseract-ocr" in body["install_hint"]
