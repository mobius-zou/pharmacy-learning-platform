"""进程内限流（批次 4）。

用于登录等敏感端点的失败次数限制：滑动窗口 + 锁定。

设计取舍
--------
- **进程内**：不引入 Redis 等外部依赖。多 worker 部署时每个 worker 各算一份，
  实际阈值约为 ``max_attempts × worker 数``；单进程部署则精确。
  这点已在文档与日志中说明，不假装它是分布式限流。
- **只计失败**：成功登录会清零计数，避免正常用户被自己的成功操作拖累。
- **内存有界**：过期条目会被清理，并设置键数量上限，防止被海量用户名打爆。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from .logging_config import get_logger

logger = get_logger(__name__)

# 键数量上限：超出后淘汰最久未使用的键，避免内存无界增长
MAX_TRACKED_KEYS = 10_000


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    """一次限流检查的结果。"""

    allowed: bool
    remaining: int
    retry_after: int = 0

    @property
    def limited(self) -> bool:
        return not self.allowed


class SlidingWindowLimiter:
    """按 key 统计失败次数的滑动窗口限流器（线程安全）。"""

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        lockout_seconds: float = 300.0,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self.lockout_seconds = max(1.0, float(lockout_seconds))
        self.max_keys = max(1, int(max_keys))

        self._lock = threading.Lock()
        # key -> 最近失败时间戳（窗口内）
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        # key -> 锁定到期时间
        self._locked_until: dict[str, float] = {}

    # -- 内部（调用方需持锁） --

    def _prune(self, key: str, now: float) -> deque[float]:
        window = self._failures.get(key)
        if window is None:
            window = deque()
            self._failures[key] = window
        cutoff = now - self.window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        if not window:
            self._failures.pop(key, None)
            return deque()
        return window

    def _evict_if_needed(self) -> None:
        while len(self._failures) > self.max_keys:
            self._failures.popitem(last=False)

    # -- 对外接口 --

    def check(self, key: str, now: float | None = None) -> RateLimitStatus:
        """检查是否被限流（不计数）。"""
        moment = time.monotonic() if now is None else now
        with self._lock:
            locked_until = self._locked_until.get(key)
            if locked_until is not None:
                if locked_until > moment:
                    return RateLimitStatus(
                        allowed=False, remaining=0, retry_after=int(locked_until - moment) + 1
                    )
                self._locked_until.pop(key, None)

            window = self._prune(key, moment)
            remaining = self.max_attempts - len(window)
            return RateLimitStatus(allowed=True, remaining=max(0, remaining))

    def record_failure(self, key: str, now: float | None = None) -> RateLimitStatus:
        """记录一次失败；达到阈值则进入锁定。"""
        moment = time.monotonic() if now is None else now
        with self._lock:
            window = self._prune(key, moment)
            window.append(moment)
            self._failures[key] = window
            self._evict_if_needed()

            count = len(window)
            if count >= self.max_attempts:
                self._locked_until[key] = moment + self.lockout_seconds
                logger.warning(
                    "触发限流 key=%s 失败 %d 次，锁定 %.0f 秒", key, count, self.lockout_seconds
                )
                return RateLimitStatus(
                    allowed=False, remaining=0, retry_after=int(self.lockout_seconds) + 1
                )
            return RateLimitStatus(allowed=True, remaining=self.max_attempts - count)

    def reset(self, key: str) -> None:
        """登录成功后清零该 key 的失败记录。"""
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def clear(self) -> None:
        """清空全部状态（测试用）。"""
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()

    @property
    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._failures)


# 全局限流器：登录失败限流（由 config 决定阈值）
_login_limiter: SlidingWindowLimiter | None = None
_limiter_lock = threading.Lock()


def get_login_limiter() -> SlidingWindowLimiter:
    """登录限流器单例（加锁，惰性按配置构造）。"""
    global _login_limiter
    if _login_limiter is None:
        with _limiter_lock:
            if _login_limiter is None:
                from .config import settings

                _login_limiter = SlidingWindowLimiter(
                    max_attempts=settings.login_max_attempts,
                    window_seconds=settings.login_window_seconds,
                    lockout_seconds=settings.login_lockout_seconds,
                )
    return _login_limiter


def reset_login_limiter(limiter: SlidingWindowLimiter | None = None) -> None:
    """替换/清空登录限流器（测试用）。"""
    global _login_limiter
    with _limiter_lock:
        _login_limiter = limiter


# 注册限流器（批量刷号会触发大量 bcrypt 哈希，属 CPU 放大）
_register_limiter: SlidingWindowLimiter | None = None


def get_register_limiter() -> SlidingWindowLimiter:
    global _register_limiter
    if _register_limiter is None:
        with _limiter_lock:
            if _register_limiter is None:
                from .config import settings

                _register_limiter = SlidingWindowLimiter(
                    max_attempts=settings.register_max_per_window,
                    window_seconds=settings.login_window_seconds,
                    lockout_seconds=settings.login_lockout_seconds,
                )
    return _register_limiter


def reset_register_limiter(limiter: SlidingWindowLimiter | None = None) -> None:
    global _register_limiter
    with _limiter_lock:
        _register_limiter = limiter


def client_key(request, username: str = "") -> str:
    """限流 key：用户名 + 客户端地址（两者任一维度被滥用都能拦住）。"""
    host = request.client.host if getattr(request, "client", None) else "unknown"
    return f"{username.strip().lower()}|{host}"
