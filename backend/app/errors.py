"""统一错误响应。

约定响应体：``{"error": "", "message": "", "detail": {}}``
所有未捕获异常统一返回 500 并记录堆栈。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging_config import get_logger
from .tags import TagValidationError

logger = get_logger(__name__)


def error_payload(error: str, message: str, detail: Any = None) -> dict[str, Any]:
    """构造统一错误响应体。"""
    return {"error": error, "message": message, "detail": detail if detail is not None else {}}


class APIError(Exception):
    """业务异常：携带 HTTP 状态码与错误码。"""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        error: str = "bad_request",
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error = error
        self.detail = detail if detail is not None else {}


class NotFoundError(APIError):
    def __init__(self, message: str = "资源不存在", detail: Any = None) -> None:
        super().__init__(message, status.HTTP_404_NOT_FOUND, "not_found", detail)


class ConflictError(APIError):
    def __init__(self, message: str = "资源冲突", detail: Any = None) -> None:
        super().__init__(message, status.HTTP_409_CONFLICT, "conflict", detail)


class UnauthorizedError(APIError):
    def __init__(self, message: str = "未认证或登录已过期", detail: Any = None) -> None:
        super().__init__(message, status.HTTP_401_UNAUTHORIZED, "unauthorized", detail)


class ForbiddenError(APIError):
    def __init__(self, message: str = "无权访问该资源", detail: Any = None) -> None:
        super().__init__(message, status.HTTP_403_FORBIDDEN, "forbidden", detail)


class ValidationError(APIError):
    def __init__(self, message: str = "数据校验失败", detail: Any = None) -> None:
        # 422 语义为 UNPROCESSABLE_CONTENT；用字面量避免跟随 Starlette 常量改名
        super().__init__(message, 422, "validation_error", detail)


# HTTP 状态码 -> 错误码
_STATUS_TO_ERROR = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    429: "too_many_requests",
}


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(TagValidationError)
    async def _handle_tag_validation(
        request: Request, exc: TagValidationError
    ) -> JSONResponse:
        """标签不合规属于**校验错误**，统一返回 422 而不是 500。"""
        logger.warning("标签校验 %s %s -> 422: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "message": str(exc), "detail": {}},
        )

    @app.exception_handler(APIError)
    async def _handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        logger.warning(
            "业务异常 %s %s -> %s: %s", request.method, request.url.path, exc.status_code, exc.message
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.error, exc.message, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.info("参数校验失败 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=422,  # UNPROCESSABLE_CONTENT
            content=error_payload(
                "validation_error",
                "请求参数校验失败",
                {"errors": _jsonable_errors(exc.errors())},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        error = _STATUS_TO_ERROR.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(error, str(exc.detail), {}),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 统一返回 500 并记录完整堆栈
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_payload(
                "internal_server_error",
                # 不回传异常类名：那属于内部实现细节，会帮助攻击者判断
                # 是哪个组件出错（审计 #20）。完整堆栈只写日志。
                "服务器内部错误，请稍后重试或联系管理员",
                {},
            ),
        )


def _jsonable_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 pydantic 的校验错误整理成可 JSON 序列化的形式。"""
    out: list[dict[str, Any]] = []
    for item in errors:
        out.append(
            {
                "loc": [str(part) for part in item.get("loc", [])],
                "msg": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
        )
    return out
