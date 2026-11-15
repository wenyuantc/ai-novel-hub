"""Server-Sent Events 输出。

事件格式：
    event: <name>
    data: <JSON>

data 一律 JSON 编码，避免正文中的换行破坏 SSE 帧。事件名为空串表示心跳，渲染为注释帧 ": ping"。
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, aclosing

import anyio
from fastapi.responses import StreamingResponse

from app.core.errors import ForbiddenError

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # 关闭 Nginx 等反代的响应缓冲，保证增量到达前端
    "X-Accel-Buffering": "no",
}


def format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class _FrameGuardedResponse(StreamingResponse):
    """仅保护指定帧；取锁、ASGI 发送及解锁始终发生在同一协程中。"""

    def __init__(
        self, events: AsyncIterator[tuple[str, dict]], frame_lease: Callable[[], AbstractAsyncContextManager[None]],
        frame_timeout: float,
    ) -> None:
        super().__init__(events, media_type="text/event-stream", headers=SSE_HEADERS)
        self._events = events
        self._frame_lease = frame_lease
        self._frame_timeout = frame_timeout

    async def _send_guarded(self, send, chunk: bytes) -> str:
        entered = False
        try:
            # 连同取锁和复验一起限时，避免已取得的用户锁在等待其他锁时无限阻塞撤权。
            with anyio.fail_after(self._frame_timeout):
                async with self._frame_lease():
                    entered = True
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            return "sent"
        except TimeoutError:
            # 慢客户端可能已收到部分字节；结束流并释放锁，不重试本帧。
            return "abort"
        except Exception as exc:
            if entered:
                # 发送或释放期间的异常不能包含正文或 SQL，也不能尝试重复发送。
                if isinstance(exc, OSError):
                    raise OSError("改写流连接已断开") from None
                raise RuntimeError("改写流传输中断") from None
            # 未取得有效授权时只输出固定安全错误，不泄漏产生该错误的 SQL 或正文。
            # 向业务生成器投递同一安全失败，使它保留错误日志语义并执行原 finally。
            throw = getattr(self._events, "athrow", None)
            if throw is not None:
                try:
                    await throw(ForbiddenError("权限或模型授权已变化，已停止发送", extra={"code": "permission_denied"}))
                except Exception:
                    pass
            error = format_sse("error", {"detail": "权限或模型授权已变化，已停止发送",
                                         "code": "permission_denied"}).encode("utf-8")
            try:
                with anyio.fail_after(self._frame_timeout):
                    await send({"type": "http.response.body", "body": error, "more_body": True})
            except (TimeoutError, OSError):
                return "abort"
            except Exception:
                raise RuntimeError("改写流传输中断") from None
            return "error"

    async def stream_response(self, send) -> None:
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            await send({"type": "http.response.body", "body": b": stream-start\n\n", "more_body": True})
            async for event, data in self._events:
                chunk = (format_sse(event, data) if event else ": ping\n\n").encode("utf-8")
                if event in {"delta", "done"}:
                    outcome = await self._send_guarded(send, chunk)
                    if outcome == "abort":
                        return
                    if outcome == "error":
                        # 先完成业务 finally/错误日志，再告知客户端响应已结束。
                        await self._close_events()
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                        return
                else:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await self._close_events()

    async def _close_events(self) -> None:
        # 断连/取消时也在当前 task 中结束业务生成器，确保其 finally 和资源清理完成。
        failed = False
        with anyio.CancelScope(shield=True):
            try:
                await self._events.aclose()
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("改写流清理失败") from None


def sse_response(
    events: AsyncIterator[tuple[str, dict]], *,
    frame_lease: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    frame_timeout: float = 5.0,
) -> StreamingResponse:
    if frame_lease is not None:
        return _FrameGuardedResponse(events, frame_lease, frame_timeout)

    async def body() -> AsyncIterator[str]:
        # aclosing：响应被中断（客户端断开）时确定性地关闭业务生成器，
        # 让其 finally 立即执行（例如回退章节状态），而不是等到垃圾回收
        async with aclosing(events) as stream:
            # 先发一条注释帧，促使代理/浏览器尽早建立流
            yield ": stream-start\n\n"
            async for event, data in stream:
                yield format_sse(event, data) if event else ": ping\n\n"

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)

