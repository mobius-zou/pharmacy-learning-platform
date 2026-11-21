"""嵌入后端（阶段五）。

提供两种可互换的向量后端：

1. ``SentenceTransformerBackend`` —— 规范要求的 ``sentence-transformers`` 精筛；
2. ``HashingBackend`` —— **无 Agent 降级模式**，复用阶段三的离线确定性哈希嵌入，
   不下载模型、不联网、跨进程稳定。

选择逻辑：优先 sentence-transformers；未安装、模型加载失败或不允许下载时自动降级，
并通过 ``describe()`` 如实说明当前用的是哪一个以及降级原因。

两种后端输出都做 L2 归一化，因此余弦相似度可直接用点积计算。
"""

from __future__ import annotations

import importlib.util
import math
import os
import threading
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from .config import settings
from .logging_config import get_logger

logger = get_logger(__name__)


def _prepare_huggingface_env() -> None:
    """在导入 transformers 之前配置镜像与缓存目录。

    - ``HF_ENDPOINT``：本机无法直连 huggingface.co，默认走 hf-mirror.com；
    - ``HF_HOME``：默认放在项目内 ``.hf_cache``（用户主目录的缓存可能不可写）。
    """
    if settings.hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    os.environ.setdefault("HF_HOME", str(settings.hf_home))
    # hf-mirror 的部分文件会被重定向到 cas-bridge.xethub.hf.co（本机 DNS 不通），
    # 关闭 Xet 传输可强制走镜像的普通 HTTP 下载路径。
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    try:
        settings.hf_home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 目录不可写时不影响导入
        logger.warning("无法创建 HuggingFace 缓存目录 %s：%s", settings.hf_home, exc)


_prepare_huggingface_env()


@runtime_checkable
class EmbeddingBackend(Protocol):
    """向量后端接口。"""

    name: str

    @property
    def is_semantic(self) -> bool:
        """是否为真正的语义模型（False 表示降级的哈希嵌入）。"""
        ...

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """把文本编码为 **已 L2 归一化** 的向量。"""
        ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（输入需已归一化；这里仍做一次归一化以防万一）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


class HashingBackend:
    """离线确定性哈希嵌入（阶段三的 ``kg_service.embed_text``）。"""

    name = "hashing-offline"

    def __init__(self, dimension: int | None = None) -> None:
        self.dimension = dimension or settings.embedding_dim

    @property
    def is_semantic(self) -> bool:
        return False

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        from .services.kg_service import embed_text

        return [_l2_normalize(embed_text(text, self.dimension)) for text in texts]


class SentenceTransformerBackend:
    """sentence-transformers 语义嵌入（延迟加载模型）。"""

    name = "sentence-transformers"

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name or settings.st_model_name
        self._model: Any = None

    @property
    def is_semantic(self) -> bool:
        return True

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info(
                "加载 sentence-transformers 模型：%s（allow_download=%s）",
                self.model_name,
                settings.st_allow_download,
            )
            self._model = SentenceTransformer(
                self.model_name,
                local_files_only=not settings.st_allow_download,
            )
        return self._model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, vector)) for vector in vectors]


# --- 后端选择（进程内缓存，避免重复尝试加载模型） --------------------------

_resolved: EmbeddingBackend | None = None
_fallback_reason: str | None = None
_probe_done = False
# 模型加载锁：防止并发首次请求各加载一份模型（内存翻倍/OOM）
_resolve_lock = threading.Lock()


def _try_sentence_transformer() -> tuple[EmbeddingBackend | None, str | None]:
    if not settings.st_enabled:
        return None, "ST_ENABLED=false，已禁用 sentence-transformers"

    if not settings.st_model_name:
        return None, "未配置 ST_MODEL_NAME"

    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return None, "未安装 sentence-transformers"

    backend = SentenceTransformerBackend()
    try:
        # 真正实例化模型：只有成功才算可用（避免首次匹配时才报错）
        backend._load()  # noqa: SLF001 - 探测即预热
    except Exception as exc:  # 模型下载失败 / 无网络 / 版本不兼容
        return None, f"sentence-transformers 模型不可用：{type(exc).__name__}: {exc}"

    return backend, None


