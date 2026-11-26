"""插件管理的 DB / HTTP 边界，沿用设置写入的锁顺序和原登录身份。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, ForbiddenError
from app.services import authorization_service as authz
from app.services.mcp import settings_gate
from app.services.system_settings_snapshot import SettingsUnavailable, effective_settings


async def require_enabled(db: AsyncSession) -> None:
    if not (await effective_settings(db)).mcp_enabled:
        raise ForbiddenError("管理员已关闭 MCP")


async def require_write(db: AsyncSession, user_id: int) -> None:
    # 设置保存也先取授权门。插件写入沿用此事务直到 commit。
    await authz.require_authenticated(db, user_id, write=True)
    await require_enabled(db)


class ManagementRequestGate:
    """每个刷新或试用操作单独使用，支持 SDK 并行的控制流和业务请求。"""

    def __init__(self, maker: async_sessionmaker[AsyncSession], actor: authz.AuthenticatedActor, *,
                 sse: bool, local_check=None):
        self.maker = maker
        self.actor = actor
        self.sse = sse
        self.local_check = local_check
        self._leases: dict[httpx.Request, AsyncExitStack] = {}
        self._failure = asyncio.get_running_loop().create_future()

    def _report_denial(self, request: httpx.Request, error: AppError) -> None:
        # 关闭会话的 DELETE 和可选的 Streamable HTTP GET 不应吞掉已经收到的业务结果。
        if (request.method == "POST" or (self.sse and request.method == "GET")) and not self._failure.done():
            self._failure.set_result(error)

    async def check(self, request: httpx.Request | None = None) -> None:
        try:
            async with self.maker() as db:
                await require_enabled(db)
        except AppError as exc:
            if request is not None:
                self._report_denial(request, exc)
            raise

    @asynccontextmanager
    async def lease(self, body: dict | None = None):
        async with self.maker() as db:
            # PG 持有用户与设置的共享锁，SQLite 的 User 写屏障与设置写入互斥。
            try:
                await authz.require_authenticated_for_send(db, self.actor)
                await settings_gate.lock_for_reload(db)
                await require_enabled(db)
                if self.local_check is not None:
                    await self.local_check(db, body)
            except SQLAlchemyError:
                raise SettingsUnavailable() from None
            yield

    def stdio_start(self):
        return self.lease()

    def stdio_write(self, body: dict):
        return self.lease(body)

    async def before_request(self, request: httpx.Request) -> None:
        lease = AsyncExitStack()
        try:
            await lease.enter_async_context(self.lease())
        except AppError as exc:
            self._report_denial(request, exc)
            raise
        self._leases[request] = lease
        method = None
        if request.method == "POST":
            try:
                body = json.loads(request.content)
                method = body.get("method") if isinstance(body, dict) else None
            except (ValueError, httpx.RequestNotRead):
                pass
        handshake = method == "initialize" or (isinstance(method, str) and method.startswith("notifications/"))
        if request.method == "POST" and not handshake:
            # 工具请求和无法识别的 POST 都持锁到响应头，正文读取可以继续。
            return
        previous = request.extensions.get("trace")

        async def trace(event, info):
            if previous is not None:
                await previous(event, info)
            # HTTPX 的 trace 在开始接收响应头前提供实际 HTTP 请求。HTTPS 代理
            # 的 CONNECT 还未发送 MCP 消息，必须等后续原请求进入同一阶段。
            if (event.endswith(".receive_response_headers.started")
                    and getattr(info.get("request"), "method", None) == request.method.encode("ascii")):
                await self.release(request)

        request.extensions["trace"] = trace

    async def release(self, request: httpx.Request) -> None:
        if lease := self._leases.pop(request, None):
            with anyio.CancelScope(shield=True):
                await lease.aclose()

    async def after_headers(self, response: httpx.Response) -> None:
        await self.release(response.request)

    async def aclose(self) -> None:
        for request in tuple(self._leases):
            await self.release(request)

    async def run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        # SDK 写入协程可能把策略异常包装成协议错误或仍在等响应，直接将本地拒绝交还 API。
        work = asyncio.ensure_future(operation())
        try:
            done, _ = await asyncio.wait((work, self._failure), return_when=asyncio.FIRST_COMPLETED)
            if self._failure in done:
                raise self._failure.result()
            return work.result()
        finally:
            if not work.done():
                work.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(work, return_exceptions=True)
                await self.aclose()


class ManagementHttpClient(httpx.AsyncClient):
    def __init__(self, gate: ManagementRequestGate, **kwargs):
        super().__init__(**kwargs)
        self._settings_gate = gate

    async def send(self, request, **kwargs):
        try:
            return await super().send(request, **kwargs)
        finally:
            # DNS、连接、取消等无响应头的失败，也立即结束这个请求的数据库事务。
            await self._settings_gate.release(request)
