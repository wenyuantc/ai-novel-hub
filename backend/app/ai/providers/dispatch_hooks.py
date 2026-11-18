"""绑定单次 Provider 操作的 HTTP 钩子，保持原 SDK 客户端及重定向策略。"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, contextmanager
from contextvars import ContextVar

import httpx

from app.ai.providers.base import RequestPolicyError
from app.ai.providers.usage import (
    has_model_usage_observer,
    mark_model_response,
    mark_model_send_allowed,
    mark_model_send_blocked,
    observe_model_http_request,
    observe_model_http_response,
)


class DispatchHooks:
    def __init__(self, acquire: Callable[[], AbstractAsyncContextManager[None]]):
        self._acquire = acquire
        self._leases: dict[int, AsyncExitStack] = {}
        self._owned_clients: list[httpx.AsyncClient] = []
        self._after_close: list[Callable[[], Awaitable[None]]] = []

    @property
    def event_hooks(self) -> dict[str, list]:
        return {"request": [self._before_request], "response": [self._after_headers]}

    async def _before_request(self, request: httpx.Request) -> None:
        # HTTPX 在每一跳实际 send 前调用（不是每个业务方法只调用一次）。
        key = id(request)
        if key in self._leases:
            raise RequestPolicyError("受控模型请求未完成，请重试")
        lease = AsyncExitStack()
        self._leases[key] = lease
        try:
            await lease.enter_async_context(self._acquire())
        except BaseException:
            self._leases.pop(key, None)
            mark_model_send_blocked()
            raise
        mark_model_send_allowed()

    async def _after_headers(self, response: httpx.Response) -> None:
        # 仅收到响应头即可证明发送窗口已关闭；绝不能锁到响应体或 SSE 完成。
        mark_model_response()
        if lease := self._leases.pop(id(response.request), None):
            await lease.aclose()

    def own_client(self, client: httpx.AsyncClient) -> httpx.AsyncClient:
        # Gemini 不接管调用方传入的 HTTPX 客户端，需由操作作用域释放。
        self._owned_clients.append(client)
        return client

    def after_close(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._after_close.append(callback)

    async def aclose(self) -> None:
        # 传输异常/取消不一定有响应头。先关闭传输，再兜底释放仍持有的事务。
        async with AsyncExitStack() as stack:
            for callback in self._after_close:
                stack.push_async_callback(callback)
            for lease in self._leases.values():
                stack.push_async_callback(lease.aclose)
            for client in self._owned_clients:
                stack.push_async_callback(client.aclose)
            self._leases.clear()
            self._owned_clients.clear()
            self._after_close.clear()


_active_hooks: ContextVar[DispatchHooks | None] = ContextVar("model_dispatch_hooks", default=None)


def current_dispatch_hooks() -> DispatchHooks | None:
    return _active_hooks.get()


async def _observe_response(response: httpx.Response) -> None:
    target = None
    if response.has_redirect_location:
        try:
            target = hash(str(response.request.url.join(response.headers["location"])))
        except httpx.InvalidURL:
            pass
    observe_model_http_response(target)


async def _observe_request(request: httpx.Request) -> None:
    # OpenAI 与 Anthropic SDK 标注内部重试次数。无该字段时用 HTTPX 重定向目标排除同次请求的跳转。
    retry_header = request.headers.get("x-stainless-retry-count", "")
    retry_count = None
    if len(retry_header) < 6 and retry_header.isascii() and retry_header.isdecimal():
        retry_count = int(retry_header)
    await observe_model_http_request(hash(str(request.url)), retry_count)


def model_client_event_hooks() -> dict[str, list] | None:
    if hooks := current_dispatch_hooks():
        return hooks.event_hooks
    if has_model_usage_observer():
        return {"request": [_observe_request], "response": [_observe_response]}
    return None


@contextmanager
def use_dispatch_hooks(hooks: DispatchHooks) -> Iterator[None]:
    token = _active_hooks.set(hooks)
    try:
        yield
    finally:
        _active_hooks.reset(token)