def resolve_backend(force_refresh: bool = False, allow_probe: bool = True) -> EmbeddingBackend:
    """返回当前可用的嵌入后端（进程内单例，**加锁**）。

    加载 sentence-transformers 模型要几百 MB 内存、十几秒时间。若不加锁，
    并发首次请求会各自加载一份模型（N 份 ≈ N×0.5GB），直接打爆内存。
    这里用双重检查锁保证**整个进程只加载一次**，其他线程等锁后复用同一实例。
    """
    global _resolved, _fallback_reason, _probe_done

    if _resolved is not None and not force_refresh:
        return _resolved

    with _resolve_lock:
        # 双重检查：等锁期间可能已有线程完成加载
        if _resolved is not None and not force_refresh:
            return _resolved

        if not allow_probe:
            # 不探测时直接给离线后端（用于不希望触发模型下载的场景）
            _resolved = HashingBackend()
            _fallback_reason = "未探测 sentence-transformers"
            return _resolved

        backend, reason = _try_sentence_transformer()
        if backend is None:
            logger.warning("嵌入后端降级为离线哈希：%s", reason)
            _resolved = HashingBackend()
            _fallback_reason = reason
        else:
            logger.info(
                "嵌入后端：%s (%s)",
                backend.name,
                getattr(backend, "model_name", None),
            )
            _resolved = backend
            _fallback_reason = None
        _probe_done = True
        return _resolved


def warmup_async() -> threading.Thread | None:
    """在**后台线程**预热嵌入后端，避免首个请求同步等待十几秒。

    返回线程对象（便于测试 join）；已加载时返回 None。
    """
    if _resolved is not None:
        return None

    def _load() -> None:
        try:
            backend = resolve_backend()
            logger.info("嵌入后端预热完成：%s", backend.name)
        except Exception:  # pragma: no cover - 预热失败不影响服务，首次使用时重试
            logger.exception("嵌入后端预热失败（将在首次使用时重试）")

    thread = threading.Thread(target=_load, name="embedding-warmup", daemon=True)
    thread.start()
    return thread


def reset_backend_cache() -> None:
    """清空缓存（测试用）。"""
    global _resolved, _fallback_reason, _probe_done
    _resolved = None
    _fallback_reason = None
    _probe_done = False


def _sentence_transformer_importable() -> bool:
    """判断包是否已安装，但**不真正执行导入**。

    ``import sentence_transformers`` 会连带加载 torch + transformers（数秒），
    而 ``find_spec`` 只查元数据，因此 ``/capabilities`` 能立即返回。
    """
    try:
        return importlib.util.find_spec("sentence_transformers") is not None
    except (ImportError, ValueError):  # 包损坏 / 路径异常
        return False


def describe(probe: bool = False) -> dict[str, Any]:
    """描述当前嵌入后端状态（供 API 暴露给前端）。

    ``probe=False``（默认）**不会加载模型**，只做廉价的可用性判断，
    因此 ``/capabilities`` 之类的接口不会因加载几百 MB 的模型而阻塞。
    真正的匹配路径会调用 ``resolve_backend()`` 完成加载。
    """
    if probe:
        backend = resolve_backend()
        return {
            "backend": backend.name,
            "is_semantic": backend.is_semantic,
            "model": getattr(backend, "model_name", None),
            "fallback_reason": _fallback_reason,
            "degraded": not backend.is_semantic,
            "loaded": True,
        }

    if _resolved is not None:
        return {
            "backend": _resolved.name,
            "is_semantic": _resolved.is_semantic,
            "model": getattr(_resolved, "model_name", None),
            "fallback_reason": _fallback_reason,
            "degraded": not _resolved.is_semantic,
            "loaded": True,
        }

    # 尚未解析：给出基于配置与依赖的预估，不加载模型
    importable = _sentence_transformer_importable()
    will_use_st = bool(settings.st_enabled and settings.st_model_name and importable)
    reason: str | None = None
    if not will_use_st:
        if not settings.st_enabled:
            reason = "ST_ENABLED=false，已禁用 sentence-transformers"
        elif not settings.st_model_name:
            reason = "未配置 ST_MODEL_NAME"
        elif not importable:
            reason = "未安装 sentence-transformers"

    return {
        "backend": "sentence-transformers" if will_use_st else "hashing-offline",
        "is_semantic": will_use_st,
        "model": settings.st_model_name if will_use_st else None,
        "fallback_reason": reason,
        "degraded": not will_use_st,
        "loaded": False,
    }
