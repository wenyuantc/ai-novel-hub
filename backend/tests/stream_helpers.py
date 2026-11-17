"""把仅测试使用的响应工厂留在应用内，避免框架全局缓存延长其寿命。"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response


async def _stream_response_endpoint(request: Request) -> Response:
    factory = request.app.state.test_stream_responses[request.scope["route"].path]
    return await factory()


def mount_stream_response(app: FastAPI, path: str, factory: Callable[[], Awaitable[Response]]) -> None:
    """挂载无参数、无需依赖注入的测试响应；工厂可以捕获本例应用和持帧点。"""
    if not hasattr(app.state, "test_stream_responses"):
        app.state.test_stream_responses = {}
    app.state.test_stream_responses[path] = factory
    app.add_api_route(path, _stream_response_endpoint, methods=["GET"])
