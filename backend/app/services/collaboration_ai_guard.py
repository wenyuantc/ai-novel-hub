"""作品 AI 出站的内部身份与模型绑定；由受信任的服务端任务和 HTTP 路径使用。

必须与 ``ai_dispatch.guard_provider`` 的真实 HTTP lease 一起使用：认证/User
读屏障在发送门禁之前，项目、任务和模型绑定在门禁取得后复核。发送的响应头到达后
锁随 lease 释放；响应流、结果保存及任务清理必须由调用者另行复验。
"""

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import AnthropicProvider, GeminiProvider, OpenAICompatProvider, ProviderFactory, create_provider
from app.ai.providers.base import ChatMessage, GeneratedImage, ImageOptions, LLMProvider, ThinkingLevel
from app.ai.tools import AgentEvent, AgentMessage, ToolChoice, ToolSpec
from app.core.security import SecretCipher
from app.models import AIConfig, Chapter, Project, Task, User
from app.services import (
    ai_config_service,
    ai_dispatch,
    ai_usage_recording,
    chapter_claim,
    project_ai_config,
    project_lifecycle,
    scene_ai_context,
)
from app.services.ai_dispatch import (
    VisibilityBusyError,
    VisibilityChangedError,
    VisibilityPolicyError,
    VisibilityStamp,
    guard_provider,
    require_visibility_stamp,
)
from app.services.authorization_service import AuthenticatedActor, require_authenticated, require_authenticated_for_send
from app.services.collaboration_task_access import GRANT_KEY
from app.services.task_engine import execution_identity

type ModelBinding = project_ai_config.ResolvedProjectModel | project_ai_config.ResolvedSharedEmbedding
type AfterVerify = Callable[[], Awaitable[None]]
type HttpSourceVerify = Callable[[AsyncSession], Awaitable[None]]
MODEL_KEY = "_project_model"
EMBEDDING_KEY = "_shared_embedding"
_PROVIDER_TYPES = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


class CollaborationSendBlocked(VisibilityPolicyError):
    """受控发送失败，不可作为普通模型故障降级或自动重试。"""

    def __init__(self) -> None:
        super().__init__("权限或模型授权已变化，已停止发送", extra={"code": "permission_denied"})


class _ChapterValidationFailure(Exception):
    """内部标记：只用于识别章节原语自己的失效结果。"""


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**31 - 1


def _uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _trusted_factory(factory: ProviderFactory) -> bool:
    # factory 可执行任意代码，必须在调用前判断；不可信工厂甚至可能在构造时发网络。
    return factory is create_provider


def _trusted_provider(factory: ProviderFactory, provider: LLMProvider, provider_name: str) -> bool:
    # 真实实现经逐跳 hooks 验证；不能只信可由第三方自行声明的 uses_dispatch_hooks。
    return (factory is create_provider and type(provider) is _PROVIDER_TYPES.get(provider_name)
            and provider.uses_dispatch_hooks is True)


def _contains_database_error(exc: BaseException) -> bool:
    # SDK 可能把 HTTP hook 的数据库异常包为一般模型错误；沿两种异常链查找，防止泄漏 SQL/参数。
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, SQLAlchemyError):
            return True
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                pending.append(nested)
    return False


def _database_failure_type(exc: Exception) -> type[VisibilityPolicyError] | None:
    if not _contains_database_error(exc):
        return None
    return VisibilityBusyError if isinstance(exc, VisibilityBusyError) else CollaborationSendBlocked


@dataclass(frozen=True, slots=True, repr=False)
class BatchAttemptIdentity:
    """由原执行体在章节 claim 事务中捕获；不得从后来的 checkpoint 重签。"""

    claim: chapter_claim.ChapterClaim
    cursor: int
    attempt_key: str


