"""一次模型尝试的计量观察，不持有凭据、请求或响应内容。"""

import asyncio
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)

AttemptStatus = Literal["completed", "failed", "cancelled"]
MAX_USAGE_COUNT = 2**63 - 1


@dataclass(frozen=True, slots=True)
class ModelUsage:
    attempt_id: str
    started_at: datetime
    status: AttemptStatus
    sent: bool | None
    input_tokens: int | None
    output_tokens: int | None
    image_count: int | None
    dispatched_at: datetime | None = None
    cached_input_tokens: int | None = None


class ModelUsageObserver(Protocol):
    async def begin(self, attempt_id: str, started_at: datetime) -> None: ...

    async def finish(self, snapshot: ModelUsage) -> None:
        """收集结果。无响应头的失败可能仍持有发送租约，持久化须由外层收尾。"""
        ...


_observer: ContextVar[ModelUsageObserver | None] = ContextVar("model_usage_observer", default=None)


@contextmanager
def observe_model_usage(observer: ModelUsageObserver) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def has_model_usage_observer() -> bool:
    return _observer.get() is not None


def may_request_usage_options() -> bool:
    observer = _observer.get()
    return observer is not None and getattr(observer, "request_usage_options", True)


def valid_usage_count(value: object) -> int | None:
    if type(value) is int and 0 <= value <= MAX_USAGE_COUNT:
        return value
    return None


class ModelAttempt:
    """本地统计只在 SDK 调用期间绑定到 HTTP 钩子，不能跨出站 SSE 的 yield。"""

    def __init__(self, observer: ModelUsageObserver | None):
        self._observer = observer
        self._attempt_id: str | None = None
        self._started_at: datetime | None = None
        self._has_http_request = False
        self._http_retry_count: int | None = None
        self._redirect_target: int | None = None
        self.sent: bool | None = None
        self.dispatched_at: datetime | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.image_count: int | None = None
        self._cached_input_tokens: int | None = None

    async def begin(self) -> None:
        if self._observer is None:
            return
        self._attempt_id, self._started_at = str(uuid4()), datetime.now(UTC)
        self.sent, self.dispatched_at = None, None
        self.input_tokens, self.output_tokens, self.image_count = None, None, None
        self._cached_input_tokens = None
        try:
            await self._observer.begin(self._attempt_id, self._started_at)
        except asyncio.CancelledError:
            self.mark_not_sent()
            raise
        except Exception:
            logger.warning("模型用量尝试记录未建立")

    async def finish(self, status: AttemptStatus) -> None:
        if self._observer is None or self._attempt_id is None or self._started_at is None:
            return
        # 总输入量和命中量可能分两帧到达，合并后再校验子集关系。
        cached = self._cached_input_tokens
        if cached is not None and (self.input_tokens is None or cached > self.input_tokens):
            cached = None
        snapshot = ModelUsage(
            self._attempt_id, self._started_at, status, self.sent,
            self.input_tokens, self.output_tokens, self.image_count,
            self.dispatched_at or (self._started_at if self.sent else None),
            cached_input_tokens=cached,
        )
        self._attempt_id = None
        try:
            await self._observer.finish(snapshot)
        except Exception:
            logger.warning("模型用量尝试结果未收集")

    async def observe_http_request(self, request_key: int, retry_count: int | None) -> None:
        """仅在不持授权租约的 SDK 回调中拆分内部重试。"""
        if self._has_http_request:
            if self._http_retry_count is not None and retry_count is not None:
                retry = retry_count > self._http_retry_count
            else:
                retry = request_key != self._redirect_target
            if retry:
                await self.finish("failed")
                await self.begin()
        self._has_http_request, self._http_retry_count = True, retry_count
        self._redirect_target = None
        self.mark_send_allowed()

    def observe_http_response(self, redirect_target: int | None) -> None:
        self._redirect_target = redirect_target
        self.mark_response()

    def set_usage(
        self, *, input_tokens: object = None, output_tokens: object = None, image_count: object = None,
        cached_input_tokens: object = None,
    ) -> None:
        if (count := valid_usage_count(input_tokens)) is not None:
            self.input_tokens = count
        if (count := valid_usage_count(output_tokens)) is not None:
            self.output_tokens = count
        if (count := valid_usage_count(image_count)) is not None:
            self.image_count = count
        if (count := valid_usage_count(cached_input_tokens)) is not None:
            self._cached_input_tokens = count

    def mark_response(self) -> None:
        self.sent = True

    def mark_send_allowed(self) -> None:
        if self.dispatched_at is None:
            self.dispatched_at = datetime.now(UTC)

    def mark_not_sent(self) -> None:
        if self.sent is None and self.dispatched_at is None:
            self.sent = False

    async def invoke[T](self, operation: Awaitable[T]) -> T:
        if self._observer is None:
            return await operation
        token = _active_attempt.set(self)
        try:
            return await operation
        finally:
            _active_attempt.reset(token)

    async def iterate[T](self, items: AsyncIterable[T]) -> AsyncIterator[T]:
        iterator = aiter(items)
        while True:
            try:
                item = await self.invoke(anext(iterator))
            except StopAsyncIteration:
                return
            self.mark_response()
            yield item


_active_attempt: ContextVar[ModelAttempt | None] = ContextVar("model_usage_attempt", default=None)


async def observe_model_http_request(request_key: int, retry_count: int | None) -> None:
    if attempt := _active_attempt.get():
        await attempt.observe_http_request(request_key, retry_count)


def observe_model_http_response(redirect_target: int | None) -> None:
    if attempt := _active_attempt.get():
        attempt.observe_http_response(redirect_target)


def mark_model_response() -> None:
    if attempt := _active_attempt.get():
        attempt.mark_response()


def mark_model_send_allowed() -> None:
    if attempt := _active_attempt.get():
        attempt.mark_send_allowed()


def mark_model_send_blocked() -> None:
    if attempt := _active_attempt.get():
        attempt.mark_not_sent()


@asynccontextmanager
async def model_attempt() -> AsyncIterator[ModelAttempt]:
    attempt = ModelAttempt(_observer.get())
    status: AttemptStatus = "completed"
    try:
        await attempt.begin()
        yield attempt
    except (asyncio.CancelledError, GeneratorExit):
        status = "cancelled"
        raise
    except BaseException:
        status = "failed"
        raise
    finally:
        await attempt.finish(status)
