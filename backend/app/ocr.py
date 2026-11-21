"""OCR 模块（阶段五）：PPT 截图 / 试卷照片 → 文本。

设计
----
OCR 需要一个**系统级引擎**（当前实现基于 Tesseract）。本模块把引擎抽象成
可插拔后端，并如实报告可用性：

- 后端可用 → 正常识别（中英混排，优先 chi_sim+eng）；
- 后端不可用 → 返回明确的不可用原因与修复指引，**不抛 500**，
  同时保留「粘贴文本 / 上传 txt 文件」这条始终可用的导入路径。

依赖分层：
- ``pytesseract``（Python 包，已在 requirements 中声明）
- ``tesseract``（系统二进制，需要 ``apt-get install tesseract-ocr tesseract-ocr-chi-sim``）
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .logging_config import get_logger

logger = get_logger(__name__)

# 识别语言：中文简体 + 英文（药学考点常见中英混排）
DEFAULT_LANGUAGES = "chi_sim+eng"

# 单张图片大小上限（8 MiB）
MAX_IMAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OCRResult:
    """一次 OCR 的结果。"""

    ok: bool
    text: str = ""
    backend: str = ""
    error: str | None = None
    languages: str = ""


@runtime_checkable
class OCRBackend(Protocol):
    """OCR 后端接口。"""

    name: str

    @property
    def available(self) -> bool: ...

    def extract_text(self, image_bytes: bytes, languages: str = ...) -> OCRResult: ...


class TesseractOCR:
    """基于 pytesseract + Tesseract 二进制的 OCR 后端。"""

    name = "tesseract"

    def __init__(self) -> None:
        self._available: bool | None = None
        self._reason: str | None = None
        # 语言包枚举会起一次 ``tesseract --list-langs`` 子进程，代价不低；
        # 这里缓存结果，避免每次 OCR 都重复执行（原先每次识别都调一次）。
        self._languages: list[str] | None = None

    def _probe(self) -> None:
        if self._available is not None:
            return
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            self._available = False
            self._reason = "未安装 pytesseract"
            return

        try:
            import pytesseract

            version = pytesseract.get_tesseract_version()
            self._available = True
            self._reason = None
            logger.info("Tesseract 可用：%s", version)
        except Exception as exc:  # 二进制缺失 / 未安装 / 权限问题
            self._available = False
            self._reason = (
                f"缺少 tesseract 系统二进制（{type(exc).__name__}）。"
                "请安装：apt-get install tesseract-ocr tesseract-ocr-chi-sim"
            )
            logger.warning("Tesseract 不可用：%s", self._reason)

    @property
    def available(self) -> bool:
        self._probe()
        return bool(self._available)

    @property
    def reason(self) -> str | None:
        self._probe()
        return self._reason

    def installed_languages(self) -> list[str]:
        """枚举已安装的语言包（结果缓存，避免每次 OCR 都起子进程）。"""
        if self._languages is not None:
            return list(self._languages)
        if not self.available:
            return []
        try:
            import pytesseract

            self._languages = list(pytesseract.get_languages(config=""))
        except Exception:  # pragma: no cover - 探测失败不影响主流程
            self._languages = []
        return list(self._languages)

    def extract_text(self, image_bytes: bytes, languages: str = DEFAULT_LANGUAGES) -> OCRResult:
        if not self.available:
            # 用 reason 属性（而非私有 _reason）：保证未探测过时也能给出原因
            return OCRResult(ok=False, backend=self.name, error=self.reason)

        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            return OCRResult(ok=False, backend=self.name, error=f"缺少依赖：{exc}")

        try:
            image = Image.open(io.BytesIO(image_bytes))
            # 统一转 RGB：处理带透明通道的 PNG 截图
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")

            installed = self.installed_languages()
            usable = languages
            if installed:
                # 缺 chi_sim 时退回 eng，避免直接失败
                wanted = [lang for lang in languages.split("+") if lang in installed]
                if wanted:
                    usable = "+".join(wanted)
                elif "eng" in installed:
                    usable = "eng"

            text = pytesseract.image_to_string(image, lang=usable)
            cleaned = "\n".join(line.rstrip() for line in text.splitlines()).strip()
            return OCRResult(
                ok=True, text=cleaned, backend=self.name, languages=usable
            )
        except Exception as exc:
            logger.warning("OCR 识别失败：%s: %s", type(exc).__name__, exc)
            return OCRResult(ok=False, backend=self.name, error=f"识别失败：{type(exc).__name__}: {exc}")


_backend: TesseractOCR | None = None
_backend_lock = threading.Lock()


def get_ocr_backend() -> TesseractOCR:
    """当前 OCR 后端（进程内单例，加锁）。"""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = TesseractOCR()
    return _backend


def reset_ocr_backend() -> None:
    """清空缓存（测试用）。"""
    global _backend
    _backend = None


def ocr_status() -> dict[str, Any]:
    """OCR 可用性状态（供 API 暴露）。"""
    backend = get_ocr_backend()
    available = backend.available
    status: dict[str, Any] = {
        "backend": backend.name,
        "available": available,
        "languages": backend.installed_languages() if available else [],
        "default_languages": DEFAULT_LANGUAGES,
        "max_image_bytes": MAX_IMAGE_BYTES,
        "alternatives": ["text", "txt_file"],
    }
    if not available:
        status["reason"] = backend.reason
        status["install_hint"] = (
            "apt-get install tesseract-ocr tesseract-ocr-chi-sim"
            "（Debian/Ubuntu，需要管理员权限）"
        )
    return status


def extract_text_from_image(image_bytes: bytes, languages: str = DEFAULT_LANGUAGES) -> OCRResult:
    """识别图片文本；后端不可用时返回 ``ok=False`` 而不是抛异常。"""
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return OCRResult(
            ok=False,
            error=f"图片过大（{len(image_bytes)} 字节，上限 {MAX_IMAGE_BYTES}）",
        )
    if not image_bytes:
        return OCRResult(ok=False, error="图片内容为空")
    return get_ocr_backend().extract_text(image_bytes, languages=languages)
