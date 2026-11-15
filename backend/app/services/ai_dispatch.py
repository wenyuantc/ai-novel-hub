"""保护已组装的设定负载；策略保存与实际请求发出共享事务门禁。"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from weakref import WeakKeyDictionary

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


class SendLeaseError(VisibilityPolicyError):
    """发送事务的认证、连接或物理门禁不再是同一个。"""

    def __init__(self) -> None:
        super().__init__("受控发送状态已变化，已停止发送", extra={"code": "send_lease_changed"})


_SEND_MODE = object()
_PREFLIGHT_MODE = object()


@dataclass(frozen=True, slots=True, repr=False)
class _RequestHookContext:
    owner: asyncio.Task
    guard: object
    request: object


_REQUEST_HOOK: ContextVar[_RequestHookContext | None] = ContextVar("chapter_send_request_hook", default=None)


@dataclass(slots=True)
class _SendLease:
    owner: asyncio.Task
    project_id: int
    provider: LLMProvider
    mode: object
    request_context: _RequestHookContext | None
    actor_id: int | None = None
    transaction: object | None = None
    connection: object | None = None
    connection_transaction: object | None = None
    driver_connection: object | None = None
    locked: bool = False
    valid: bool = True


# 不放进调用方可写的 db.info；仅 _send_lease 的私有会话和当前任务有资格使用。
_ACTIVE_SEND_LEASES: dict[AsyncSession, _SendLease] = {}


@dataclass(frozen=True, slots=True, repr=False)
class _TransportOrigin:
    factory: object
    provider_name: str
    provider_type: type[LLMProvider]


_CHAPTER_TRANSPORTS: WeakKeyDictionary[LLMProvider, _TransportOrigin] = WeakKeyDictionary()


def _chapter_factory_allowed(factory) -> bool:
    from app.ai.providers import create_provider

    return factory is create_provider


def _chapter_provider_allowed(factory, provider_name: str, provider: LLMProvider) -> bool:
    from app.ai.providers import AnthropicProvider, GeminiProvider, OpenAICompatProvider, create_provider

    classes = {"openai_compat": OpenAICompatProvider, "anthropic": AnthropicProvider, "gemini": GeminiProvider}
    return (factory is create_provider and type(provider) is classes.get(provider_name)
            and provider.uses_dispatch_hooks is True)


def chapter_provider_factory(factory):
    """先审核工厂，再由该工厂构造并登记可供章节逐跳门禁使用的 SDK 实例。"""
    if not _chapter_factory_allowed(factory):
        raise SendLeaseError()

    def create(provider_name: str, api_key: str, base_url: str | None, timeout: float) -> LLMProvider:
        provider = factory(provider_name, api_key, base_url, timeout)
        if not _chapter_provider_allowed(factory, provider_name, provider):
            raise SendLeaseError()
        _CHAPTER_TRANSPORTS[provider] = _TransportOrigin(factory, provider_name, type(provider))
        return provider

    return create


def _chapter_transport(provider: LLMProvider) -> tuple[LLMProvider, _TransportOrigin]:
    from app.services.retrieval_service import _GuardedEmbeddingProvider

    if type(provider) in (_GuardedProvider, _GuardedEmbeddingProvider):
        provider = provider._provider
    # 模型绑定包装仅转发给已检查的标准 SDK；不能信任任意自定义包装。
    from app.services.collaboration_ai_guard import _BoundModelProvider

    if type(provider) is _BoundModelProvider:
        if provider.uses_dispatch_hooks is not True:
            raise SendLeaseError()
        provider = provider._provider
    try:
        origin = _CHAPTER_TRANSPORTS.get(provider)
        valid = origin is not None and type(provider) is origin.provider_type and provider.uses_dispatch_hooks is True
    except (TypeError, AttributeError):
        valid = False
    if not valid:
        raise SendLeaseError()
    return provider, origin


def require_chapter_transport(provider: LLMProvider) -> None:
    """在开始调用前检查来源；自报 hooks 的第三方实例不能获得章节发送资格。"""
    _chapter_transport(provider)


def _invalidate_send_lease(db: AsyncSession) -> None:
    if state := _ACTIVE_SEND_LEASES.get(db):
        state.valid = False


async def _same_send_transaction(db: AsyncSession, state: _SendLease) -> bool:
    try:
        if state.mode is _SEND_MODE and (state.request_context is None
                                         or _REQUEST_HOOK.get() is not state.request_context):
            return False
        transaction = db.get_transaction()
        if (not state.valid or state.owner is not asyncio.current_task() or transaction is None
                or not transaction.is_active or transaction.sync_transaction is not state.transaction
                or db.get_nested_transaction() is not None):
            return False
        connection = await db.connection()
        if connection.closed or connection.invalidated or connection.sync_connection is not state.connection:
            return False
        physical = connection.sync_connection
        return (physical.get_transaction() is state.connection_transaction
                and physical.get_nested_transaction() is None
                and physical.connection.dbapi_connection is state.driver_connection)
    except Exception:
        # 无法确认原连接和锁仍有效时，一律不能把这次调用认作受控发送。
        return False


async def authorize_send_actor(db: AsyncSession, actor) -> None:
    """认证并锁定发送 actor；仅真正的 _send_lease 可以登记当前事务的证明。"""
    from app.services import authorization_service

    state = _ACTIVE_SEND_LEASES.get(db)
    try:
        original = authorization_service.actor_snapshot(db, actor)
        actor_id = original if isinstance(original, int) else original.id
        if type(actor_id) is not int or not 0 < actor_id <= 2**31 - 1:
            raise SendLeaseError()
        if state is not None and (state.owner is not asyncio.current_task() or state.locked
                                  or not state.valid or (state.actor_id is not None and state.actor_id != actor_id)):
            raise SendLeaseError()
        await authorization_service.require_authenticated_for_send(db, original)
        if state is None:
            return
        transaction = db.get_transaction()
        if transaction is None or not transaction.is_active or db.get_nested_transaction() is not None:
            raise SendLeaseError()
        connection = await db.connection()
        physical = connection.sync_connection
        if connection.closed or connection.invalidated or physical.get_transaction() is None:
            raise SendLeaseError()
        if state.actor_id is not None:
            if not await _same_send_transaction(db, state):
                raise SendLeaseError()
        else:
            state.actor_id = actor_id
            state.transaction = transaction.sync_transaction
            state.connection = physical
            state.connection_transaction = physical.get_transaction()
            state.driver_connection = physical.connection.dbapi_connection
    except BaseException:
        _invalidate_send_lease(db)
        raise


async def _require_send_lease(db: AsyncSession, actor_id: int, project_id: int) -> None:
    state = _ACTIVE_SEND_LEASES.get(db)
    if (state is None or not state.locked or state.actor_id != actor_id or state.project_id != project_id
            or not await _same_send_transaction(db, state)):
        _invalidate_send_lease(db)
        raise SendLeaseError()
    try:
        require_chapter_transport(state.provider)
    except BaseException:
        _invalidate_send_lease(db)
        raise


async def _require_mode_lease(db: AsyncSession, actor_id: int, project_id: int, mode: object) -> None:
    state = _ACTIVE_SEND_LEASES.get(db)
    if (state is None or state.mode is not mode or not state.locked
            or type(actor_id) is not int or type(project_id) is not int
            or state.actor_id != actor_id or state.project_id != project_id
            or not await _same_send_transaction(db, state)):
        _invalidate_send_lease(db)
        raise SendLeaseError()


async def require_preflight_lease(db: AsyncSession, actor_id: int, project_id: int) -> None:
    """只接受同一物理预检事务；真实发送不能用免一次性动作的回调冒充。"""
    await _require_mode_lease(db, actor_id, project_id, _PREFLIGHT_MODE)


async def require_actual_send_lease(db: AsyncSession, actor_id: int, project_id: int) -> None:
    """一次性发送动作只能在实际 HTTP 请求的物理门禁中消费。"""
    await _require_mode_lease(db, actor_id, project_id, _SEND_MODE)


async def _wait_send_cleanup(action: Awaitable[None]) -> None:
    cleanup = asyncio.create_task(action)
    cancelled = None
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError as exc:
            if cleanup.cancelled():
                raise
            cancelled = exc
    if cancelled is not None:
        raise cancelled


async def _close_send_session(db: AsyncSession) -> None:
    """重复取消也要等事务释放，避免把锁遗留给后台清理任务。"""
    await _wait_send_cleanup(db.close())


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
    # Agent 的 PostgreSQL REPEATABLE READ 会话绑定的是带隔离选项的引擎代理；发送校验会话
    # 须先加锁再读最新提交，使用其记录的原引擎（同一连接池、默认隔离级别）。
    base = db.info.get("visibility_bind")
    if base is not None:
        if not isinstance(base, AsyncEngine) or not isinstance(bind, AsyncEngine) or base.pool is not bind.pool:
            raise RuntimeError("受控 AI 请求缺少数据库引擎")
        bind = base
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("受控 AI 请求缺少数据库引擎")
    # 单次授权发送可能先取认证写门禁；仅复用协调器，不跨会话共享 actor 或事务状态。
    runtime = db.info.get("authorization_runtime")
    info = {"authorization_runtime": runtime} if runtime is not None else {}
    return async_sessionmaker(bind, expire_on_commit=False, info=info)


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
    restore = text(f"PRAGMA busy_timeout = {timeout}")
    try:
        await db.execute(text(f"PRAGMA busy_timeout = {int(_LOCK_WAIT_SECONDS * 1000)}"))
        result = await db.execute(statement)
    except BaseException as exc:
        try:
            await db.execute(restore)
        except Exception:
            # 取消打断语句时 _release_cancelled_sqlite 已作废该连接（不回连接池），恢复必然 PendingRollbackError；
            # 不能让它掩盖取消（否则撤权/用户取消被记成「服务器内部错误」）。其余原异常下的恢复失败照旧上抛。
            if not isinstance(exc, asyncio.CancelledError):
                raise
        if isinstance(exc, OperationalError) and is_sqlite_busy(exc):
            raise VisibilityBusyError from None
        raise
    await db.execute(restore)
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


@dataclass(frozen=True, slots=True, repr=False)
class _ChapterProviderSnapshot:
    model: str
    model_type: str
    api_key: str
    base_url: str | None
    timeout: float
    follow_redirects: bool


def _chapter_provider_snapshot(provider: LLMProvider, transport: LLMProvider) -> _ChapterProviderSnapshot:
    from app.services.collaboration_ai_guard import _BoundModelProvider

    if (type(provider) is not _BoundModelProvider or provider._provider is not transport
            or type(provider._model) is not str or not provider._model
            or provider._model_type not in ("text", "embedding", "image")
            or provider.api_key != transport.api_key or provider.base_url != transport.base_url
            or provider.timeout != transport.timeout or provider.follow_redirects != transport.follow_redirects
            or provider.uses_dispatch_hooks is not True or transport.uses_dispatch_hooks is not True):
        raise SendLeaseError()
    return _ChapterProviderSnapshot(provider._model, provider._model_type, transport.api_key, transport.base_url,
                                    transport.timeout, transport.follow_redirects)


@dataclass(frozen=True, slots=True, repr=False)
class _ChapterGuardBinding:
    chapter: object
    provider: LLMProvider
    transport: LLMProvider
    origin: _TransportOrigin
    snapshot: _ChapterProviderSnapshot
    stamp: VisibilityStamp
    sessionmaker: async_sessionmaker[AsyncSession]
    bind: object
    runtime: object
    authorize: object
    verify: object
    verify_preflight: object


_CHAPTER_GUARDS: WeakKeyDictionary[LLMProvider, _ChapterGuardBinding] = WeakKeyDictionary()


def _bind_chapter_guard(guarded: "_GuardedProvider", chapter) -> None:
    """仅对新建的内部包装登记原身份；重复绑定不能改签已有章节。"""
    from app.services import chapter_claim
    from app.services.retrieval_service import _GuardedEmbeddingProvider

    if (type(guarded) not in (_GuardedProvider, _GuardedEmbeddingProvider) or guarded in _CHAPTER_GUARDS
            or guarded._chapter is not chapter or not chapter_claim._valid_identity(chapter)
            or chapter.phase != "running" or chapter.project_id != guarded._stamp.project_id):
        raise SendLeaseError()
    transport, origin = _chapter_transport(guarded._provider)
    snapshot = _chapter_provider_snapshot(guarded._provider, transport)
    maker = guarded._sessionmaker
    _CHAPTER_GUARDS[guarded] = _ChapterGuardBinding(
        chapter, guarded._provider, transport, origin, snapshot, guarded._stamp, maker, maker.kw.get("bind"),
        maker.kw.get("info", {}).get("authorization_runtime"), guarded._authorize, guarded._verify,
        guarded._verify_preflight,
    )


def _bound_chapter(guarded: "_GuardedProvider"):
    binding = _CHAPTER_GUARDS.get(guarded)
    if binding is None:
        if guarded._chapter is not None:
            raise SendLeaseError()
        return None
    maker = guarded._sessionmaker
    if (guarded._chapter is not binding.chapter or guarded._provider is not binding.provider
            or guarded._stamp != binding.stamp or maker is not binding.sessionmaker
            or maker.kw.get("bind") is not binding.bind
            or maker.kw.get("info", {}).get("authorization_runtime") is not binding.runtime
            or guarded._authorize is not binding.authorize or guarded._verify is not binding.verify
            or guarded._verify_preflight is not binding.verify_preflight):
        raise SendLeaseError()
    transport, origin = _chapter_transport(binding.provider)
    if (transport is not binding.transport or origin is not binding.origin
            or _chapter_provider_snapshot(binding.provider, transport) != binding.snapshot):
        raise SendLeaseError()
    return binding.chapter


class _PhysicalDispatchHooks(DispatchHooks):
    """把 send 证明绑定到当前 guard 的实际 request hook，而非直接调用租约。"""

    def __init__(self, guarded: "_GuardedProvider") -> None:
        super().__init__(guarded._send_lease)
        self._guarded = guarded

    async def _before_request(self, request) -> None:
        token = _REQUEST_HOOK.set(_RequestHookContext(asyncio.current_task(), self._guarded, request))
        try:
            await super()._before_request(request)
        finally:
            _REQUEST_HOOK.reset(token)


class _GuardedProvider(LLMProvider):
    def __init__(
        self, provider: LLMProvider, stamp: VisibilityStamp, sessionmaker: async_sessionmaker[AsyncSession],
        verify: Callable[[AsyncSession], Awaitable[None]] | None = None,
        authorize: Callable[[AsyncSession], Awaitable[None]] | None = None,
        *, chapter=None, verify_preflight: Callable[[AsyncSession], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(provider.api_key, provider.base_url, provider.timeout)
        self.follow_redirects = provider.follow_redirects
        self._provider = provider
        self._stamp = stamp
        self._sessionmaker = sessionmaker
        self._verify = verify
        self._authorize = authorize
        self._chapter = chapter
        self._verify_preflight = verify_preflight

    @asynccontextmanager
    async def _send_lease(self) -> AsyncIterator[None]:
        # HTTP hooks 永远走 send；调用方不能用参数把一次实际请求切换成预检。
        async with self._lease(_SEND_MODE):
            yield

    @asynccontextmanager
    async def _lease(self, mode: object) -> AsyncIterator[None]:
        if mode is not _SEND_MODE and mode is not _PREFLIGHT_MODE:
            raise SendLeaseError()
        request_context = _REQUEST_HOOK.get() if mode is _SEND_MODE else None
        if mode is _SEND_MODE and (request_context is None or request_context.owner is not asyncio.current_task()
                                   or request_context.guard is not self):
            raise SendLeaseError()
        db = self._sessionmaker()
        state = _SendLease(asyncio.current_task(), self._stamp.project_id, self._provider, mode, request_context)
        pending: BaseException | None = None
        try:
            failed = False
            try:
                chapter = _bound_chapter(self)
                if db in _ACTIVE_SEND_LEASES:
                    raise SendLeaseError()
                _ACTIVE_SEND_LEASES[db] = state
                if self._authorize is not None:
                    await self._authorize(db)
                if state.actor_id is not None and not await _same_send_transaction(db, state):
                    raise SendLeaseError()
                try:
                    await _lock(db, self._stamp.project_id, write=False)
                except NotFoundError:
                    raise VisibilityChangedError from None
                await require_visibility_stamp(db, self._stamp)
                state.locked = True
                if chapter is not None:
                    from app.services import chapter_claim

                    await chapter_claim.require_sending_chapter(db, chapter)
                verify = (self._verify_preflight
                          if mode is _PREFLIGHT_MODE and self._verify_preflight is not None else self._verify)
                if verify is not None:
                    await verify(db)
                if chapter is not None:
                    if _bound_chapter(self) is not chapter:
                        raise SendLeaseError()
                    await chapter_claim.require_sending_chapter(db, chapter)
                if not state.valid or (state.actor_id is not None and not await _same_send_transaction(db, state)):
                    raise SendLeaseError()
            except VisibilityPolicyError:
                raise
            except Exception:
                failed = True
            if failed:
                # 在 except 外创建错误，避免 SDK 或日志携带连接、SQL 和其他内部异常链。
                raise SendLeaseError()
            yield
        except BaseException as exc:
            pending = exc
        finally:
            state.valid = False
            if _ACTIVE_SEND_LEASES.get(db) is state:
                _ACTIVE_SEND_LEASES.pop(db)
        close_failed = False
        close_cancelled = None
        try:
            await _close_send_session(db)
        except asyncio.CancelledError as exc:
            close_cancelled = exc
        except Exception:
            close_failed = True
        if close_failed:
            try:
                # close 失败时尽力使连接失效；仍不声称数据库一定已释放锁。
                await _wait_send_cleanup(db.invalidate())
            except asyncio.CancelledError as exc:
                close_cancelled = exc
            except Exception:
                pass
        if isinstance(pending, asyncio.CancelledError):
            raise pending
        if close_cancelled is not None:
            raise close_cancelled
        if close_failed:
            raise SendLeaseError()
        if pending is not None:
            raise pending

    async def preflight(self) -> None:
        """仅供无需出站的缓存校验；离开后不保留凭据，也不证明未来请求。"""
        async with self._lease(_PREFLIGHT_MODE):
            pass

    def _operation_provider(self) -> LLMProvider:
        if _bound_chapter(self) is None:
            return self._provider
        binding = _CHAPTER_GUARDS[self]
        snapshot, origin = binding.snapshot, binding.origin
        failed = False
        try:
            # 不能把可被其他协程修改的原实例用于构造真实 request：每次操作仅持有此私有副本。
            provider = origin.factory(origin.provider_name, snapshot.api_key, snapshot.base_url, snapshot.timeout)
            if not _chapter_provider_allowed(origin.factory, origin.provider_name, provider):
                raise SendLeaseError()
            provider.follow_redirects = snapshot.follow_redirects
            from app.services.collaboration_ai_guard import _BoundModelProvider

            return _BoundModelProvider(provider, snapshot.model, snapshot.model_type)
        except VisibilityPolicyError:
            raise
        except Exception:
            failed = True
        if failed:
            raise SendLeaseError()
        raise SendLeaseError()

    @asynccontextmanager
    async def _hooks(self) -> AsyncIterator[DispatchHooks]:
        _bound_chapter(self)
        if not self._provider.uses_dispatch_hooks:
            # 注入的 FakeLLM 等不发 HTTP，仍要在调用时检查，确保测试捕获的
            # messages 与真实模型受同一代次限制；不把事务锁留到替身响应流。
            async with self._sessionmaker() as db:
                if self._authorize is not None:
                    await self._authorize(db)
                await require_visibility_stamp(db, self._stamp)
                if self._verify is not None:
                    await self._verify(db)
        hooks = _PhysicalDispatchHooks(self)
        try:
            yield hooks
        finally:
            await hooks.aclose()

    async def _stream[T](self, start: Callable[[LLMProvider], AsyncIterator[T]]) -> AsyncIterator[T]:
        async with self._hooks() as hooks:
            provider = self._operation_provider()
            with use_dispatch_hooks(hooks):
                stream = start(provider)
            async with aclosing(stream):
                while True:
                    # 不能把 ContextVar 留给 yield 的调用方，也不能跨任务 reset token。
                    with use_dispatch_hooks(hooks):
                        try:
                            item = await anext(stream)
                        except StopAsyncIteration:
                            return
                    yield item

    async def _call[T](self, operation: Callable[[LLMProvider], Awaitable[T]]) -> T:
        async with self._hooks() as hooks:
            provider = self._operation_provider()
            with use_dispatch_hooks(hooks):
                return await operation(provider)

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
            lambda provider: provider.generate_stream(
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
            lambda provider: provider.chat_tools_stream(
                messages, model, tools, tool_choice=tool_choice, max_tokens=max_tokens,
                thinking=thinking, temperature=temperature,
            )
        )

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return await self._call(lambda provider: provider.embed(texts, model))

    async def generate_image(self, prompt: str, model: str) -> GeneratedImage:
        return await self._call(lambda provider: provider.generate_image(prompt, model))

    async def list_models(self) -> list[ModelInfo]:
        return await self._call(lambda provider: provider.list_models())


def guard_provider(
    provider: LLMProvider, stamp: VisibilityStamp, sessionmaker: async_sessionmaker[AsyncSession],
    *, verify: Callable[[AsyncSession], Awaitable[None]] | None = None,
    authorize: Callable[[AsyncSession], Awaitable[None]] | None = None,
    verify_preflight: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> LLMProvider:
    """把构建时的戳绑定到 provider；每次调用重验而非给旧消息续签。"""
    if isinstance(provider, _GuardedProvider):
        if provider._stamp != stamp:
            raise VisibilityChangedError
        if verify is not None and provider._verify is not verify:
            raise VisibilityChangedError
        if authorize is not None and provider._authorize is not authorize:
            raise VisibilityChangedError
        if verify_preflight is not None and provider._verify_preflight is not verify_preflight:
            raise VisibilityChangedError
        return provider
    return _GuardedProvider(provider, stamp, sessionmaker, verify, authorize, verify_preflight=verify_preflight)


def guard_chapter_provider(
    provider: LLMProvider, stamp: VisibilityStamp, sessionmaker: async_sessionmaker[AsyncSession],
    *, chapter, verify: Callable[[AsyncSession], Awaitable[None]] | None = None,
    authorize: Callable[[AsyncSession], Awaitable[None]] | None = None,
    verify_preflight: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> LLMProvider:
    """固定原占用和工厂来源；每一跳在受控事务自动校验原章节。"""
    require_chapter_transport(provider)
    if isinstance(provider, _GuardedProvider):
        if _bound_chapter(provider) != chapter:
            raise SendLeaseError()
        return guard_provider(provider, stamp, sessionmaker, verify=verify, authorize=authorize,
                              verify_preflight=verify_preflight)
    guarded = _GuardedProvider(provider, stamp, sessionmaker, verify, authorize, chapter=chapter,
                               verify_preflight=verify_preflight)
    _bind_chapter_guard(guarded, chapter)
    return guarded
