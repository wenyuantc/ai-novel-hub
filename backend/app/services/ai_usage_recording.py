"""把服务端调用身份绑定到 Provider 观测，仅在租约之外写入用量。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers.base import ChatMessage, GeneratedImage, LLMProvider, ModelInfo, ThinkingLevel
from app.ai.providers.dispatch_hooks import current_dispatch_hooks
from app.ai.providers.usage import ModelUsage, model_attempt, observe_model_usage
from app.ai.tools import AgentEvent, AgentMessage, ToolChoice, ToolSpec
from app.services import ai_usage_storage as storage
from app.services.periodic_database import retry_contended_write

logger = logging.getLogger(__name__)
_PENDING_MAX_AGE = timedelta(days=1)
_RECOVERY_INTERVAL = timedelta(hours=1)


class ModelConfig(storage.PriceConfig, Protocol):
    @property
    def user_id(self) -> int: ...


@dataclass(frozen=True, slots=True)
class UsageBinding:
    identity: storage.UsageCallIdentity
    sessionmaker: async_sessionmaker[AsyncSession]


class UsageRecorder:
    """一次 Provider 操作可包含多次 SDK 尝试；响应/失败只采集，外层关闭后统一写入。"""

    def __init__(self, binding: UsageBinding):
        self._binding = binding
        self._prices: dict[str, storage.UsagePriceSnapshot | None] = {}
        self._reserved: set[str] = set()
        self._results: list[ModelUsage] = []

    @property
    def request_usage_options(self) -> bool:
        # 选区确认是一次性发送，不能让部分兼容端点拒绝新选项后再试一次。
        return self._binding.identity.kind != "chapter.section.rewrite"

    async def begin(self, attempt_id: str, started_at: datetime) -> None:
        binding = self._binding
        async with binding.sessionmaker() as db:
            price = await storage.price_snapshot(db, binding.identity)
            self._prices[attempt_id] = price
            await storage.reserve_call(
                db, binding.identity, call_id=attempt_id, started_at=started_at,
                price=price, verify_identity=True,
            )
            await db.commit()
            self._reserved.add(attempt_id)

    async def finish(self, snapshot: ModelUsage) -> None:
        self._results.append(snapshot)

    async def _persist_result(self, result: ModelUsage) -> None:
        async with self._binding.sessionmaker() as db:
            if result.attempt_id not in self._reserved:
                # 若预留事务没能写入，以同一 ID 补写。缺少发送时价格证明时保留缺价。
                await storage.reserve_call(
                    db, self._binding.identity, call_id=result.attempt_id, started_at=result.started_at,
                    price=self._prices.get(result.attempt_id), verify_identity=True,
                )
            if result.sent:
                await storage.mark_call_sent(db, result.attempt_id, started_at=result.dispatched_at)
            await storage.finish_call(
                db, result.attempt_id, status="succeeded" if result.status == "completed" else result.status,
                sent=result.sent,
                measurement=storage.UsageMeasurement(
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                    image_count=result.image_count, cached_input_tokens=result.cached_input_tokens,
                    input_source="provider" if result.input_tokens is not None else "unknown",
                    output_source="provider" if result.output_tokens is not None else "unknown",
                    image_source="provider" if result.image_count is not None else "unknown",
                ),
                finished_at=datetime.now(UTC),
            )
            await db.commit()

    async def _persist(self) -> None:
        for result in self._results:
            for can_retry in (True, False):
                try:
                    await self._persist_result(result)
                    break
                except DBAPIError as exc:
                    if can_retry and _transient_storage_error(exc):
                        await asyncio.sleep(0.05)
                        continue
                except Exception:  # noqa: BLE001  计量异常不篡改业务异常或触发模型重发。
                    pass
                logger.warning("模型用量收尾暂不可用")
                break

    async def flush(self) -> None:
        if not self._results:
            return
        from app.services.ai_dispatch import _wait_send_cleanup

        await _wait_send_cleanup(self._persist())


def _transient_storage_error(exc: DBAPIError) -> bool:
    from app.services.ai_dispatch import is_sqlite_busy

    return (exc.connection_invalidated or getattr(exc.orig, "sqlstate", None) in ("40001", "40P01")
            or (isinstance(exc, OperationalError) and is_sqlite_busy(exc)))


async def recover_stale_usage(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """超时遗留尝试失去可确认结果。保持其他实例近期发送的记录可收尾。"""
    now = datetime.now(UTC)
    async def write() -> None:
        async with sessionmaker() as db:
            await storage.interrupt_stale_calls(db, before=now - _PENDING_MAX_AGE, finished_at=now)
            await db.commit()

    try:
        await retry_contended_write(write)
    except Exception:  # noqa: BLE001  避免后台异常泄漏连接、SQL 与参数。
        logger.warning("模型用量中断记录收尾暂不可用")


@asynccontextmanager
async def usage_maintenance(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    async def recover_periodically() -> None:
        while True:
            await asyncio.sleep(_RECOVERY_INTERVAL.total_seconds())
            await recover_stale_usage(sessionmaker)

    await recover_stale_usage(sessionmaker)
    task = asyncio.create_task(recover_periodically(), name="ai-usage-recovery")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@asynccontextmanager
async def record_operation(binding: UsageBinding | None, model: str | None) -> AsyncIterator[UsageRecorder | None]:
    if binding is None or model != binding.identity.model:
        yield None
        return
    recorder = UsageRecorder(binding)
    try:
        yield recorder
    finally:
        if hooks := current_dispatch_hooks():
            # 裸 Provider 有时被同步图片生成的外层 hooks 包围；无响应头时须等待这些租约先关闭。
            hooks.after_close(recorder.flush)
        else:
            await recorder.flush()


async def observed_stream[T](
    provider: LLMProvider, start: Callable[[], AsyncIterator[T]], recorder: UsageRecorder | None,
) -> AsyncIterator[T]:
    with observe_model_usage(recorder) if recorder is not None else nullcontext():
        stream = start() if recorder is None or provider.reports_model_usage else _fallback_stream(start)
    async with aclosing(stream):
        while True:
            with observe_model_usage(recorder) if recorder is not None else nullcontext():
                try:
                    item = await anext(stream)
                except StopAsyncIteration:
                    return
            yield item


async def _fallback_stream[T](start: Callable[[], AsyncIterator[T]]) -> AsyncIterator[T]:
    # 内存替身用同一管道验证业务接入。真实适配器在 SDK 请求边界计量拆分与回退。
    async with model_attempt() as attempt, aclosing(start()) as raw:
        async for item in attempt.iterate(raw):
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "usage" and isinstance(item[1], dict):
                attempt.set_usage(
                    input_tokens=item[1].get("input_tokens"), output_tokens=item[1].get("output_tokens"),
                    cached_input_tokens=item[1].get("cached_input_tokens"),
                )
            yield item
        attempt.mark_response()


async def observed_call[T](
    provider: LLMProvider, start: Callable[[], Awaitable[T]], recorder: UsageRecorder | None,
) -> T:
    if recorder is None:
        return await start()
    with observe_model_usage(recorder):
        if provider.reports_model_usage:
            return await start()
        async with model_attempt() as attempt:
            result = await attempt.invoke(start())
            attempt.mark_response()
            if isinstance(result, GeneratedImage):
                attempt.set_usage(image_count=1)
            return result


class _MeteredProvider(LLMProvider):
    """个人同步调用的透明包装。受控调用由原守卫直接携带计量上下文。"""

    def __init__(self, provider: LLMProvider, binding: UsageBinding):
        super().__init__(provider.api_key, provider.base_url, provider.timeout)
        self.follow_redirects = provider.follow_redirects
        self.uses_dispatch_hooks = provider.uses_dispatch_hooks
        self.reports_model_usage = provider.reports_model_usage
        self._provider = provider
        self._usage_binding = binding

    async def _stream[T](self, model: str, start: Callable[[], AsyncIterator[T]]) -> AsyncIterator[T]:
        async with record_operation(self._usage_binding, model) as recorder:
            async with aclosing(observed_stream(self._provider, start, recorder)) as observed:
                async for item in observed:
                    yield item

    async def _call[T](self, model: str, start: Callable[[], Awaitable[T]]) -> T:
        async with record_operation(self._usage_binding, model) as recorder:
            return await observed_call(self._provider, start, recorder)

    def generate_stream(
        self, messages: list[ChatMessage], model: str, *, temperature: float | None = None,
        max_tokens: int | None = None, thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        return self._stream(model, lambda: self._provider.generate_stream(
            messages, model, temperature=temperature, max_tokens=max_tokens, thinking=thinking,
        ))

    def chat_tools_stream(
        self, messages: list[AgentMessage], model: str, tools: list[ToolSpec], *,
        tool_choice: ToolChoice = "auto", max_tokens: int | None = None,
        thinking: ThinkingLevel = "off", temperature: float | None = None,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(model, lambda: self._provider.chat_tools_stream(
            messages, model, tools, tool_choice=tool_choice, max_tokens=max_tokens, thinking=thinking,
            temperature=temperature,
        ))

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return await self._call(model, lambda: self._provider.embed(texts, model))

    async def generate_image(self, prompt: str, model: str) -> GeneratedImage:
        return await self._call(model, lambda: self._provider.generate_image(prompt, model))

    async def list_models(self) -> list[ModelInfo]:
        return await self._provider.list_models()


def _bind(provider: LLMProvider, binding: UsageBinding) -> LLMProvider:
    from app.services.ai_dispatch import _GuardedProvider

    if isinstance(provider, _GuardedProvider):
        provider._usage_binding = binding
        return provider
    return _MeteredProvider(unmeter_provider(provider), binding)


def unmeter_provider(provider: LLMProvider) -> LLMProvider:
    """再建受控包裹前解掉裸计量包装，之后由 inherit_meter 把身份绑定到新守卫。"""
    return provider._provider if isinstance(provider, _MeteredProvider) else provider


def inherit_meter(provider: LLMProvider, source: LLMProvider) -> LLMProvider:
    from app.services.ai_dispatch import _GuardedProvider

    if isinstance(source, (_GuardedProvider, _MeteredProvider)) and source._usage_binding is not None:
        return _bind(provider, source._usage_binding)
    return provider


def meter_provider(
    provider: LLMProvider, config: ModelConfig, sessionmaker: async_sessionmaker[AsyncSession], *,
    actor_id: int, kind: str, project_id: int | None = None, project_owner_id: int | None = None,
    chapter_id: int | None = None, task_id: int | None = None, task_key: str | None = None,
    actor_created_at: datetime | None = None, payer_created_at: datetime | None = None,
    chapter_created_at: datetime | None = None,
) -> LLMProvider:
    """只在完成原业务身份和模型校验后绑定。记录层不会参与发送授权。"""
    identity = storage.UsageCallIdentity(
        actor_id=actor_id, payer_id=config.user_id, project_id=project_id, chapter_id=chapter_id,
        task_id=task_id, task_key=task_key, config_instance_key=config.instance_key,
        actor_created_at=actor_created_at, chapter_created_at=chapter_created_at,
        payer_created_at=actor_created_at if config.user_id == actor_id else payer_created_at,
        author_paid=project_id is not None and project_owner_id == config.user_id,
        provider=config.provider, model=config.model, model_type=config.model_type, kind=kind,
    )
    return _bind(provider, UsageBinding(identity, sessionmaker))