class _BoundModelProvider(LLMProvider):
    """只允许调用由固定配置构造的 provider 和模型名称。"""

    def __init__(
        self, provider: LLMProvider, model: str, model_type: str, *,
        embedding_inputs: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(provider.api_key, provider.base_url, provider.timeout)
        self.follow_redirects = provider.follow_redirects
        self.uses_dispatch_hooks = provider.uses_dispatch_hooks
        self.reports_model_usage = provider.reports_model_usage
        self._provider = provider
        self._model = model
        self._model_type = model_type
        self._embedding_inputs = embedding_inputs

    def _check(self, model: str, model_type: str) -> None:
        if (type(model) is not str or model != self._model or model_type != self._model_type
                or self._provider.api_key != self.api_key
                or self._provider.base_url != self.base_url
                or self._provider.follow_redirects != self.follow_redirects):
            raise CollaborationSendBlocked()

    async def generate_stream(
        self, messages: list[ChatMessage], model: str, *, temperature: float | None = None,
        max_tokens: int | None = None, thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        self._check(model, "text")
        try:
            async with aclosing(self._provider.generate_stream(
                messages, model, temperature=temperature, max_tokens=max_tokens, thinking=thinking,
            )) as stream:
                async for part in stream:
                    yield part
            return
        except Exception as exc:
            failure = _database_failure_type(exc)
            if failure is None:
                raise
        raise failure()

    async def chat_tools_stream(
        self, messages: list[AgentMessage], model: str, tools: list[ToolSpec], *, tool_choice: ToolChoice = "auto",
        max_tokens: int | None = None, thinking: ThinkingLevel = "off", temperature: float | None = None,
    ) -> AsyncIterator[AgentEvent]:
        self._check(model, "text")
        try:
            async with aclosing(self._provider.chat_tools_stream(
                messages, model, tools, tool_choice=tool_choice, max_tokens=max_tokens, thinking=thinking,
                temperature=temperature,
            )) as stream:
                async for event in stream:
                    yield event
            return
        except Exception as exc:
            failure = _database_failure_type(exc)
            if failure is None:
                raise
        raise failure()

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        self._check(model, "embedding")
        if self._embedding_inputs is not None and (
            type(texts) is not list or any(type(value) is not str for value in texts)
            or tuple(texts) != self._embedding_inputs
        ):
            raise CollaborationSendBlocked()
        try:
            # 调用方可能在异步网络期间修改自己的 list；网络只看到冻结快照的新列表。
            selected = list(self._embedding_inputs) if self._embedding_inputs is not None else texts
            return await self._provider.embed(selected, model)
        except Exception as exc:
            failure = _database_failure_type(exc)
            if failure is None:
                raise
        raise failure()

    async def generate_image(self, prompt: str, model: str, *, options: ImageOptions | None = None) -> GeneratedImage:
        kwargs = {"options": options} if options is not None else {}
        self._check(model, "image")
        try:
            return await self._provider.generate_image(prompt, model, **kwargs)
        except Exception as exc:
            failure = _database_failure_type(exc)
            if failure is None:
                raise
        raise failure()


@dataclass(frozen=True, slots=True, repr=False)
class BoundProvider:
    """仅供服务端执行；对象不得直接序列化或写应用日志，生成记录须另按隐私合同投影。"""

    provider: LLMProvider
    model: str
    provider_name: str
    thinking_level: str
    context_window_k: int | None
    max_output_k: int | None

    def __repr__(self) -> str:
        return "<BoundProvider>"


def _checked_binding(binding: ModelBinding) -> None:
    """校验内部 dataclass 结构；不会替代发送屏障内的数据库重验。"""
    if type(binding) is project_ai_config.ResolvedProjectModel:
        if binding.model_type not in ("text", "image"):
            raise CollaborationSendBlocked()
        project_ai_config.serialize_project_model(binding)
    elif type(binding) is project_ai_config.ResolvedSharedEmbedding:
        project_ai_config.serialize_shared_embedding(binding)
        if binding.mode != "vector" or binding.model is None:
            # 关键词选择没有向量出站资格，即使调用方恰好持有别的 provider。
            raise CollaborationSendBlocked()
    else:
        raise CollaborationSendBlocked()


def _matches_task_binding(payload: object, binding: ModelBinding, *, legacy_owner_index: bool = False) -> bool:
    """可信 Task 行内的独立 grant 和模型绑定必须与本执行体的原绑定一致。

    ``legacy_owner_index`` 仅供 kb.index：作者文档/重建/旧记忆任务不在 Task 中冻结选择，接受作者本人
    默认向量的运行时绑定（缺 grant、缺选择、actor 即作者）；每次发送/写入仍由 revalidate 重验该配置。
    """
    if type(payload) is not dict:
        return False
    expected = {
        "actor_id": binding.actor_id, "project_id": binding.project_id, "project_owner_id": binding.project_owner_id,
    }
    if type(binding) is project_ai_config.ResolvedSharedEmbedding:
        selected = project_ai_config.deserialize_shared_embedding(
            payload.get(EMBEDDING_KEY, project_ai_config.SHARED_EMBEDDING_ABSENT), **expected,
            task_grant=payload.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
            allow_legacy_owner=legacy_owner_index is True,
        )
        if selected is None:
            from app.services.retrieval_service import legacy_owner_index_selection

            return legacy_owner_index is True and GRANT_KEY not in payload and legacy_owner_index_selection(binding)
    else:
        selected = project_ai_config.deserialize_project_model(
            payload.get(MODEL_KEY, project_ai_config.PROJECT_MODEL_ABSENT), **expected,
        )
        grant = binding.grant_identity
        if grant is None:
            if GRANT_KEY in payload:
                return False
        else:
            raw = payload.get(GRANT_KEY)
            if (type(raw) is not dict or set(raw) != {"invitation_id", "revision"}
                    or type(raw["invitation_id"]) is not str or type(raw["revision"]) is not int
                    or (raw["invitation_id"], raw["revision"]) != grant):
                return False
    return selected == binding


async def _lock_active_project(db: AsyncSession, project_id: int) -> None:
    # 与内容写入的项目父行相同；位于发送门禁之后、任务和配置锁之前。
    row = await db.scalar(select(Project.id).where(
        Project.id == project_id, *project_lifecycle.active_conditions(),
    ).with_for_update(read=True))
    if row is None:
        raise CollaborationSendBlocked()


async def require_model_bindings(db, primary, extras, *, cipher, allow_private, write=False) -> None:
    # 多绑定不能先锁文本配置行再等向量配置选择门禁，否则与配置写入的门禁→行锁顺序相反。
    for owner_id in sorted({primary.actor_id, primary.project_owner_id}):
        await ai_config_service.lock_config_owner(db, owner_id, write=False)
    bindings = sorted((primary, *extras),
                      key=lambda value: type(value) is project_ai_config.ResolvedSharedEmbedding)
    for binding in bindings:
        if type(binding) is project_ai_config.ResolvedSharedEmbedding:
            await project_ai_config.revalidate_shared_embedding(
                db, binding, cipher=cipher, allow_private=allow_private,
                for_update=write, lock_config=True,
            )
        else:
            await project_ai_config.revalidate_project_model(
                db, binding, cipher=cipher, allow_private=allow_private,
                for_update=write, lock_config=True,
            )


async def require_agent_stream_conversation(db, actor_id, project_id, cid, grant_identity, epoch) -> None:
    """单帧共享锁内复验会话的原归属、邀请及历史代次。"""
    from app.models import AgentConversation

    expected_invitation = None if grant_identity is None else grant_identity[0]
    statement = select(
        AgentConversation.id,
        AgentConversation.collaboration_invitation_id,
        AgentConversation.ai_history_epoch,
    ).where(
        AgentConversation.id == cid,
        AgentConversation.project_id == project_id,
        AgentConversation.user_id == actor_id,
    )
    if db.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(read=True)
    row = (await db.execute(statement)).one_or_none()
    if (row is None or row.collaboration_invitation_id != expected_invitation
            or row.ai_history_epoch != epoch):
        raise CollaborationSendBlocked()


def _matches_batch_attempt(task: Task, expected: execution_identity.TaskExecutionIdentity,
                           attempt: BatchAttemptIdentity) -> bool:
    """只允许本次章节 attempt 改变游标和断点；不得从新断点重新签发身份。"""
    try:
        return _batch_attempt_state(task.checkpoint, task.step_cursor, expected, attempt)
    except (AttributeError, TypeError, ValueError, RecursionError):
        return False


def _batch_attempt_state(
    checkpoint: object, step_cursor: object, expected: execution_identity.TaskExecutionIdentity,
    attempt: BatchAttemptIdentity,
) -> bool:
    """断点与游标恰为本次章节 attempt 写入的状态（持锁 Task 行与发布身份共用同一判定）。"""
    try:
        claim, cursor = attempt.claim, attempt.cursor
        original = expected.input
        ids = original.get("chapter_ids")
        if (type(cursor) is not int or cursor < expected.step_cursor or type(ids) is not list
                or cursor >= len(ids) or type(ids[cursor]) is not int or ids[cursor] != claim.chapter_id
                or type(checkpoint) is not dict or checkpoint.get("phase") != "generate"
                or any(key in checkpoint for key in ("_batch_completion", "rich_analysis", "_batch_outcome_unknown"))
                or checkpoint.get("_batch_attempt") != attempt.attempt_key
                or not _uuid(attempt.attempt_key) or attempt.attempt_key in (
                    expected.task_key, expected.execution_key, claim.claim_key,
                )):
            return False
        items = checkpoint.get("items")
        current = checkpoint.get("current")
        pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
        return bool(type(items) is list and cursor < len(items) and type(items[cursor]) is dict
                    and type(items[cursor].get("chapter_id")) is int
                    and items[cursor]["chapter_id"] == claim.chapter_id
                    and type(current) is dict and type(current.get("chapter_id")) is int
                    and type(current.get("_chapter_claim")) is dict
                    and type(current["_chapter_claim"].get("chapter_id")) is int
                    and current == {"chapter_id": claim.chapter_id, "prev_status": claim.prev_status,
                                    "_chapter_claim": pointer}
                    and type(step_cursor) is int and step_cursor == cursor)
    except (AttributeError, TypeError, ValueError, RecursionError):
        return False


async def _check_task(
    db: AsyncSession, expected: execution_identity.TaskExecutionIdentity, binding: ModelBinding,
    attempt: BatchAttemptIdentity | None, *, write: bool = False, analysis_execution: object = None,
    analysis_pending: object = None, analysis_reference: object = None, streaming: bool = False,
) -> None:
    # 同时锁住原执行轮次：取消/重试不能在最终检查后、HTTP 发出前穿透。
    row = await db.scalar(select(Task).where(
        Task.id == expected.task_id, Task.statistics_key == expected.task_key, Task.user_id == expected.actor_id,
        Task.project_id == expected.project_id, Task.chapter_id == expected.chapter_id, Task.kind == expected.kind,
        Task.status == "running", Task.queued_at == expected.queued_at, Task.execution_key == expected.execution_key,
        Task.admin_cancel_operation_id.is_(None),
    ).with_for_update(read=not write).execution_options(populate_existing=True))
    if row is None or not _matches_task_binding(row.input, binding, legacy_owner_index=expected.kind == "kb.index"):
        raise CollaborationSendBlocked()
    if not execution_identity.matches_running_base(expected, row):
        raise CollaborationSendBlocked()
    await execution_identity.require_live_worker(db, expected.worker_key, read_only=not write)
    if analysis_execution is not None:
        from app.services import analysis_service

        if (analysis_execution.task_id != expected.task_id
                or analysis_execution.task_key != expected.task_key
                or analysis_execution.actor_id != expected.actor_id
                or analysis_execution.project_id != expected.project_id
                or analysis_execution.task_chapter_id != expected.chapter_id
                or analysis_execution.task_kind != expected.kind
                or not _same_time(analysis_execution.queued_at, expected.queued_at)
                or analysis_execution.execution_key != expected.execution_key
                or analysis_execution.grant_identity != expected.grant_identity
                or analysis_execution.actor_token_version != expected.actor_token_version
                or analysis_execution.input_hash != analysis_service.analysis_input_hash(expected.input)):
            raise CollaborationSendBlocked()
        await analysis_service._lock_analysis_execution(  # noqa: SLF001
            db, analysis_execution, status="running", read_lock=not write,
        )
        if analysis_pending is not None:
            if row.step_cursor != expected.step_cursor:
                raise CollaborationSendBlocked()
            await analysis_service._require_analysis_pending(db, analysis_pending)  # noqa: SLF001
            initial = expected.checkpoint
            if initial is not None and type(initial) is not dict:
                raise CollaborationSendBlocked()
            initial = dict(initial or {})
            if "_analysis_pending" in initial or "_analysis_completion" in initial:
                raise CollaborationSendBlocked()
            initial["_analysis_pending"] = analysis_service._pending_document(analysis_pending)  # noqa: SLF001
            if json.dumps(row.checkpoint, sort_keys=True, separators=(",", ":"), allow_nan=False) != json.dumps(
                initial, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ):
                raise CollaborationSendBlocked()
        if analysis_reference is not None:
            if streaming and not write:
                # 受控任务流只读核对（E5-c2）：本章分析收据仍为本执行的 running，或已由本执行完成。
                await analysis_service._require_batch_analysis(  # noqa: SLF001
                    db, analysis_execution, analysis_reference, read_lock=True, streaming=True,
                )
            else:
                # 发送/写入路径不变：只接受 running。
                await analysis_service._require_batch_analysis(  # noqa: SLF001
                    db, analysis_execution, analysis_reference, read_lock=not write,
                )
        return
    if attempt is None:
        valid = execution_identity.matches_running(expected, row)
    else:
        # 批次断点可以推进，只将领取时的不可变字段和原输入与当前行比较。
        valid = _matches_batch_attempt(row, expected, attempt)
    if not valid:
        raise CollaborationSendBlocked()


def _transaction_identity(db: AsyncSession):
    transaction = db.get_transaction()
    if transaction is None or not transaction.is_active or db.get_nested_transaction() is not None:
        raise CollaborationSendBlocked()
    return transaction.sync_transaction


def _reject_pending(db: AsyncSession) -> None:
    # 身份查询使用 populate_existing；先拒绝暂存改动，避免静默覆盖未 flush 的撤权或身份字段。
    if db.new or db.dirty or db.deleted:
        raise CollaborationSendBlocked()


def _same_time(first: datetime, second: datetime) -> bool:
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    return utc(first) == utc(second)


def _matches_chapter_identity(
    chapter: chapter_claim.ChapterClaim, task: execution_identity.TaskExecutionIdentity, binding: ModelBinding,
    scene: scene_ai_context.SceneContextStamp | None,
) -> bool:
    return (
        type(chapter) is chapter_claim.ChapterClaim and chapter.phase == "running"
        and chapter.kind in ("single", "batch") and chapter.prev_status in ("draft", "done")
        and all(_positive(value) for value in (
            chapter.task_id, chapter.actor_id, chapter.project_id, chapter.chapter_id,
        )) and _uuid(chapter.claim_key)
        and chapter.claim_key not in (task.task_key, task.execution_key)
        and chapter.task_id == task.task_id and chapter.task_key == task.task_key
        and chapter.actor_id == task.actor_id and chapter.project_id == task.project_id
        and isinstance(chapter.queued_at, datetime) and _same_time(chapter.queued_at, task.queued_at)
        and chapter.execution_key == task.execution_key and chapter.worker_key == task.worker_key
        and chapter.grant_identity == binding.grant_identity
        and ((chapter.kind == "single" and task.kind in ("chapter.generate", "chapter.polish", "chapter.revise")
              and task.chapter_id == chapter.chapter_id)
             or (chapter.kind == "batch" and task.kind == "chapter.batch" and task.chapter_id is None))
        and (chapter.grant_identity is None or type(chapter.grant_identity) is tuple)
        and (scene is None or scene.chapter_id == chapter.chapter_id)
    )


@dataclass(frozen=True, slots=True, repr=False)
class SendGuard:
    """一次执行的固定绑定；不要记录或向客户端序列化本对象。"""

    _actor: AuthenticatedActor
    _binding: ModelBinding
    _stamp: VisibilityStamp
    _cipher: SecretCipher
    _allow_private: bool
    _task: execution_identity.TaskExecutionIdentity | None
    _chapter: chapter_claim.ChapterClaim | None
    _batch_attempt: BatchAttemptIdentity | None
    _analysis_execution: object
    _analysis_pending: object
    _analysis_reference: object
    _extra_bindings: tuple[ModelBinding, ...]
    _resume: object
    _document_claim: object
    _entity_sources: object
    _embedding_inputs: tuple[object, ...] | None
    _scene: scene_ai_context.SceneContextStamp | None
    _after_verify: AfterVerify | None
    _http_source_verify: HttpSourceVerify | None
    # 仅供受控任务流发布证明（批量章与章之间 E5-c2；向导保存后 done 帧 E5-c3）：不能用于任何发送、预检或写屏障。
    _publication_only: bool = False
    # 仅向导续写的保存后发布 guard（E5-c3）：构建戳 epoch，来源 epoch 与之比较（本任务覆盖保存可能推进 epoch）；
    # 其余 guard 恒为 None，来源 epoch 与当前 epoch 比较。
    _resume_epoch: int | None = None

    def __repr__(self) -> str:
        return "<SendGuard>"

    async def authorize(self, db: AsyncSession) -> None:
        """在发送门禁前锁定并重验 HTTP 原令牌或可信后台 actor。"""
        try:
            with db.no_autoflush:
                _reject_pending(db)
                await ai_dispatch.authorize_send_actor(db, self._actor)
            return
        except Exception:  # noqa: BLE001  数据库、账号和令牌失败均失败关闭且不泄露原异常。
            pass
        raise CollaborationSendBlocked()

    async def _verify_locked(self, db: AsyncSession, *, preflight: bool) -> None:
        await _lock_active_project(db, self._binding.project_id)
        # 调用方可能还在检查缓存命中；锁后再次校验才能避免预检与项目锁之间的代次变化。
        await require_visibility_stamp(db, self._stamp)
        if self._scene is not None:
            await scene_ai_context.require_stamp(db, self._scene, sending=True)
        if self._task is not None:
            await self._verify_task(db)
        if self._document_claim is not None:
            from app.services.kb_index_service import require_sending_document_claim

            await require_sending_document_claim(
                db, self._document_claim, actor=self._actor, grant_identity=self._binding.grant_identity,
                preflight=preflight,
            )
        if self._embedding_inputs is not None:
            from app.services.retrieval_service import require_sending_embedding_snapshots

            await require_sending_embedding_snapshots(
                db, self._embedding_inputs, actor=self._actor, execution=self._task, selection=self._binding,
                cipher=self._cipher, allow_private=self._allow_private, preflight=preflight,
            )
        if self._chapter is not None:
            # 仅真实 lease 可提供可验证证明；须先持原 Task，再取章节锁，最后取配置锁。
            try:
                await chapter_claim.require_sending_chapter(db, self._chapter)
            except VisibilityPolicyError as exc:
                if (exc.extra or {}).get("code") == "chapter_claim_changed":
                    raise _ChapterValidationFailure() from None
                raise
        await self._verify_entity_sources(db)
        await self._verify_resume(db)
        await self._verify_bindings(db)
        if self._http_source_verify is not None:
            # 仅内部 HTTP 调用方可附加来源核对；同一物理发送事务内不允许提交或挂起写入。
            original_transaction = _transaction_identity(db)
            await self._http_source_verify(db)
            _reject_pending(db)
            if _transaction_identity(db) is not original_transaction:
                raise CollaborationSendBlocked()

    async def _verify_task(self, db: AsyncSession, *, write: bool = False, streaming: bool = False) -> None:
        if self._task is None:
            raise CollaborationSendBlocked()
        await _check_task(
            db, self._task, self._binding, self._batch_attempt, write=write,
            analysis_execution=self._analysis_execution, analysis_pending=self._analysis_pending,
            analysis_reference=self._analysis_reference, streaming=streaming,
        )

    async def _verify_resume(self, db: AsyncSession) -> None:
        if self._resume is not None:
            from app.services.wizard_service import verify_resume_source

            await verify_resume_source(db, self._resume, for_update=True, expected_epoch=self._resume_epoch)

    async def _verify_entity_sources(
        self, db: AsyncSession, *, write: bool = False, allow_changes: bool = False,
    ) -> None:
        if self._entity_sources is not None:
            from app.services.entity_ai_service import require_entity_source_snapshot

            await require_entity_source_snapshot(
                db, self._entity_sources, for_update=write, allow_changes=allow_changes,
            )

    async def _verify_bindings(self, db: AsyncSession, *, write: bool = False) -> None:
        await require_model_bindings(db, self._binding, self._extra_bindings, cipher=self._cipher,
                                     allow_private=self._allow_private, write=write)

    def _stream_readable(self) -> bool:
        """受控任务流可证明的执行类型：大纲/实体（无占用）、单章（章节占用只读核对）、
        单独的章节分析（原执行 + pending 共享锁核对）、批量的三种发布 guard（E5-c2）、
        向导的两种发布 guard（E5-c3）、以及智能体轮次的仅发布 guard。
        文档占用与向量输入尚无帧租约验证协议（E6），继续失败关闭。"""
        if self._task is not None and self._task.kind == "chapter.batch":
            return self._batch_stream_shape() is not None
        if self._task is not None and self._task.kind in _WIZARD_KINDS:
            return self._wizard_stream_shape() is not None
        if self._task is not None and self._task.kind == "agent.turn":
            return self._agent_stream_shape()
        return (self._task is not None
                and self._batch_attempt is None and self._analysis_reference is None
                and self._document_claim is None and self._embedding_inputs is None
                and (self._chapter is None or (self._chapter.kind == "single" and self._analysis_execution is None))
                and (self._analysis_execution is None or self._task.kind == "chapter.analyze"))

    def _agent_stream_shape(self) -> bool:
        """智能体轮次发布 guard 的形状；只接受 AgentHandler.stream_guard 构造的仅发布 guard。

        不绑定章节/批量/分析/文档/向量输入/实体来源/向导续写/场景戳，也不带一次性回调；
        主绑定必须是文本模型，附加绑定最多一份与原执行同 grant 的共享向量选择；任务输入须含
        合法会话 ID（与 resource_key 一致）与同代次 history_epoch。
        """
        task = self._task
        if (type(task) is not execution_identity.TaskExecutionIdentity or task.kind != "agent.turn"
                or task.chapter_id is not None or not self._publication_only
                or self._chapter is not None or self._batch_attempt is not None
                or self._analysis_execution is not None or self._analysis_pending is not None
                or self._analysis_reference is not None or self._document_claim is not None
                or self._entity_sources is not None or self._embedding_inputs is not None
                or self._resume is not None or self._resume_epoch is not None
                or self._scene is not None or self._after_verify is not None
                or self._http_source_verify is not None
                or type(self._binding) is not project_ai_config.ResolvedProjectModel
                or self._binding.model_type != "text"
                or type(self._extra_bindings) is not tuple or len(self._extra_bindings) > 1
                or any(type(extra) is not project_ai_config.ResolvedSharedEmbedding
                       or extra.grant_identity != task.grant_identity for extra in self._extra_bindings)):
            return False
        try:
            raw = task.input
            req = raw.get("request") if type(raw) is dict else None
            cid = req.get("conversation_id") if type(req) is dict else None
            epoch = raw.get("history_epoch") if type(raw) is dict else None
            return (
                _positive(cid) and task.resource_key == f"agent:{cid}"
                and type(epoch) is int and epoch == self._stamp.epoch
            )
        except Exception:  # noqa: BLE001
            return False

    async def _verify_agent_conversation(self, db: AsyncSession) -> None:
        """智能体受控流帧租约内只读核对会话归属、原邀请轮次与开放历史代次。"""
        if self._task is None or self._task.kind != "agent.turn":
            return
        raw = self._task.input
        req = raw.get("request") if type(raw) is dict else None
        cid = req.get("conversation_id") if type(req) is dict else None
        if not _positive(cid):
            raise CollaborationSendBlocked()
        await require_agent_stream_conversation(
            db, self._task.actor_id, self._task.project_id, cid, self._task.grant_identity, self._stamp.epoch,
        )

    def _batch_stream_shape(self) -> str | None:
        """批量发布 guard 的形状；只接受 BatchHandler.stream_guard 构造的三种，其余为 None。

        - ``attempt``：本章 attempt + 同一批量章节占用（章内帧；构建前无场景戳，构建后有）；
        - ``analysis``：本章逐章分析的原执行与收据引用（分析中的帧），无占用、无场景戳；
        - ``between``：无当前章（章与章之间、审核点、完成），guard 原执行即发布时断点。
        """
        if (self._task is None or self._task.kind != "chapter.batch" or self._task.chapter_id is not None
                or self._document_claim is not None or self._embedding_inputs is not None
                or self._analysis_pending is not None or self._resume is not None or self._entity_sources is not None
                or len(self._extra_bindings) != 1
                or type(self._extra_bindings[0]) is not project_ai_config.ResolvedSharedEmbedding):
            return None
        if self._batch_attempt is not None:
            attempt = self._batch_attempt
            if (type(attempt) is BatchAttemptIdentity and type(self._chapter) is chapter_claim.ChapterClaim
                    and self._chapter.kind == "batch" and attempt.claim == self._chapter
                    and self._analysis_execution is None and self._analysis_reference is None
                    and (self._scene is None or self._scene.chapter_id == self._chapter.chapter_id)):
                return "attempt"
            return None
        if self._chapter is not None or self._scene is not None:
            return None
        if self._analysis_execution is not None:
            return ("analysis" if self._analysis_reference is not None and not self._publication_only
                    else None)
        return "between" if self._analysis_reference is None and self._publication_only else None

    def _wizard_stream_shape(self) -> str | None:
        """向导发布 guard 的形状（E5-c3）；只接受 WizardHandler 构造的两种，其余为 None。

        - ``pending``：构建时的发送 guard，原执行断点恰带本轮未决凭据（运行中的 stage/delta/error 帧）；
        - ``completed``：保存后仅发布的 guard，原执行断点恰带由同一凭据派生的完成收据，
          可见性戳为保存事务内读取的新戳（done 帧）；永不可发送、预检或写入。
        两者都没有章节/批量/分析/文档/向量/实体来源/场景戳，也不夹带一次性回调。额外绑定只允许
        受理时冻结的那一份共享向量选择（E6-d 协作执行；E6-f 裁决 13 起作者执行同样带；构造时已由
        ``_check_extra_bindings`` 核对与 Task 输入一致，且与原执行同一 grant），主绑定必须是文本模型。
        """
        task = self._task
        if (type(task) is not execution_identity.TaskExecutionIdentity or task.kind not in _WIZARD_KINDS
                or task.chapter_id is not None or self._chapter is not None or self._batch_attempt is not None
                or self._analysis_execution is not None or self._analysis_pending is not None
                or self._analysis_reference is not None or self._document_claim is not None
                or self._entity_sources is not None or self._embedding_inputs is not None
                or self._scene is not None or self._after_verify is not None
                or type(self._binding) is not project_ai_config.ResolvedProjectModel
                or type(self._extra_bindings) is not tuple or len(self._extra_bindings) > 1
                or any(type(extra) is not project_ai_config.ResolvedSharedEmbedding
                       or extra.grant_identity != task.grant_identity for extra in self._extra_bindings)):
            return None
        try:
            checkpoint = task.checkpoint
            if type(checkpoint) is not dict:
                return None
            if self._publication_only:
                if "_wizard_pending" in checkpoint or not _wizard_marker_matches(
                    checkpoint.get("_wizard_completion"), task, completed=True,
                ):
                    return None
                return "completed"
            if "_wizard_completion" in checkpoint or not _wizard_marker_matches(
                checkpoint.get("_wizard_pending"), task, completed=False,
            ):
                return None
            return "pending"
        except Exception:  # noqa: BLE001
            return None

    def _terminal_input_matches(self, value: object) -> bool:
        """终态 Task.input 须与领取时逐字一致；单章释放占用时只允许移除本 guard 原章节占用指针。"""
        def canonical(item: object) -> str:
            return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

        current = canonical(value)
        if current == self._task.input_json:
            return True
        if self._chapter is None or type(value) is not dict:
            return False
        expected = json.loads(self._task.input_json)
        pointer = expected.pop("_chapter_claim", None) if type(expected) is dict else None
        return (pointer == {"chapter_id": self._chapter.chapter_id, "claim_key": self._chapter.claim_key,
                            "prev_status": self._chapter.prev_status}
                and "_chapter_claim" not in value and current == canonical(expected))

    async def require_task_read(self, db: AsyncSession) -> None:
        """只用于中央已登记的同事务 ASGI 帧租约，不能自行声称持有物理发送锁。"""
        failure: VisibilityPolicyError
        try:
            from app.services.task_engine.stream_lease import (
                require_task_stream_lease,
                require_task_stream_physical_lease,
            )

            with db.no_autoflush:
                _reject_pending(db)
                if not self._stream_readable():
                    raise CollaborationSendBlocked()
                kwargs = {"task_id": self._task.task_id, "task_key": self._task.task_key}
                await require_task_stream_physical_lease(db, self._actor.id, self._binding.project_id, **kwargs)
                await require_task_stream_lease(db, self._actor.id, self._binding.project_id, **kwargs)
                await require_authenticated_for_send(db, self._actor)
                await _lock_active_project(db, self._binding.project_id)
                await require_visibility_stamp(db, self._stamp)
                if self._scene is not None:
                    await scene_ai_context.require_stamp(db, self._scene, sending=True)
                # 分析的原执行与 pending 凭据在这里按共享锁核对（_check_task 的 read_lock 分支）；
                # 批量 attempt 按 _matches_batch_attempt、批量逐章分析按原收据只读核对（E5-c2）。
                await self._verify_task(db, streaming=True)
                if self._chapter is not None:
                    # E5-c1：只读核对本执行仍持原章节占用；不取写锁、不要求发送 lease，
                    # 以免与同一帧租约的共享锁或运行体的发送 lease 自锁。
                    await chapter_claim.require_streaming_chapter(db, self._chapter)
                await self._verify_agent_conversation(db)
                await self._verify_entity_sources(db, allow_changes=True)
                await self._verify_resume(db)
                await self._verify_bindings(db)
                await require_task_stream_lease(db, self._actor.id, self._binding.project_id, **kwargs)
                await require_task_stream_physical_lease(db, self._actor.id, self._binding.project_id, **kwargs)
            return
        except scene_ai_context.SceneContextChanged:
            failure = scene_ai_context.SceneContextChanged()
        except VisibilityChangedError:
            failure = VisibilityChangedError()
        except VisibilityBusyError:
            failure = VisibilityBusyError()
        except VisibilityPolicyError as exc:
            if (exc.extra or {}).get("code") == "entity_source_changed":
                failure = VisibilityPolicyError("设定来源已变化，请重新发起", extra={"code": "entity_source_changed"})
            elif (exc.extra or {}).get("code") == "wizard_resume_changed":
                from app.services.wizard_service import WizardResumeChanged

                failure = WizardResumeChanged()
            else:
                failure = CollaborationSendBlocked()
        except Exception:  # noqa: BLE001
            failure = CollaborationSendBlocked()
        raise failure

    async def require_terminal_read(self, db: AsyncSession, terminal: object) -> None:
        """只接受本帧真实 ASGI 租约中由引擎固定的原终态证据。"""
        failure: VisibilityPolicyError
        try:
            from app.services.task_engine.execution_identity import same_execution
            from app.services.task_engine.stream_evidence import TerminalEvidence, capture_task_view
            from app.services.task_engine.stream_lease import (
                require_task_stream_physical_lease,
                require_task_stream_terminal,
            )

            with db.no_autoflush:
                _reject_pending(db)
                if (not self._stream_readable() or type(terminal) is not TerminalEvidence
                        or not same_execution(self._task, terminal.execution)):
                    raise CollaborationSendBlocked()
                kwargs = {"task_id": self._task.task_id, "task_key": self._task.task_key}
                await require_task_stream_physical_lease(db, self._actor.id, self._binding.project_id, **kwargs)
                await require_task_stream_terminal(db, self._actor.id, self._binding.project_id,
                                                   terminal=terminal, **kwargs)
                await require_authenticated_for_send(db, self._actor)
                await _lock_active_project(db, self._binding.project_id)
                await require_visibility_stamp(db, self._stamp)
                if self._scene is not None and self._chapter is None:
                    # 单章：场景戳含目标章 content_revision，本执行自己的保存必然改变它；
                    # 与引擎 done/paused 发布核验（收据 + 可见性 + 模型，不比场景戳）一致，
                    # 终态只认下方逐字一致的持久 Task 视图（含 result/checkpoint）。章节占用此时已释放，不再核对。
                    await scene_ai_context.require_stamp(db, self._scene, sending=True)
                row = await db.scalar(select(Task).where(
                    Task.id == self._task.task_id, Task.statistics_key == self._task.task_key,
                    Task.user_id == self._task.actor_id, Task.project_id == self._task.project_id,
                    Task.chapter_id == self._task.chapter_id, Task.kind == self._task.kind,
                    Task.queued_at == self._task.queued_at,
                ).with_for_update(read=True).execution_options(populate_existing=True))
                if (row is None or row.status not in ("succeeded", "failed", "cancelled", "interrupted", "paused")
                        or row.resource_key != self._task.resource_key or row.serial_key != self._task.serial_key
                        or row.retry_of_id != self._task.retry_of_id or capture_task_view(row) != terminal.view
                        or not self._terminal_input_matches(row.input)):
                    raise CollaborationSendBlocked()
                await self._verify_agent_conversation(db)
                await self._verify_entity_sources(db, allow_changes=True)
                await self._verify_resume(db)
                await self._verify_bindings(db)
                await require_task_stream_terminal(db, self._actor.id, self._binding.project_id,
                                                   terminal=terminal, **kwargs)
                await require_task_stream_physical_lease(db, self._actor.id, self._binding.project_id, **kwargs)
            return
        except scene_ai_context.SceneContextChanged:
            failure = scene_ai_context.SceneContextChanged()
        except VisibilityChangedError:
            failure = VisibilityChangedError()
        except VisibilityBusyError:
            failure = VisibilityBusyError()
        except VisibilityPolicyError as exc:
            if (exc.extra or {}).get("code") == "entity_source_changed":
                failure = VisibilityPolicyError("设定来源已变化，请重新发起", extra={"code": "entity_source_changed"})
            elif (exc.extra or {}).get("code") == "wizard_resume_changed":
                from app.services.wizard_service import WizardResumeChanged

                failure = WizardResumeChanged()
            else:
                failure = CollaborationSendBlocked()
        except Exception:  # noqa: BLE001
            failure = CollaborationSendBlocked()
        raise failure

    async def require_task_write(self, db: AsyncSession) -> None:
        """调用者同一根事务在变更或发布前调用；成功后保留全部写屏障直到提交。"""
        failure: VisibilityPolicyError
        try:
            with db.no_autoflush:
                _reject_pending(db)
                if self._task is None or db.get_nested_transaction() is not None or self._publication_only:
                    raise CollaborationSendBlocked()
                # 协作者和作品作者均须在项目/任务之前按 User ID 顺序取锁。
                await require_authenticated(
                    db, self._actor, write=True, targets=(self._binding.project_owner_id,),
                )
                original_transaction = _transaction_identity(db)
                await ai_dispatch.lock_visibility_write(db, self._binding.project_id)
                await project_lifecycle.lock_active(db, self._binding.project_id)
                await require_visibility_stamp(db, self._stamp)
                if self._scene is not None:
                    await scene_ai_context.require_stamp(db, self._scene)
                await self._verify_task(db, write=True)
                if self._document_claim is not None:
                    from app.services.kb_index_service import require_writing_document_claim

                    await require_writing_document_claim(
                        db, self._document_claim, actor=self._actor, grant_identity=self._binding.grant_identity,
                    )
                if self._embedding_inputs is not None:
                    from app.services.retrieval_service import require_writing_embedding_snapshots

                    await require_writing_embedding_snapshots(
                        db, self._embedding_inputs, actor=self._actor, execution=self._task,
                        selection=self._binding, cipher=self._cipher, allow_private=self._allow_private,
                    )
                if self._chapter is not None:
                    await chapter_claim.require_running_chapter(db, self._chapter)
                await self._verify_entity_sources(db, write=True, allow_changes=True)
                await self._verify_resume(db)
                await self._verify_bindings(db, write=True)
                if _transaction_identity(db) is not original_transaction:
                    raise CollaborationSendBlocked()
            return
        except scene_ai_context.SceneContextChanged:
            await db.rollback()
            failure = scene_ai_context.SceneContextChanged()
        except VisibilityChangedError:
            await db.rollback()
            failure = VisibilityChangedError()
        except VisibilityBusyError:
            await db.rollback()
            failure = VisibilityBusyError()
        except VisibilityPolicyError as exc:
            await db.rollback()
            if (exc.extra or {}).get("code") == "wizard_resume_changed":
                from app.services.wizard_service import WizardResumeChanged

                failure = WizardResumeChanged()
            elif (exc.extra or {}).get("code") == "entity_source_changed":
                failure = VisibilityPolicyError("设定来源已变化，请重新发起", extra={"code": "entity_source_changed"})
            else:
                failure = CollaborationSendBlocked()
        except Exception:  # noqa: BLE001  不向事件或日志传播输入、配置、SQL。
            await db.rollback()
            failure = CollaborationSendBlocked()
        except BaseException:
            await db.rollback()
            raise
        raise failure

    async def _verify(self, db: AsyncSession, *, consume_one_shot: bool) -> None:
        failure: VisibilityPolicyError | None = None
        try:
            if self._publication_only:
                raise CollaborationSendBlocked()
            with db.no_autoflush:
                if consume_one_shot:
                    await ai_dispatch.require_actual_send_lease(db, self._binding.actor_id, self._binding.project_id)
                else:
                    # 普通请求和真实发送不得借用免消费的验证，证明由 preflight 的物理事务提供。
                    await ai_dispatch.require_preflight_lease(db, self._binding.actor_id, self._binding.project_id)
                _reject_pending(db)
                await self._verify_locked(db, preflight=not consume_one_shot)
                if consume_one_shot and self._after_verify is not None:
                    original_transaction = _transaction_identity(db)
                    await self._after_verify()
                    _reject_pending(db)
                    if _transaction_identity(db) is not original_transaction:
                        raise CollaborationSendBlocked()
                    # callback 无 session 参数，但错误闭包仍可能持有该会话。所有锁已由本事务持有，
                    # 再读原 actor/任务/模型，避免同事务中的隐式改动绕过最终发送检查。
                    await require_authenticated_for_send(db, self._actor)
                    await self._verify_locked(db, preflight=False)
                    if _transaction_identity(db) is not original_transaction:
                        raise CollaborationSendBlocked()
            return
        except scene_ai_context.SceneContextChanged:
            failure = scene_ai_context.SceneContextChanged()
        except VisibilityChangedError:
            failure = VisibilityChangedError()
        except VisibilityBusyError:
            failure = VisibilityBusyError()
        except _ChapterValidationFailure:
            failure = VisibilityPolicyError("章节生成状态已变化，已停止发送", extra={
                "code": "chapter_claim_changed",
            })
        except VisibilityPolicyError as exc:
            if (exc.extra or {}).get("code") == "section_confirmation_used":
                failure = VisibilityPolicyError("本次选区确认不可重发，请重新确认", extra={
                    "code": "section_confirmation_used",
                })
            elif (exc.extra or {}).get("code") == "wizard_resume_changed":
                from app.services.wizard_service import WizardResumeChanged

                failure = WizardResumeChanged()
            elif (exc.extra or {}).get("code") == "entity_source_changed":
                failure = VisibilityPolicyError("设定来源已变化，请重新发起", extra={"code": "entity_source_changed"})
            else:
                failure = CollaborationSendBlocked()
        except Exception:  # noqa: BLE001  只返回固定策略错误，不能携带密钥、SQL 或提示词。
            failure = CollaborationSendBlocked()
        # 在 except 作用域之外抛出，避免异常链持有数据库参数或外部 callback 的私有信息。
        raise failure

    async def verify(self, db: AsyncSession) -> None:
        """真实发送共享门禁内验证并执行一次性动作。"""
        await self._verify(db, consume_one_shot=True)

    async def verify_without_one_shot(self, db: AsyncSession) -> None:
        """只接受同 actor/project 的物理预检事务，不构成未来发送的证明。"""
        await self._verify(db, consume_one_shot=False)

    async def prepare_provider(
        self, sessionmaker: async_sessionmaker[AsyncSession], factory: ProviderFactory, *, request_timeout: float,
        usage_kind: str | None = None, usage_chapter_id: int | None = None,
    ) -> BoundProvider:
        """仅从固定绑定构造 provider；不接受客户端 provider、端点或模型名。

        独立短事务内按 User → 项目 → 任务 → 配置锁顺序读取配置快照并结束事务；
        DNS/构造在事务外进行。真正的每次出站仍由 guard 的 lease 重验同一指纹。
        """
        failure: VisibilityPolicyError
        try:
            if not _trusted_factory(factory) or self._publication_only:
                raise CollaborationSendBlocked()
            task = self._task
            chapter_id = usage_chapter_id
            if chapter_id is None:
                if self._chapter is not None:
                    chapter_id = self._chapter.chapter_id
                elif self._analysis_reference is not None:
                    chapter_id = self._analysis_reference.chapter_id
                elif task is not None:
                    chapter_id = task.chapter_id
            async with sessionmaker() as db:
                await self.authorize(db)
                with db.no_autoflush:
                    await _lock_active_project(db, self._binding.project_id)
                    if self._task is not None:
                        await self._verify_task(db)
                    await self._verify_entity_sources(db)
                    await self._verify_resume(db)
                    await self._verify_bindings(db)
                    if type(self._binding) is project_ai_config.ResolvedSharedEmbedding:
                        model_binding = self._binding.model
                        if model_binding is None:
                            raise CollaborationSendBlocked()
                    else:
                        model_binding = self._binding
                    cfg = await db.scalar(select(AIConfig).where(
                        AIConfig.id == model_binding.config_id, AIConfig.user_id == model_binding.config_owner_id,
                        AIConfig.model_type == model_binding.model_type,
                    ))
                    if cfg is None:
                        raise CollaborationSendBlocked()
                    # 复制所需列，决不把 session、ORM 实例或连接锁带入 DNS／模型调用。
                    snapshot = AIConfig(id=cfg.id, instance_key=cfg.instance_key, user_id=cfg.user_id,
                                        provider=cfg.provider, base_url=cfg.base_url,
                                        api_key_enc=cfg.api_key_enc, model=cfg.model, model_type=cfg.model_type,
                                        thinking_level=cfg.thinking_level, context_window_k=cfg.context_window_k,
                                        max_output_k=cfg.max_output_k)
                    accounts = (await db.execute(select(User.id, User.created_at).where(
                        User.id.in_((model_binding.actor_id, model_binding.config_owner_id)),
                    ))).all()
                    account_stamps = {row.id: row.created_at for row in accounts}
                    chapter_created_at = None
                    if chapter_id is not None:
                        chapter_created_at = await db.scalar(select(Chapter.created_at).where(
                            Chapter.id == chapter_id, Chapter.project_id == model_binding.project_id,
                        ))
            selected_factory = ai_dispatch.chapter_provider_factory(factory) if self._chapter is not None else factory
            provider = await ai_config_service.build_provider(
                snapshot, self._cipher, selected_factory, request_timeout, allow_private=self._allow_private,
            )
            if not _trusted_provider(factory, provider, snapshot.provider):
                raise CollaborationSendBlocked()
            texts = None
            if self._embedding_inputs is not None:
                from app.services.retrieval_service import EMBED_TEXT_CHARS

                texts = tuple(item.embed_text[:EMBED_TEXT_CHARS] for item in self._embedding_inputs)
            constrained = _BoundModelProvider(provider, snapshot.model, snapshot.model_type, embedding_inputs=texts)
            if self._chapter is not None:
                protected = ai_dispatch.guard_chapter_provider(
                    constrained, self._stamp, sessionmaker, chapter=self._chapter,
                    authorize=self.authorize, verify=self.verify, verify_preflight=self.verify_without_one_shot,
                )
            else:
                protected = guard_provider(
                    constrained, self._stamp, sessionmaker, authorize=self.authorize, verify=self.verify,
                    verify_preflight=self.verify_without_one_shot,
                )
            kind = usage_kind or ("chapter.analyze" if self._analysis_execution is not None else
                                  task.kind if task is not None else f"{snapshot.model_type}.generate")
            if usage_kind is None and snapshot.model_type == "embedding":
                kind = f"{task.kind}.embedding" if task is not None else "retrieval.embedding"
            protected = ai_usage_recording.meter_provider(
                protected, snapshot, sessionmaker,
                actor_id=model_binding.actor_id, kind=kind, project_id=model_binding.project_id,
                project_owner_id=model_binding.project_owner_id, chapter_id=chapter_id,
                task_id=task.task_id if task is not None else None,
                task_key=task.task_key if task is not None else None,
                actor_created_at=self._actor.editor_created_at or account_stamps.get(model_binding.actor_id),
                payer_created_at=account_stamps.get(model_binding.config_owner_id),
                chapter_created_at=chapter_created_at,
            )
            return BoundProvider(protected, snapshot.model, snapshot.provider, snapshot.thinking_level,
                                 snapshot.context_window_k, snapshot.max_output_k)
        except VisibilityPolicyError as exc:
            if (exc.extra or {}).get("code") == "wizard_resume_changed":
                from app.services.wizard_service import WizardResumeChanged

                failure = WizardResumeChanged()
            elif (exc.extra or {}).get("code") == "entity_source_changed":
                failure = VisibilityPolicyError("设定来源已变化，请重新发起", extra={"code": "entity_source_changed"})
            else:
                failure = CollaborationSendBlocked()
        except Exception:  # noqa: BLE001
            failure = CollaborationSendBlocked()
        raise failure


def publication_execution_matches(guard: object, execution: object) -> bool:
    """引擎签发 / 中央校验帧来源时，来源执行身份是否属于该 guard 的原执行。

    一般须与 ``guard._task`` 逐字相等。单独章节分析在领取后会把 pending 凭据（之后是完成收据）
    写入断点，发布身份随之推进；只接受「原断点 + 本 guard 的 pending 文档」或
    「原断点 + 由同一 pending 文档派生的完成收据」，其余字段（含 step_cursor）不得变化。
    """
    if (type(guard) is not SendGuard or type(execution) is not execution_identity.TaskExecutionIdentity
            or type(guard._task) is not execution_identity.TaskExecutionIdentity):
        return False
    original = guard._task
    if original.kind == "chapter.batch":
        return _batch_publication_matches(guard, execution)
    if original.kind in _WIZARD_KINDS:
        # 向导（E5-c3）：只接受两种已证明形状之一，且发布身份与 guard 原执行逐字相等。
        # 未决 → 完成收据的推进不在此放宽：done 帧由保存后另行构造的仅发布 guard 签发。
        return guard._wizard_stream_shape() is not None and execution == original
    if original.kind == "agent.turn":
        # 智能体轮次：只接受 AgentHandler.stream_guard 构造的仅发布 guard，且发布身份逐字相等。
        return guard._agent_stream_shape() and execution == original
    if execution == original:
        return True
    if (guard._analysis_execution is None or guard._analysis_pending is None
            or guard._analysis_reference is not None or original.kind != "chapter.analyze"):
        return False
    try:
        from app.services import analysis_service

        base = original.checkpoint
        if base is not None and type(base) is not dict:
            return False
        base = dict(base or {})
        if "_analysis_pending" in base or "_analysis_completion" in base:
            return False
        pending = analysis_service._pending_document(guard._analysis_pending)  # noqa: SLF001
        if execution == execution_identity.advance_checkpoint(original, {**base, "_analysis_pending": pending}):
            return True
        current = execution.checkpoint
        receipt = current.get("_analysis_completion") if type(current) is dict else None
        # 收据必须逐键包含同一 pending 文档（缺键不能以 None 冒充，如作者的 grant=None）。
        if (type(receipt) is not dict or not set(pending) <= set(receipt)
                or any(receipt[key] != value for key, value in pending.items())
                or set(current) != {*base, "_analysis_completion"}):
            return False
        return execution == execution_identity.advance_checkpoint(
            original, {**base, "_analysis_completion": receipt},
        )
    except Exception:  # noqa: BLE001
        return False


def _batch_publication_matches(guard: SendGuard, execution: execution_identity.TaskExecutionIdentity) -> bool:
    """批量（E5-c2）：发布身份必须恰为该 guard 所绑定的那一步断点。

    - between：guard 原执行就是发布时断点（逐字相等）；
    - attempt：同一执行，断点与游标恰为本 attempt 写入的状态（与持锁行核对同一判定）；
    - analysis：同一执行，游标即本章、阶段为 analyze、断点中分析引用即本 guard 的收据引用，且无当前章。
    交出旧章 guard、或断点已换 attempt / 换章时一律拒绝；不放宽为 same_execution。
    """
    try:
        shape = guard._batch_stream_shape()
        original = guard._task
        if shape == "between":
            return execution == original
        if shape is None or not execution_identity.same_execution(original, execution):
            return False
        if shape == "attempt":
            return _batch_attempt_state(execution.checkpoint, execution.step_cursor, original, guard._batch_attempt)
        from app.services import analysis_service

        _check_analysis_identity(original, guard._analysis_execution, None, guard._analysis_reference)
        analysis, reference = guard._analysis_execution, guard._analysis_reference
        checkpoint = execution.checkpoint
        items = checkpoint.get("items") if type(checkpoint) is dict else None
        cursor = analysis.batch_cursor
        return bool(type(checkpoint) is dict and checkpoint.get("phase") == "analyze"
                    and checkpoint.get("current") is None and execution.step_cursor == cursor
                    and checkpoint.get("rich_analysis") == reference.as_dict()
                    and analysis_service.capture_analysis_reference(checkpoint["rich_analysis"]) == reference
                    and type(items) is list and cursor < len(items) and type(items[cursor]) is dict
                    and type(items[cursor].get("chapter_id")) is int
                    and items[cursor]["chapter_id"] == reference.chapter_id)
    except Exception:  # noqa: BLE001
        return False


def publication_successor(
    guard: object, execution: object, later_guard: object, later_execution: object,
) -> bool:
    """批量（E5-c2）与智能体轮次帧的来源已被同一执行后来发布的来源取代时，可否改以后者在发送时复核。

    - 批量：两者都是已验证的批量发布 guard、同一 actor/绑定/原执行、后者游标不回退；
    - 智能体轮次：两者都是已验证的智能体仅发布 guard、同一 actor/绑定/可见性戳/cipher/私网开关/原执行、
      step_cursor 均为 0、断点已变化且 round 与 tool_calls_used 不回退。
    其余类型（单章、分析、大纲等）不适用，仍只认原来源或终态证明。调用方另须证明后者由同一 hub 登记、
    且与持锁行逐字一致。
    """
    try:
        if not (type(guard) is SendGuard and type(later_guard) is SendGuard
                and type(execution) is execution_identity.TaskExecutionIdentity
                and type(later_execution) is execution_identity.TaskExecutionIdentity
                and guard._task is not None and later_guard._task is not None
                and guard._task.kind == later_guard._task.kind
                and guard._actor == later_guard._actor and guard._binding == later_guard._binding
                and guard._extra_bindings == later_guard._extra_bindings
                and execution_identity.same_execution(guard._task, later_guard._task)
                and execution_identity.same_execution(execution, later_execution)
                and execution_identity.same_execution(guard._task, execution)
                and publication_execution_matches(guard, execution)
                and publication_execution_matches(later_guard, later_execution)):
            return False
        if guard._task.kind == "chapter.batch":
            return later_execution.step_cursor >= execution.step_cursor
        if guard._task.kind == "agent.turn":
            if (not guard._agent_stream_shape() or not later_guard._agent_stream_shape()
                    or guard._stamp != later_guard._stamp or guard._cipher is not later_guard._cipher
                    or guard._allow_private is not later_guard._allow_private
                    or execution.step_cursor != 0 or later_execution.step_cursor != 0
                    or later_execution == execution):
                return False
            prev_cp = execution.checkpoint if type(execution.checkpoint) is dict else {}
            next_cp = later_execution.checkpoint if type(later_execution.checkpoint) is dict else {}
            prev_round, next_round = prev_cp.get("round", 0), next_cp.get("round", 0)
            prev_used, next_used = prev_cp.get("tool_calls_used", 0), next_cp.get("tool_calls_used", 0)
            return (type(prev_round) is int and type(next_round) is int and next_round >= prev_round
                    and type(prev_used) is int and type(next_used) is int and next_used >= prev_used)
        return False
    except Exception:  # noqa: BLE001
        return False


# 设定类向导：整体替换已有条目（或候选竞争）时保存本身会收紧可见性。大纲保存不改可见性，不适用。
_WIZARD_SUCCESSOR_KINDS = frozenset({"wizard.characters", "wizard.world", "wizard.careers"})


def _same_stamp(first: object, second: object) -> bool:
    if not isinstance(first, VisibilityStamp) or not isinstance(second, VisibilityStamp):
        return False
    if (first.project_id != second.project_id or first.revision != second.revision
            or first.epoch != second.epoch):
        return False
    if first.last_deleted_at is None or second.last_deleted_at is None:
        return first.last_deleted_at is None and second.last_deleted_at is None
    return _same_time(first.last_deleted_at, second.last_deleted_at)


def wizard_successor_eligible(guard: object) -> bool:
    """帧来源是否为设定类向导未决阶段的构建 guard（同执行戳后继的唯一适用对象）。"""
    try:
        return bool(type(guard) is SendGuard and not guard._publication_only
                    and type(guard._task) is execution_identity.TaskExecutionIdentity
                    and guard._task.kind in _WIZARD_SUCCESSOR_KINDS
                    and guard._wizard_stream_shape() == "pending")
    except Exception:  # noqa: BLE001
        return False


def wizard_publication_successor(
    guard: object, execution: object, later_guard: object, later_execution: object, frame_stamp: object,
) -> bool:
    """设定类向导（2026-11-13 同执行戳后继）：保存前产出、在本任务收紧可见性的保存提交后才发出的帧，
    可否改由同一执行保存后发布的 done 仅发布 guard 在发送时复核。纯判定，不读库。

    全部成立才为真：
    - 原 guard 是设定类向导未决阶段的构建 guard，后者是同一执行的完成仅发布 guard，各自与发布身份逐字相等；
    - 同 actor、模型绑定、cipher、私网开关、续写来源对象；续写时后者的 resume_epoch 恰为构建戳 epoch；
    - 完成断点恰为「原断点去掉未决凭据 + 由同一凭据逐键（含类型）派生的完成收据」，游标不变；
    - 新戳与构建戳同作品、同删除代次且不回退；帧投影戳必须是这两者之一（不能是第三方变化后的戳）。
    调用方另须证明后者由同一 hub 登记，并以后者 guard 在帧租约锁内复核当前授权与新戳仍成立。
    """
    try:
        if not (wizard_successor_eligible(guard) and type(later_guard) is SendGuard
                and later_guard._publication_only and later_guard._wizard_stream_shape() == "completed"
                and type(execution) is execution_identity.TaskExecutionIdentity
                and type(later_execution) is execution_identity.TaskExecutionIdentity
                and later_guard._task.kind == guard._task.kind
                and publication_execution_matches(guard, execution)
                and publication_execution_matches(later_guard, later_execution)
                and execution_identity.same_execution(execution, later_execution)
                and later_execution.step_cursor == execution.step_cursor
                and guard._actor == later_guard._actor and guard._binding == later_guard._binding
                and guard._extra_bindings == later_guard._extra_bindings
                and guard._cipher is later_guard._cipher and guard._allow_private is later_guard._allow_private
                and later_guard._resume is guard._resume
                and later_guard._resume_epoch == (guard._stamp.epoch if guard._resume is not None else None)):
            return False
        base = execution.checkpoint
        current = later_execution.checkpoint
        if type(base) is not dict or type(current) is not dict:
            return False
        base = dict(base)
        marker = base.pop("_wizard_pending")
        receipt = current.get("_wizard_completion")
        if (type(marker) is not dict or type(receipt) is not dict
                or set(receipt) != set(marker) | {"result", "result_hash", "stream_hash"}
                or any(type(receipt[key]) is not type(value) or receipt[key] != value
                       for key, value in marker.items())
                or current != {**base, "_wizard_completion": receipt}):
            return False
        old, new = guard._stamp, later_guard._stamp
        if (not isinstance(old, VisibilityStamp) or not isinstance(new, VisibilityStamp)
                or old.project_id != new.project_id or new.revision < old.revision or new.epoch < old.epoch
                or not _same_stamp(replace(new, revision=old.revision, epoch=old.epoch), old)):
            return False
        return _same_stamp(frame_stamp, old) or _same_stamp(frame_stamp, new)
    except Exception:  # noqa: BLE001
        return False


def _check_analysis_identity(
    task: execution_identity.TaskExecutionIdentity, analysis: object, pending: object, reference: object,
) -> None:
    if analysis is None:
        if pending is not None or reference is not None or task.kind == "chapter.analyze":
            raise CollaborationSendBlocked()
        return
    from app.services import analysis_service

    if (not analysis_service._valid_execution(analysis)  # noqa: SLF001
            or type(analysis.task_identity) is not execution_identity.TaskExecutionIdentity
            or analysis.task_identity != task):
        raise CollaborationSendBlocked()
    if task.kind == "chapter.analyze":
        if (type(pending) is not analysis_service.AnalysisPending or pending.execution != analysis
                or not _uuid(pending.nonce) or reference is not None):
            raise CollaborationSendBlocked()
    elif task.kind == "chapter.batch":
        if (pending is not None or type(reference) is not analysis_service.AnalysisReference
                or analysis.batch_cursor is None or analysis.batch_cursor < task.step_cursor):
            raise CollaborationSendBlocked()
        ids = task.input.get("chapter_ids")
        if (type(ids) is not list or analysis.batch_cursor >= len(ids)
                or type(ids[analysis.batch_cursor]) is not int
                or ids[analysis.batch_cursor] != reference.chapter_id
                or analysis_service.capture_analysis_reference(reference.as_dict()) != reference):
            raise CollaborationSendBlocked()
    else:
        raise CollaborationSendBlocked()


def _check_extra_bindings(
    task: execution_identity.TaskExecutionIdentity | None, binding: ModelBinding, extras: tuple[ModelBinding, ...],
) -> None:
    if type(extras) is not tuple or len(extras) > 1:
        raise CollaborationSendBlocked()
    if not extras:
        if task is not None:
            other_key = EMBEDDING_KEY if type(binding) is project_ai_config.ResolvedProjectModel else MODEL_KEY
            if other_key in task.input:
                raise CollaborationSendBlocked()
        return
    extra = extras[0]
    if task is None or type(extra) is type(binding):
        raise CollaborationSendBlocked()
    if type(extra) is project_ai_config.ResolvedSharedEmbedding:
        project_ai_config.serialize_shared_embedding(extra)
    elif type(extra) is project_ai_config.ResolvedProjectModel:
        _checked_binding(extra)
    else:
        raise CollaborationSendBlocked()
    if ((extra.actor_id, extra.project_id, extra.project_owner_id, extra.grant_identity)
            != (binding.actor_id, binding.project_id, binding.project_owner_id, binding.grant_identity)
            or not _matches_task_binding(task.input, extra)):
        raise CollaborationSendBlocked()


_WIZARD_KINDS = frozenset({"wizard.outline", "wizard.characters", "wizard.world", "wizard.careers"})
_WIZARD_MARKER_FIELDS = frozenset({"version", "nonce", "task_key", "execution_key", "queued_at", "actor_id",
                                   "project_id", "kind", "input_hash"})
_WIZARD_RECEIPT_FIELDS = _WIZARD_MARKER_FIELDS | {"result", "result_hash", "stream_hash"}


def _wizard_marker_matches(
    marker: object, task: execution_identity.TaskExecutionIdentity, *, completed: bool,
) -> bool:
    """向导未决凭据 / 完成收据与原执行身份逐项一致（E5-c3 发布形状）；纯判定，不读库。"""
    fields = _WIZARD_RECEIPT_FIELDS if completed else _WIZARD_MARKER_FIELDS
    if type(marker) is not dict or set(marker) != fields:
        return False
    if type(marker["version"]) is not int or marker["version"] != 1:
        return False
    if not all(_uuid(marker[key]) for key in ("nonce", "task_key", "execution_key")):
        return False
    if (marker["task_key"] != task.task_key or marker["execution_key"] != task.execution_key
            or marker["nonce"] in (task.task_key, task.execution_key)
            or marker["queued_at"] != task.queued_at.isoformat()
            or type(marker["actor_id"]) is not int or marker["actor_id"] != task.actor_id
            or type(marker["project_id"]) is not int or marker["project_id"] != task.project_id
            or marker["kind"] != task.kind
            or marker["input_hash"] != hashlib.sha256(task.input_json.encode()).hexdigest()):
        return False
    if not completed:
        return True
    result = marker["result"]
    return (type(result) is dict and result.get("kind") == task.kind.removeprefix("wizard.")
            and type(marker["result_hash"]) is str and type(marker["stream_hash"]) is str)


def _check_resume_identity(task: execution_identity.TaskExecutionIdentity | None, resume: object) -> None:
    if task is None:
        if resume is not None:
            raise CollaborationSendBlocked()
        return
    if not task.kind.startswith("wizard."):
        if resume is not None:
            raise CollaborationSendBlocked()
        return
    raw = task.input.get("_wizard_resume")
    request = task.input.get("request")
    if raw is None and resume is None:
        if isinstance(request, dict) and request.get("continue_from_task_id") is not None:
            raise CollaborationSendBlocked()
        return
    from app.services.wizard_service import ResumeBinding

    if (type(resume) is not ResumeBinding or not task.kind.startswith("wizard.")
            or type(raw) is not dict or type(request) is not dict
            or type(raw.get("version")) is not int or type(raw.get("task_id")) is not int
            or type(request.get("continue_from_task_id")) is not int
            or not _uuid(resume.task_key)
            or raw != {"version": 1, "task_id": resume.task_id, "task_key": resume.task_key,
                       "fingerprint": resume.fingerprint}
            or request.get("continue_from_task_id") != resume.task_id
            or request.get("continue_from_task_key") != resume.task_key
            or (resume.actor_id, resume.project_id, resume.kind, resume.actor_token_version)
            != (task.actor_id, task.project_id, task.kind.removeprefix("wizard."), task.actor_token_version)):
        raise CollaborationSendBlocked()


def _check_document_claim(task: execution_identity.TaskExecutionIdentity | None, claim: object) -> None:
    if task is None or task.kind != "kb.index":
        if claim is not None:
            raise CollaborationSendBlocked()
        return
    request = task.input.get("request")
    if type(request) is not dict or request.get("mode") not in ("document", "memory", "rebuild"):
        raise CollaborationSendBlocked()
    if request.get("mode") != "document":
        if claim is not None:
            raise CollaborationSendBlocked()
        return
    from app.services.kb_index_service import DocumentIndexClaim

    if (type(claim) is not DocumentIndexClaim or claim.phase != "running"
            or not all(_positive(value) for value in (
                claim.task_id, claim.actor_id, claim.project_id, claim.document_id,
            ))
            or not _uuid(claim.task_key) or not _uuid(claim.claim_key)
            or type(request.get("document_id")) is not int or request["document_id"] != claim.document_id
            or type(request.get("project_id")) is not int or request["project_id"] != task.project_id
            or task.resource_key != f"kbdoc:{claim.document_id}"
            or (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.claim_key)
            != (task.task_id, task.task_key, task.actor_id, task.project_id, task.execution_key)
            or not isinstance(claim.queued_at, datetime) or not _same_time(claim.queued_at, task.queued_at)):
        raise CollaborationSendBlocked()


def _check_entity_sources(task: execution_identity.TaskExecutionIdentity | None, sources: object) -> None:
    kinds = {"character.generate", "organization.generate", "career.generate", "relations.parse",
             "relations.generate"}
    if task is None or task.kind not in kinds:
        if sources is not None:
            raise CollaborationSendBlocked()
        return
    from app.services.entity_ai_service import EntitySourceSnapshot

    if type(sources) is not EntitySourceSnapshot or sources.execution != task:
        raise CollaborationSendBlocked()


def _check_embedding_inputs(
    task: execution_identity.TaskExecutionIdentity | None, binding: ModelBinding, snapshots: object,
) -> None:
    if task is None or task.kind != "kb.index":
        if snapshots is not None:
            raise CollaborationSendBlocked()
        return
    from app.services.retrieval_service import (
        EMBED_TEXT_CHARS,
        _require_index_snapshot_identity,
    )

    if type(snapshots) is not tuple or type(binding) is not project_ai_config.ResolvedSharedEmbedding:
        raise CollaborationSendBlocked()
    _require_index_snapshot_identity(task, binding, snapshots)
    texts = tuple(item.embed_text[:EMBED_TEXT_CHARS] for item in snapshots)
    if len(set(texts)) != len(texts):
        raise CollaborationSendBlocked()


def _make(
    actor: AuthenticatedActor, binding: ModelBinding, stamp: VisibilityStamp, *, cipher: SecretCipher,
    allow_private: bool, task: execution_identity.TaskExecutionIdentity | None,
    chapter: chapter_claim.ChapterClaim | None, batch_attempt: BatchAttemptIdentity | None,
    analysis_execution: object, analysis_pending: object, analysis_reference: object,
    extra_bindings: tuple[ModelBinding, ...], resume: object, document_claim: object, entity_sources: object,
    embedding_inputs: tuple[object, ...] | None,
    scene: scene_ai_context.SceneContextStamp | None,
    after_verify: AfterVerify | None,
    http_source_verify: HttpSourceVerify | None = None,
    publication_only: bool = False,
    resume_epoch: int | None = None,
) -> SendGuard:
    try:
        if type(publication_only) is not bool or (publication_only and (
            task is None or task.kind not in ("chapter.batch", "agent.turn", *_WIZARD_KINDS) or after_verify is not None
        )):
            raise CollaborationSendBlocked()
        # 构建戳 epoch 只属于向导续写的保存后发布 guard，且不晚于保存后的新戳；续写的发布 guard 必须携带。
        if resume_epoch is not None and (
            not publication_only or resume is None or type(resume_epoch) is not int or resume_epoch < 0
            or not isinstance(stamp, VisibilityStamp) or resume_epoch > stamp.epoch
        ):
            raise CollaborationSendBlocked()
        if publication_only and resume is not None and resume_epoch is None:
            raise CollaborationSendBlocked()
        _checked_binding(binding)
        actor_id = actor.id
        if (not _positive(actor_id) or actor_id != binding.actor_id or not isinstance(stamp, VisibilityStamp)
                or type(allow_private) is not bool):
            raise CollaborationSendBlocked()
        if stamp.project_id != binding.project_id or (
            scene is not None and (not isinstance(scene, scene_ai_context.SceneContextStamp)
                                   or scene.project_id != binding.project_id or scene.visibility != stamp)
        ):
            raise CollaborationSendBlocked()
        if after_verify is not None and not callable(after_verify):
            raise CollaborationSendBlocked()
        if http_source_verify is not None and (task is not None or not callable(http_source_verify)):
            raise CollaborationSendBlocked()
        if chapter is not None and (task is None or not _matches_chapter_identity(chapter, task, binding, scene)):
            raise CollaborationSendBlocked()
        if batch_attempt is not None and (type(batch_attempt) is not BatchAttemptIdentity
                                          or chapter is None or batch_attempt.claim != chapter):
            raise CollaborationSendBlocked()
        if task is not None:
            _check_analysis_identity(task, analysis_execution, analysis_pending, analysis_reference)
            is_batch_analysis = task.kind == "chapter.batch" and analysis_reference is not None
            if task.kind == "chapter.batch" and ((batch_attempt is not None) == is_batch_analysis) and not (
                # 仅发布证明：批量章与章之间既无 attempt 也无分析引用（E5-c2），且永不可发送。
                publication_only and batch_attempt is None and analysis_execution is None
                and chapter is None and scene is None
            ):
                raise CollaborationSendBlocked()
            if task.kind != "chapter.batch" and batch_attempt is not None:
                raise CollaborationSendBlocked()
        elif any(value is not None for value in (
            analysis_execution, analysis_pending, analysis_reference, resume, document_claim, entity_sources,
        )):
            raise CollaborationSendBlocked()
        if task is not None and task.kind in ("chapter.generate", "chapter.polish", "chapter.revise") \
                and chapter is None:
            raise CollaborationSendBlocked()
        if analysis_execution is not None and chapter is not None:
            raise CollaborationSendBlocked()
        _check_extra_bindings(task, binding, extra_bindings)
        if analysis_execution is not None:
            all_bindings = (binding, *extra_bindings)
            if (analysis_execution.model_binding not in all_bindings
                    or analysis_execution.embedding_selection not in all_bindings):
                raise CollaborationSendBlocked()
        _check_resume_identity(task, resume)
        _check_document_claim(task, document_claim)
        _check_entity_sources(task, entity_sources)
        _check_embedding_inputs(task, binding, embedding_inputs)
        guard = SendGuard(actor, binding, stamp, cipher, allow_private, task, chapter, batch_attempt,
                          analysis_execution, analysis_pending, analysis_reference, extra_bindings, resume,
                          document_claim, entity_sources, embedding_inputs, scene, after_verify, http_source_verify,
                          publication_only, resume_epoch)
        if publication_only and task.kind in _WIZARD_KINDS and guard._wizard_stream_shape() != "completed":
            # 向导仅发布 guard（E5-c3）只能绑定已带本轮完成收据的断点。
            raise CollaborationSendBlocked()
        if publication_only and task.kind == "agent.turn" and not guard._agent_stream_shape():
            raise CollaborationSendBlocked()
        return guard
    except Exception:  # noqa: BLE001
        pass
    raise CollaborationSendBlocked()


def for_http(
    authenticated_db: AsyncSession, actor_id: int, binding: ModelBinding, stamp: VisibilityStamp, *,
    cipher: SecretCipher,
    allow_private: bool = False, scene: scene_ai_context.SceneContextStamp | None = None,
    after_verify: AfterVerify | None = None,
    http_source_verify: HttpSourceVerify | None = None,
) -> SendGuard:
    """只从认证 session 捕获原始 actor，不能从当前数据库重新构造令牌版本。"""
    actor = None
    try:
        actor = authenticated_db.info.get("authenticated_actor")
    except Exception:  # noqa: BLE001
        pass
    if (type(actor) is not AuthenticatedActor or not _positive(actor_id) or actor.id != actor_id
            or not _positive(actor.id)
            or type(actor.token_version) is not int or not 0 <= actor.token_version <= 2**31 - 1):
        raise CollaborationSendBlocked()
    return _make(actor, binding, stamp, cipher=cipher, allow_private=allow_private, task=None, chapter=None,
                 batch_attempt=None, analysis_execution=None, analysis_pending=None, analysis_reference=None,
                 extra_bindings=(), resume=None, document_claim=None, entity_sources=None, embedding_inputs=None,
                 scene=scene, after_verify=after_verify, http_source_verify=http_source_verify)


def for_task(
    task: execution_identity.TaskExecutionIdentity, binding: ModelBinding, stamp: VisibilityStamp, *,
    cipher: SecretCipher,
    allow_private: bool = False, scene: scene_ai_context.SceneContextStamp | None = None,
    chapter: chapter_claim.ChapterClaim | None = None,
    batch_attempt: BatchAttemptIdentity | None = None,
    analysis_execution: object = None, analysis_pending: object = None, analysis_reference: object = None,
    extra_bindings: tuple[ModelBinding, ...] = (), resume: object = None, document_claim: object = None,
    entity_sources: object = None, embedding_inputs: tuple[object, ...] | None = None,
    after_verify: AfterVerify | None = None, publication_only: bool = False, resume_epoch: int | None = None,
) -> SendGuard:
    """接收领取事务冻结的原执行身份；缺少占用证据不对旧任务猜测。

    ``publication_only`` 只供批量章与章之间（E5-c2）、向导保存后 done 帧（E5-c3）与智能体轮次的受控任务流发布证明：
    得到的 guard 不能发送、预检或写入。向导续写的发布 guard 须带 ``resume_epoch``（构建戳 epoch）。

    调用方必须在本执行者成功领取时使用该领取结果建立 guard 并保存，之后不能
    重读 Task 行并以新一轮执行身份重建 guard；当前模块并不实现任务领取协议。
    """
    try:
        if (type(task) is not execution_identity.TaskExecutionIdentity or not _positive(task.task_id)
                or not _positive(task.actor_id) or not _positive(task.project_id)
                or not _uuid(task.task_key) or not _uuid(task.execution_key) or task.execution_key == task.task_key
                or not isinstance(task.queued_at, datetime) or type(task.kind) is not str or not task.kind
                or type(task.step_cursor) is not int or task.step_cursor < 0
                or type(task.actor_token_version) is not int or not 0 <= task.actor_token_version <= 2**31 - 1
                or task.actor_id != binding.actor_id or task.project_id != binding.project_id
                or task.grant_identity != binding.grant_identity
                or type(task.input.get("_task_actor_token_version")) is not int
                or task.input["_task_actor_token_version"] != task.actor_token_version
                or (task.kind == "agent.turn"
                    and (type(task.input.get("_agent_actor_token_version")) is not int
                         or task.input["_agent_actor_token_version"] != task.actor_token_version))
                or (task.kind.startswith("wizard.")
                    and (type(task.input.get("_wizard_actor_token_version")) is not int
                         or task.input["_wizard_actor_token_version"] != task.actor_token_version))
                or not _matches_task_binding(task.input, binding, legacy_owner_index=task.kind == "kb.index")):
            raise CollaborationSendBlocked()
        actor = AuthenticatedActor(task.actor_id, task.actor_token_version)
        return _make(actor, binding, stamp, cipher=cipher, allow_private=allow_private, task=task,
                     chapter=chapter, batch_attempt=batch_attempt, analysis_execution=analysis_execution,
                     analysis_pending=analysis_pending, analysis_reference=analysis_reference,
                     extra_bindings=extra_bindings, resume=resume, document_claim=document_claim,
                     entity_sources=entity_sources, embedding_inputs=embedding_inputs,
                     scene=scene, after_verify=after_verify, publication_only=publication_only,
                     resume_epoch=resume_epoch)
    except Exception:  # noqa: BLE001
        pass
    raise CollaborationSendBlocked()


async def prepare_shared_embedding(
    guard: SendGuard, sessionmaker: async_sessionmaker[AsyncSession], factory: ProviderFactory, *,
    request_timeout: float,
) -> BoundProvider | None:
    """任务侧（大纲 / 实体 / 向导）构造受理冻结的作者共享向量 provider（E6-f 裁决 17）。

    ``prepare_provider`` 把授权变化与普通构造故障（DNS、端点暂不可达、provider 构造异常等）统一为
    ``CollaborationSendBlocked``。与同步检索 ``kb_service._require_unchanged_shared_selection`` 同一口径：
    失败后在新事务内按原执行锁内复核——``require_task_write`` 重验原认证令牌、原 Task 执行身份、原邀请轮次
    与 accepted editor、作者模型授权（``allow_owner_ai``）以及冻结选择本身（``revalidate_shared_embedding``，
    含配置指纹、作者默认向量未换、本地可用性）。全部成立 → 返回 None，调用方只用关键词继续（零向量发送）；
    任一变化 → 原样失败关闭。授权 / 策略类错误（续写来源、设定来源、可见性代次等）不经此降级，直接抛出。
    """
    if (type(guard) is not SendGuard or guard._task is None
            or type(guard._binding) is not project_ai_config.ResolvedSharedEmbedding
            or guard._binding.mode != "vector"):
        raise CollaborationSendBlocked()
    try:
        return await guard.prepare_provider(sessionmaker, factory, request_timeout=request_timeout)
    except VisibilityPolicyError as exc:
        if type(exc) is not CollaborationSendBlocked:
            raise
    async with sessionmaker() as db:
        await guard.require_task_write(db)
        await db.rollback()
    return None
