"""绑定单次 Provider 操作的 HTTP 钩子，保持原 SDK 客户端及重定向策略。"""

from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, contextmanager
from contextvars import ContextVar

import httpx

from app.ai.providers.base import RequestPolicyError


class DispatchHooks:
    def __init__(self, acquire: Callable[[], AbstractAsyncContextManager[None]]):
        self._acquire = acquire
        self._leases: dict[int, AsyncExitStack] = {}
        self._owned_clients: list[httpx.AsyncClient] = []

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
            raise

    async def _after_headers(self, response: httpx.Response) -> None:
        # 仅收到响应头即可证明发送窗口已关闭；绝不能锁到响应体或 SSE 完成。
        if lease := self._leases.pop(id(response.request), None):
            await lease.aclose()

    def own_client(self, client: httpx.AsyncClient) -> httpx.AsyncClient:
        # Gemini 不接管调用方传入的 HTTPX 客户端，需由操作作用域释放。
        self._owned_clients.append(client)
        return client

    async def aclose(self) -> None:
        # 传输异常/取消不一定有响应头。先关闭传输，再兜底释放仍持有的事务。
        async with AsyncExitStack() as stack:
            for lease in self._leases.values():
                stack.push_async_callback(lease.aclose)
            for client in self._owned_clients:
                stack.push_async_callback(client.aclose)
            self._leases.clear()
            self._owned_clients.clear()


_active_hooks: ContextVar[DispatchHooks | None] = ContextVar("model_dispatch_hooks", default=None)


def current_dispatch_hooks() -> DispatchHooks | None:
    return _active_hooks.get()


@contextmanager
def use_dispatch_hooks(hooks: DispatchHooks) -> Iterator[None]:
    token = _active_hooks.set(hooks)
    try:
        yield
    finally:
        _active_hooks.reset(token)
