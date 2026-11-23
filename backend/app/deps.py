"""FastAPI 依赖：认证、会话、统一异常包裹。"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .errors import APIError, UnauthorizedError
from .logging_config import get_logger
from .models import User
from .security import TokenError, decode_access_token

logger = get_logger(__name__)

# auto_error=False：缺少 Authorization 头时由我们自己返回统一错误体
bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access_token>")

F = TypeVar("F", bound=Callable[..., Any])


def _extract_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """取访问令牌：Authorization 头优先，其次 HttpOnly Cookie。

    两种方式并存是为了平滑迁移：
    - 正式 SPA 用 Cookie（前端拿不到 token，XSS 无法窃取）；
    - 既有的 /ui/literature 单页与测试脚本继续用 Bearer。
    """
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    value = request.cookies.get(settings.cookie_name)
    return value.strip() if value and value.strip() else None


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """解析令牌并返回当前用户（Bearer 头或 HttpOnly Cookie）。"""
    token = _extract_token(request, credentials)
    if not token:
        raise UnauthorizedError("缺少访问令牌，请先登录")

    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise UnauthorizedError(str(exc)) from exc

    subject = payload.get("sub")
    try:
        user_id = int(subject)
    except (TypeError, ValueError) as exc:
        raise UnauthorizedError("访问令牌载荷非法") from exc

    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None:
        raise UnauthorizedError("用户不存在或已被删除")

    # 令牌版本比对：登出后旧令牌立即失效（缺失 ver 视为 0，兼容早期令牌）
    if int(payload.get("ver", 0)) != int(user.token_version or 0):
        raise UnauthorizedError("登录状态已失效，请重新登录")
    return user


def api_guard(func: F) -> F:
    """把 API 端点包裹在 try/except 中。

    - ``APIError`` 原样抛出，交给全局处理器输出统一错误体；
    - 其他异常记录完整堆栈后转为 500。
    同时兼容同步与异步端点。
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except APIError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一兜底
                logger.exception("端点异常 %s", func.__qualname__)
                raise APIError(
                    "服务器内部错误", status_code=500, error="internal_server_error",
                    detail={"exception": type(exc).__name__},
                ) from exc

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except APIError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一兜底
            logger.exception("端点异常 %s", func.__qualname__)
            raise APIError(
                "服务器内部错误", status_code=500, error="internal_server_error",
                detail={"exception": type(exc).__name__},
            ) from exc

    return sync_wrapper  # type: ignore[return-value]


def client_info(request: Request) -> str:
    """日志用：客户端地址。"""
    return request.client.host if request.client else "unknown"
