"""把仅测试使用的响应工厂留在应用内，避免框架全局缓存延长其寿命。"""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from tests.helpers import wait_until


async def _stream_response_endpoint(request: Request) -> Response:
    factory = request.app.state.test_stream_responses[request.scope["route"].path]
    return await factory()


def mount_stream_response(app: FastAPI, path: str, factory: Callable[[], Awaitable[Response]]) -> None:
    """挂载无参数、无需依赖注入的测试响应；工厂可以捕获本例应用和持帧点。"""
    if not hasattr(app.state, "test_stream_responses"):
        app.state.test_stream_responses = {}
    app.state.test_stream_responses[path] = factory
    app.add_api_route(path, _stream_response_endpoint, methods=["GET"])


@asynccontextmanager
async def _tcp_server(app, entered, release):
    async def wrapped(scope, receive, send):
        async def delayed(message):
            if scope.get("path") == "/_test/task-frame" and b"event: snapshot" in message.get("body", b""):
                entered.set()
                await release.wait()
            await send(message)

        await app(scope, receive, delayed)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    server = uvicorn.Server(uvicorn.Config(wrapped, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started, within=10)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=10) as client:
            yield client
    finally:
        release.set()
        server.should_exit = True
        await asyncio.wait_for(runner, 10)
        sock.close()
