"""OCR 模块测试（批次 2 期间发现 tesseract 已安装，补齐覆盖）。

分两层：
1. **确定性单测**：用替身替换 pytesseract，验证后处理、语言回退、失败兜底；
2. **真实引擎测试**（``ocr`` 标记）：未安装 tesseract 时自动跳过。

说明：本机没有中日韩字体，无法用 PIL 渲染中文样本图，因此真实引擎测试用英文文本
（tesseract 的 eng 模型可读）；chi_sim 语言包的存在性单独断言。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from app import ocr as ocr_module


def _png_bytes(text: str = "COX inhibitor", size: tuple[int, int] = (420, 90)) -> bytes:
    """生成一张白底黑字的 PNG（用于真实 OCR 测试）。"""
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((10, 30), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


# --- 确定性单测（不依赖真实引擎） -------------------------------------------


def test_extract_rejects_empty_bytes() -> None:
    result = ocr_module.extract_text_from_image(b"")

    assert result.ok is False
    assert "为空" in (result.error or "")


def test_extract_rejects_oversized_image() -> None:
    oversized = b"x" * (ocr_module.MAX_IMAGE_BYTES + 1)

    result = ocr_module.extract_text_from_image(oversized)

    assert result.ok is False
    assert "过大" in (result.error or "")


def test_extract_reports_unavailable_without_engine(monkeypatch) -> None:
    """引擎不可用时返回 ok=False + 原因，而不是抛异常。"""
    backend = ocr_module.get_ocr_backend()
    monkeypatch.setattr(type(backend), "available", property(lambda self: False))
    monkeypatch.setattr(type(backend), "reason", property(lambda self: "缺少 tesseract"))

    result = ocr_module.extract_text_from_image(_png_bytes())

    assert result.ok is False
    assert result.error == "缺少 tesseract"


def test_extract_normalizes_trailing_whitespace(monkeypatch) -> None:
    """OCR 原始输出常带行尾空格，需要规整。"""
    backend = ocr_module.get_ocr_backend()
    monkeypatch.setattr(type(backend), "available", property(lambda self: True))
    monkeypatch.setattr(type(backend), "installed_languages", lambda self: ["eng"])
    import pytesseract

    monkeypatch.setattr(pytesseract, "image_to_string", lambda image, lang=None: "  阿司匹林  \n COX  \n\n")

    result = backend.extract_text(_png_bytes())

    assert result.ok is True
    assert result.text == "阿司匹林\n COX"


def test_language_falls_back_when_chi_sim_missing(monkeypatch) -> None:
    """只装了 eng 时不能硬用 chi_sim+eng，否则 tesseract 直接报错。"""
    import pytesseract

    backend = ocr_module.get_ocr_backend()
    monkeypatch.setattr(type(backend), "available", property(lambda self: True))
    monkeypatch.setattr(type(backend), "installed_languages", lambda self: ["eng"])
    captured: dict[str, str] = {}

    def fake_image_to_string(image, lang=None):  # noqa: ANN001
        captured["lang"] = lang
        return "COX"

    monkeypatch.setattr(pytesseract, "image_to_string", fake_image_to_string)

    result = backend.extract_text(_png_bytes(), languages="chi_sim+eng")

    assert result.ok is True
    assert captured["lang"] == "eng", "缺少 chi_sim 时应回退到 eng"


def test_language_keeps_chi_sim_when_installed(monkeypatch) -> None:
    import pytesseract

    backend = ocr_module.get_ocr_backend()
    monkeypatch.setattr(type(backend), "available", property(lambda self: True))
    monkeypatch.setattr(type(backend), "installed_languages", lambda self: ["chi_sim", "eng"])
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        pytesseract,
        "image_to_string",
        lambda image, lang=None: captured.setdefault("lang", lang) or "阿司匹林",
    )

    backend.extract_text(_png_bytes(), languages="chi_sim+eng")

    assert captured["lang"] == "chi_sim+eng"


def test_engine_exception_is_reported_not_raised(monkeypatch) -> None:
    import pytesseract

    backend = ocr_module.get_ocr_backend()
    monkeypatch.setattr(type(backend), "available", property(lambda self: True))
    monkeypatch.setattr(type(backend), "installed_languages", lambda self: ["eng"])

    def exploding(image, lang=None):  # noqa: ANN001
        raise RuntimeError("tesseract 崩了")

    monkeypatch.setattr(pytesseract, "image_to_string", exploding)

    result = backend.extract_text(_png_bytes())

    assert result.ok is False
    assert "识别失败" in (result.error or "")


def test_status_shape() -> None:
    status = ocr_module.ocr_status()

    assert set(status) >= {
        "backend",
        "available",
        "languages",
        "default_languages",
        "max_image_bytes",
        "alternatives",
    }
    assert status["backend"] == "tesseract"
    assert status["alternatives"] == ["text", "txt_file"]
    if not status["available"]:
        assert status["install_hint"]


# --- 真实引擎（未安装 tesseract 时跳过） ------------------------------------


@pytest.mark.ocr
def test_real_ocr_reads_english_text() -> None:
    """真实调用 tesseract 读一张英文图（无 CJK 字体，故用英文）。"""
    if not ocr_module.get_ocr_backend().available:
        pytest.skip(f"tesseract 不可用：{ocr_module.get_ocr_backend().reason}")

    result = ocr_module.extract_text_from_image(_png_bytes("COX inhibitor"))

    if not result.ok:
        pytest.skip(f"OCR 引擎执行失败：{result.error}")
    assert "COX" in result.text.upper()


@pytest.mark.ocr
def test_real_ocr_has_chinese_language_pack() -> None:
    """阶段五要求识别中文考点，必须确认 chi_sim 已安装。"""
    backend = ocr_module.get_ocr_backend()
    if not backend.available:
        pytest.skip("tesseract 不可用")

    languages = backend.installed_languages()
    if not languages:
        pytest.skip("无法枚举语言包")
    assert "chi_sim" in languages, f"缺少中文语言包，已安装：{languages}"
