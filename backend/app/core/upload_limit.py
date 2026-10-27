"""上传请求体大小限制（纯 ASGI 中间件）。

Starlette 解析 multipart 时会把整个上传先写进临时文件，路由里的 `file.read(limit + 1)` 拦不住超大请求体。
这里在解析之前拦截：Content-Length 超限直接 400；没有 Content-Length（分块传输）时边收边计数，
超限即抛 HTTPException（FastAPI 解析请求体时原样抛出，由统一错误处理返回 400）。
"""

import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import HTTPException

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# multipart 边界、字段头等额外开销
MULTIPART_OVERHEAD = 1024 * 1024


class UploadLimitMiddleware:
    def __init__(self, app: ASGIApp, *, rules: list[tuple[str, re.Pattern[str], int, str]]) -> None:
        """rules：(method, path 正则, 请求体字节上限, 超限时的中文 detail)。"""
        self.app = app
        self.rules = rules

    def _match(self, scope: Scope) -> tuple[int, str] | None:
        if scope.get("type") != "http":
            return None
        for method, pattern, limit, detail in self.rules:
            if scope.get("method") == method and pattern.fullmatch(scope.get("path", "")):
                return limit, detail
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        rule = self._match(scope)
        if rule is None:
            await self.app(scope, receive, send)
            return
        limit, detail = rule
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > limit:
            body = json.dumps({"detail": detail}, ensure_ascii=False).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=400, detail=detail)
            return message

        await self.app(scope, limited_receive, send)
