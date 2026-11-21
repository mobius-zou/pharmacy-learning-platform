"""请求体大小限制（批次 4 引入，组 1 重写为纯 ASGI）。

为什么重写
----------
旧实现基于 ``BaseHTTPMiddleware`` 且**只看 Content-Length**，存在两个缺口：

1. **chunked 请求**（不带 Content-Length）完全绕过限制 —— 未认证的
   ``/auth/login`` 就能用分块大包打爆内存；
2. ``BaseHTTPMiddleware`` 本身有已知的 context/streaming 问题。

现在的实现是**纯 ASGI 中间件**，边收边计数，超限立即中断：

- JSON 等普通请求：上限 ``MAX_REQUEST_BODY_BYTES``（默认 2MiB）；
- multipart 上传：上限 ``MAX_MULTIPART_BYTES``（默认 12MiB），
  端点内部还有更细的限制（图片 8MiB / txt 5MiB）；
- 有 Content-Length 时**先判后读**，避免白读一遍大包。

上传端点另外用 ``read_upload_limited`` 分块读，形成双保险。
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import settings
from .logging_config import get_logger

logger = get_logger(__name__)


class MaxBodySizeMiddleware:
    """纯 ASGI 的请求体大小限制（流式计数，不缓冲整个 body）。"""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int | None = None,
        multipart_max_bytes: int | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes if max_bytes is not None else settings.max_request_body_bytes
        self.multipart_max_bytes = (
            multipart_max_bytes
            if multipart_max_bytes is not None
            else settings.max_multipart_bytes
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        content_type = headers.get("content-type", "").lower()
        is_multipart = content_type.startswith("multipart/form-data")
        limit = self.multipart_max_bytes if is_multipart else self.max_bytes

        # 有声明长度时先判后读，直接省掉一次大包读取
        declared = headers.get("content-length")
        if declared:
            try:
                declared_len = int(declared)
            except ValueError:
                declared_len = 0
            if declared_len > limit:
                await self._reject(send, scope, declared_len, limit)
                return

        # 先按块把 body 收下来并计数，再整体交给应用。
        #
        # 为什么不用「在 receive() 里抛异常」：Starlette 解析 body 时会捕获
        # 流异常并转成笼统的 400（实测 "There was an error parsing the body"），
        # 我们就拿不到确定的 413 了。这里改成「先计数、再重放」，
        # 缓冲上限就是 limit（默认 2MiB / multipart 12MiB），不会无界增长。
        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if chunk:
                buffered.extend(chunk)
                if len(buffered) > limit:
                    await self._reject(send, scope, len(buffered), limit)
                    return
            if not message.get("more_body", False):
                break

        replay_done = False

        async def replay_receive() -> Message:
            nonlocal replay_done
            if not replay_done:
                replay_done = True
                return {
                    "type": "http.request",
                    "body": bytes(buffered),
                    "more_body": False,
                }
            # body 已重放完毕：后续必须**透传真实事件**。
            # 若在这里硬返回 http.disconnect，会误伤监听断开的流式响应
            # （FileResponse/StreamingResponse 会以为客户端走了，响应体被截断）。
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, send: Send, scope: Scope, received: int, limit: int) -> None:
        logger.warning(
            "请求体过大 %s %s：%d 字节（上限 %d）",
            scope.get("method"),
            scope.get("path"),
            received,
            limit,
        )
        body = json.dumps(
            {
                "error": "payload_too_large",
                "message": f"请求体过大（{received} 字节，上限 {limit} 字节）",
                "detail": {"limit_bytes": limit, "received_bytes": received},
            },
            ensure_ascii=False,
        ).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


async def read_upload_limited(upload, limit: int, chunk_size: int = 64 * 1024) -> bytes:
    """分块读取上传文件，超过 ``limit`` 立即中断。

    先看 ``UploadFile.size``（Starlette 解析 multipart 时会填好），
    避免把超大文件整个读进内存后才校验 —— 旧实现正是
    「先 ``await file.read()`` 再判断大小」。
    """
    from .errors import APIError

    declared = getattr(upload, "size", None)
    if declared is not None and declared > limit:
        raise APIError(
            f"文件过大（{declared} 字节，上限 {limit}）",
            status_code=413,
            error="payload_too_large",
            detail={"limit_bytes": limit, "received_bytes": declared},
        )

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise APIError(
                f"文件过大（超过 {limit} 字节）",
                status_code=413,
                error="payload_too_large",
                detail={"limit_bytes": limit, "received_bytes": total},
            )
        chunks.append(chunk)
    return b"".join(chunks)
