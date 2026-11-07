"""保护已组装的设定负载；策略保存与实际请求发出共享事务门禁。"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.ai.providers.base import (
    ChatMessage,
    GeneratedImage,
    LLMProvider,
    ModelInfo,
    RequestPolicyError,
    ThinkingLevel,
)
from app.ai.providers.dispatch_hooks import DispatchHooks, use_dispatch_hooks
from app.ai.tools import AgentEvent, AgentMessage, ToolChoice, ToolSpec
from app.core.errors import ConflictError, NotFoundError
from app.models import Project
from app.services.project_lifecycle import active_conditions

# 两个有符号 int32 的 PostgreSQL advisory lock 命名空间，与其它资源锁隔离。
_LOCK_NAMESPACE = 0x41495653
_LOCK_WAIT_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.02


class VisibilityPolicyError(ConflictError, RequestPolicyError):
    """受控载荷不能发送，且不能降级为同一份载荷的普通网络重试。"""


class VisibilityChangedError(VisibilityPolicyError):
    def __init__(self) -> None:
        super().__init__(
            "AI 可见设置已变更，已停止发送，请按当前设置重试",
            extra={"code": "ai_visibility_changed"},
        )


class VisibilityBusyError(VisibilityPolicyError):
    def __init__(self) -> None:
        super().__init__(
            "AI 请求正在发送，本次操作未完成，请稍后重试",
            extra={"code": "ai_visibility_busy"},
        )


def is_sqlite_busy(exc: OperationalError) -> bool:
    code = getattr(exc.orig, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


@dataclass(frozen=True, slots=True)
class VisibilityStamp:
    project_id: int
    revision: int
    epoch: int
    # 软删除再恢复不复用旧发送代次。
    last_deleted_at: datetime | None = None


async def read_visibility_stamp(db: AsyncSession, project_id: int) -> VisibilityStamp:
    """从 SQL 标量读取，避免 ORM identity map 命中旧版本。调用方需先校验归属。"""
    with db.no_autoflush:
        row = (
            await db.execute(
                select(Project.ai_visibility_revision, Project.ai_history_epoch, Project.last_deleted_at)
                .where(Project.id == project_id, *active_conditions())
            )
        ).one_or_none()
    if row is None:
        raise NotFoundError("作品不存在")
    return VisibilityStamp(project_id, row.ai_visibility_revision, row.ai_history_epoch, row.last_deleted_at)


async def require_visibility_stamp(db: AsyncSession, expected: VisibilityStamp) -> None:
    try:
        actual = await read_visibility_stamp(db, expected.project_id)
    except NotFoundError:
        raise VisibilityChangedError from None
    if actual != expected:
        raise VisibilityChangedError


def visibility_sessionmaker(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """只共享引擎，不共享业务事务；不得持有写事务跨模型调用。"""
    bind = db.bind
    if isinstance(bind, AsyncConnection):
        bind = bind.engine
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("受控 AI 请求缺少数据库引擎")
    return async_sessionmaker(bind, expire_on_commit=False)


async def _lock_postgres(db: AsyncSession, project_id: int, *, write: bool) -> None:
    function = func.pg_try_advisory_xact_lock if write else func.pg_try_advisory_xact_lock_shared
    statement = select(function(_LOCK_NAMESPACE, project_id))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LOCK_WAIT_SECONDS
    while not await db.scalar(statement):
        if (remaining := deadline - loop.time()) <= 0:
            raise VisibilityBusyError
        await asyncio.sleep(min(remaining, _LOCK_POLL_SECONDS))


async def _lock_sqlite(db: AsyncSession, project_id: int, *, include_recycled: bool = False) -> None:
    # SELECT / with_for_update 在 SQLite 不持有物理写锁。no-op UPDATE 使独立连接
    # 与策略保存事务互斥，且不触碰更新时间、业务列或 ORM 中待写的值。
    statement = (
        update(Project)
        .where(Project.id == project_id, *(() if include_recycled else active_conditions()))
        .values(ai_visibility_revision=Project.ai_visibility_revision, updated_at=Project.updated_at)
        .execution_options(synchronize_session=False)
    )
    timeout = int(await db.scalar(text("PRAGMA busy_timeout")) or 0)
    try:
        await db.execute(text(f"PRAGMA busy_timeout = {int(_LOCK_WAIT_SECONDS * 1000)}"))
        result = await db.execute(statement)
    except OperationalError as exc:
        if is_sqlite_busy(exc):
            raise VisibilityBusyError from None
        raise
    finally:
        await db.execute(text(f"PRAGMA busy_timeout = {timeout}"))
    if result.rowcount != 1:
        raise NotFoundError("作品不存在")


async def _lock(db: AsyncSession, project_id: int, *, write: bool, include_recycled: bool = False) -> None:
    with db.no_autoflush:
        match db.get_bind().dialect.name:
            case "postgresql":
                await _lock_postgres(db, project_id, write=write)
            case "sqlite":
                await _lock_sqlite(db, project_id, include_recycled=include_recycled)
            case _:
                raise RuntimeError("受控 AI 请求不支持当前数据库")


async def lock_visibility_write(
    db: AsyncSession, project_id: int, *, include_recycled: bool = False
) -> None:
    """策略作者写入口：与策略 CAS 及 commit 同一事务；不要在服务内提前释放。"""
    await _lock(db, project_id, write=True, include_recycled=include_recycled)


class _GuardedProvider(LLMProvider):
    def __init__(
        self, provider: LLMProvider, stamp: VisibilityStamp, sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__(provider.api_key, provider.base_url, provider.timeout)
        self.follow_redirects = provider.follow_redirects
        self._provider = provider
        self._stamp = stamp
        self._sessionmaker = sessionmaker

    @asynccontextmanager
    async def _send_lease(self) -> AsyncIterator[None]:
        async with self._sessionmaker() as db:
            try:
                await _lock(db, self._stamp.project_id, write=False)
            except NotFoundError:
                raise VisibilityChangedError from None
            await require_visibility_stamp(db, self._stamp)
            yield

    @asynccontextmanager
    async def _hooks(self) -> AsyncIterator[DispatchHooks]:
        if not self._provider.uses_dispatch_hooks:
            # 注入的 FakeLLM 等不发 HTTP，仍要在调用时检查，确保测试捕获的
            # messages 与真实模型受同一代次限制；不把事务锁留到替身响应流。
            async with self._sessionmaker() as db:
                await require_visibility_stamp(db, self._stamp)
        hooks = DispatchHooks(self._send_lease)
        try:
            yield hooks
        finally:
            await hooks.aclose()

    async def _stream[T](self, start: Callable[[], AsyncIterator[T]]) -> AsyncIterator[T]:
        async with self._hooks() as hooks:
            with use_dispatch_hooks(hooks):
                stream = start()
            async with aclosing(stream):
                while True:
                    # 不能把 ContextVar 留给 yield 的调用方，也不能跨任务 reset token。
                    with use_dispatch_hooks(hooks):
                        try:
                            item = await anext(stream)
                        except StopAsyncIteration:
                            return
                    yield item

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._hooks() as hooks:
            with use_dispatch_hooks(hooks):
                return await operation()

    def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        return self._stream(
            lambda: self._provider.generate_stream(
                messages, model, temperature=temperature, max_tokens=max_tokens, thinking=thinking,
            )
        )

    def chat_tools_stream(
        self,
        messages: list[AgentMessage],
        model: str,
        tools: list[ToolSpec],
        *,
        tool_choice: ToolChoice = "auto",
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
        temperature: float | None = None,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(
            lambda: self._provider.chat_tools_stream(
                messages, model, tools, tool_choice=tool_choice, max_tokens=max_tokens,
                thinking=thinking, temperature=temperature,
            )
        )

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return await self._call(lambda: self._provider.embed(texts, model))

    async def generate_image(self, prompt: str, model: str) -> GeneratedImage:
        return await self._call(lambda: self._provider.generate_image(prompt, model))

    async def list_models(self) -> list[ModelInfo]:
        return await self._call(self._provider.list_models)


def guard_provider(
    provider: LLMProvider, stamp: VisibilityStamp, sessionmaker: async_sessionmaker[AsyncSession],
) -> LLMProvider:
    """把构建时的戳绑定到 provider；每次调用重验而非给旧消息续签。"""
    if isinstance(provider, _GuardedProvider):
        if provider._stamp != stamp:
            raise VisibilityChangedError
        return provider
    return _GuardedProvider(provider, stamp, sessionmaker)
