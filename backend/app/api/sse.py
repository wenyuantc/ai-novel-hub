"""Server-Sent Events 输出。

事件格式：
    event: <name>
    data: <JSON>

data 一律 JSON 编码，避免正文中的换行破坏 SSE 帧。事件名为空串表示心跳，渲染为注释帧 ": ping"。
"""

import json
from collections.abc import AsyncIterator
from contextlib import aclosing

from fastapi.responses import StreamingResponse

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # 关闭 Nginx 等反代的响应缓冲，保证增量到达前端
    "X-Accel-Buffering": "no",
}


def format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_response(events: AsyncIterator[tuple[str, dict]]) -> StreamingResponse:
    async def body() -> AsyncIterator[str]:
        # aclosing：响应被中断（客户端断开）时确定性地关闭业务生成器，
        # 让其 finally 立即执行（例如回退章节状态），而不是等到垃圾回收
        async with aclosing(events) as stream:
            # 先发一条注释帧，促使代理/浏览器尽早建立流
            yield ": stream-start\n\n"
            async for event, data in stream:
                yield format_sse(event, data) if event else ": ping\n\n"

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)
