"""持久化工具循环。每次模型响应先落库，再处理调用；断点重试不重复写入。"""

import asyncio
import hmac
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import DBAPIError

from app.ai.providers import AIProviderError, LLMProvider
from app.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from app.models import AgentChange, AgentConversation, AgentMessage, AIConfig, Task
from app.schemas.agent import ChangeOut
from app.services import ai_config_service, project_access, project_ai_config, project_lifecycle, project_service
from app.services import authorization_service as authz
from app.services.agent import context, history, mcp_tools, transactions
from app.services.agent.external_activity import ExternalActivity, drain
from app.services.agent.tools import get_tool, list_tools
from app.services.agent.tools import tasks as tasks_tool
from app.services.agent.tools.base import ToolCtx
from app.services.ai_dispatch import (
    VisibilityPolicyError,
    VisibilityStamp,
    require_visibility_stamp,
    visibility_sessionmaker,
)
from app.services.authorization_service import AuthenticatedActor
from app.services.project_ai_config import ResolvedProjectModel
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    require_live_worker,
    require_write_execution,
    same_execution,
)
from app.services.task_engine.registry import Event, TaskContext

MAX_TOOL_CALLS = 8
MAX_WRITE_PROPOSALS = 50
AUTO_EXECUTE_LIMIT = 3

if TYPE_CHECKING:
    from app.services.setting_ai_context import AiScope


@dataclass(slots=True)
class _AgentExecutionState:
    current: TaskExecutionIdentity
    publication_guard: tuple[tuple, object] | None = None
    stage_text: str = ""


def _stage(job: "AgentJob", text: str) -> Event:
    if job.execution_state is not None:
        job.execution_state.stage_text = text
    return "stage", {"stage": text}


def _refresh_stage(job: "AgentJob") -> Event:
    """断点已由本执行提交、尚无新事件时重发当前阶段：引擎随之核验并换上最新发布来源。

    文案与 hub 当前阶段相同，作者与订阅者都不会多收 progress 帧，也不触发多余 _flush。
    """
    text = job.execution_state.stage_text if job.execution_state is not None else ""
    return "stage", {"stage": text}


@dataclass(frozen=True)
class AgentJob:
    task_id: int
    user_id: int
    project_id: int
    conversation_id: int
    input_message_id: int
    config: AIConfig
    provider: LLMProvider
    stamp: VisibilityStamp
    task_key: str = field(kw_only=True)
    queued_at: datetime = field(kw_only=True)
    execution_key: str = field(kw_only=True)
    actor_token_version: int | None = field(kw_only=True, default=None)
    execution: TaskExecutionIdentity | None = field(kw_only=True, default=None)
    execution_state: _AgentExecutionState | None = field(kw_only=True, default=None)
    model_snapshot: tuple | None = field(kw_only=True, default=None, repr=False)
    conversation_created_at: datetime | None = field(kw_only=True, default=None, repr=False)
    message_snapshot: tuple | None = field(kw_only=True, default=None, repr=False)
    model_binding: ResolvedProjectModel | None = field(kw_only=True, default=None, repr=False)
    cipher: object = field(kw_only=True, default=None, repr=False)
    allow_private: bool = field(kw_only=True, default=False, repr=False)

    @property
    def task_execution_identity(self) -> TaskExecutionIdentity:
        if not _job_execution_matches(self):
            raise _StaleAgentExecution()
        return self.execution_state.current


class _StaleAgentExecution(ConflictError):
    def __init__(self) -> None:
        super().__init__("智能体任务已变化，请重新发起", extra={"code": "agent_execution_changed"})


def _external_unknown_error() -> ConflictError:
    return ConflictError("外部调用结果未知，请核对", extra={"code": "external_outcome_unknown"})


def model_snapshot(config: AIConfig) -> tuple:
    return tuple(getattr(config, field) for field in (
        "id", "instance_key", "created_at", "user_id", "model_type", "provider", "base_url", "api_key_enc", "model",
        "thinking_level", "context_window_k", "max_output_k",
    ))


def message_snapshot(message: AgentMessage) -> tuple:
    return (
        message.id, message.conversation_id, message.turn_task_id, message.turn_task_key, message.role,
        message.content, json.dumps(message.page_context, ensure_ascii=False, sort_keys=True), message.skill_id,
        json.dumps(message.skill_snapshot, ensure_ascii=False, sort_keys=True), message.created_at,
    )


async def verify_job_resources(db, job: AgentJob) -> None:
    """调用方已持原执行体锁；发事件前复验同会话、原消息与模型配置。"""
    if job.model_snapshot is None or job.message_snapshot is None or job.conversation_created_at is None:
        raise _StaleAgentExecution()
    conversation = await db.scalar(select(AgentConversation).where(
        AgentConversation.id == job.conversation_id, AgentConversation.project_id == job.project_id,
        AgentConversation.user_id == job.user_id,
    ).with_for_update().execution_options(populate_existing=True))
    if conversation is None or conversation.created_at != job.conversation_created_at:
        raise _StaleAgentExecution()
    await history.require_open_history(db, conversation, expected_epoch=job.stamp.epoch)
    message = await db.scalar(select(AgentMessage).where(
        AgentMessage.id == job.input_message_id, AgentMessage.conversation_id == job.conversation_id,
        AgentMessage.turn_task_id == job.task_id, AgentMessage.turn_task_key == job.task_key,
        AgentMessage.role == "user",
    ).with_for_update().execution_options(populate_existing=True))
    if message is None or message_snapshot(message) != job.message_snapshot:
        raise _StaleAgentExecution()
    binding = job.model_binding
    if (type(binding) is not ResolvedProjectModel or job.cipher is None
            or job.execution.input.get("_project_model") != project_ai_config.serialize_project_model(binding)
            or binding.actor_id != job.user_id or binding.project_id != job.project_id
            or binding.grant_identity != _job_grant(job)
            or binding.config_id != job.model_snapshot[0]):
        raise _StaleAgentExecution()
    # 配置归属取自冻结绑定：本人配置属 actor；授权回退（source=owner）属作品作者（E4i-b）。
    await ai_config_service.lock_config_owner(db, binding.config_owner_id, write=False)
    config = await db.scalar(select(AIConfig).where(
        AIConfig.id == job.model_snapshot[0], AIConfig.user_id == binding.config_owner_id,
    ).with_for_update().execution_options(populate_existing=True))
    if (config is None or model_snapshot(config) != job.model_snapshot
            or model_snapshot(job.config) != job.model_snapshot):
        raise _StaleAgentExecution()
    await project_ai_config.revalidate_project_model(
        db, job.model_binding, cipher=job.cipher, allow_private=job.allow_private, for_update=False, lock_config=True,
    )


def _require_auto_external_transport() -> None:
    """自动外部调用只可在实际 tools/call 发送门禁可证明时开放。"""
    raise VisibilityPolicyError("自动外部调用暂不可用，请稍后重试", extra={
        "code": "external_send_guard_unavailable",
    })


async def require_no_unknown_external(db, *, project_id: int, conversation_id: int) -> None:
    """不能靠新轮次或新 call_id 绕过会话内未决的外部操作。"""
    from app.services.agent import service

    rows = await db.execute(select(AgentChange.project_id, AgentChange.kind, AgentChange.status,
                                   AgentChange.result, AgentChange.error, AgentChange.external_worker_key).where(
        AgentChange.conversation_id == conversation_id,
    ))
    private_keys = {"_external_outcome_unknown", "_external_claim_id", "_external_task_key", "_external_execution",
                    "_external_snapshot_mac"}
    for row_project_id, kind, status, result, error, owner in rows:
        marked = isinstance(result, dict) and bool(private_keys.intersection(result))
        invalid_external = kind == "external" and (
            status not in {"proposed", "rejected", "executed", "failed", "undone"}
            or result is not None and not isinstance(result, dict)
            or status == "failed" and (
                error == service.EXTERNAL_INTERRUPTED or not isinstance(result, dict)
                or not isinstance(result.get("error"), str)
            ) or status == "executed" and (
                not isinstance(result, dict) or not isinstance(result.get("text"), str)
            )
        )
        if owner is not None or marked or invalid_external or kind == "external" and row_project_id != project_id:
            raise _external_unknown_error()


@dataclass(frozen=True, slots=True)
class _AutoExternalClaim:
    job: AgentJob
    change_id: int
    change_key: str
    claim_id: str
    call_id: str
    started_at: datetime
    queued_at: str
    execution: TaskExecutionIdentity
    change_snapshot: str
    task_snapshot: str
    snapshot_mac: str
    activity: ExternalActivity | None = None


def _claim_conditions(claim: _AutoExternalClaim):
    job = claim.job
    conditions = (
        AgentChange.id == claim.change_id, AgentChange.change_key == claim.change_key,
        AgentChange.conversation_id == job.conversation_id, AgentChange.project_id == job.project_id,
        AgentChange.turn_task_id == job.task_id, AgentChange.turn_task_key == job.task_key,
        AgentChange.tool_call_id == claim.call_id, AgentChange.kind == "external",
        AgentChange.status == "executing", AgentChange.executed_at == claim.started_at,
        AgentChange.external_worker_key == (claim.activity.worker_key if claim.activity else None),
        AgentChange.result["_external_claim_id"].as_string() == claim.claim_id,
        AgentChange.result["_external_task_key"].as_string() == job.task_key,
        AgentChange.result["_external_snapshot_mac"].as_string() == claim.snapshot_mac,
        AgentChange.result["_external_execution"]["queued_at"].as_string() == claim.queued_at,
        AgentChange.result["_external_execution"]["execution_key"].as_string() == job.execution_key,
        AgentChange.result["_external_execution"]["actor_token_version"].as_integer() == job.actor_token_version,
        exists(select(AgentConversation.id).where(
            AgentConversation.id == job.conversation_id, AgentConversation.user_id == job.user_id,
            AgentConversation.project_id == job.project_id,
        )),
    )
    return (*conditions, claim.activity.active()) if claim.activity is not None else conditions


async def _claim_payload_matches(db, claim: _AutoExternalClaim) -> bool:
    row = await db.scalar(select(AgentChange).where(*_claim_conditions(claim)).with_for_update().execution_options(
        populate_existing=True,
    ))
    if (row is None or mcp_tools.change_snapshot(row) != claim.change_snapshot
            or not mcp_tools.valid_external_claim_result(row.result, automatic=True)):
        return False
    job = claim.job
    proof = row.result.get("_external_execution")
    if (proof["queued_at"] != claim.queued_at or proof["execution_key"] != job.execution_key
            or proof["actor_token_version"] != job.actor_token_version):
        return False
    try:
        mac = mcp_tools.external_snapshot_mac(
            db.info.get("agent_view_hmac_key"), actor_id=job.user_id, token_version=job.actor_token_version,
            project_id=job.project_id, conversation_id=job.conversation_id, task_id=job.task_id,
            task_key=job.task_key, queued_at=job.queued_at, execution_key=job.execution_key,
            change_id=claim.change_id, change_key=claim.change_key, claim_id=claim.claim_id,
            tool_call_id=claim.call_id, started_at=claim.started_at, task_evidence=claim.task_snapshot,
            change_evidence=claim.change_snapshot, worker_key=claim.activity.worker_key if claim.activity else None,
        )
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(mac, claim.snapshot_mac)


def _task_conditions(job: AgentJob, execution: TaskExecutionIdentity):
    return (
        Task.id == job.task_id, Task.statistics_key == job.task_key, Task.queued_at == job.queued_at,
        Task.execution_key == job.execution_key, Task.user_id == job.user_id, Task.project_id == job.project_id,
        Task.kind == "agent.turn", Task.status == "running", Task.worker_key == execution.worker_key,
    )


def _task_actor_matches(data, version: int | None) -> bool:
    return (isinstance(data, dict) and type(version) is int and version >= 0
            and type(data.get("_agent_actor_token_version")) is int
            and type(data.get("_task_actor_token_version")) is int
            and data["_agent_actor_token_version"] == data["_task_actor_token_version"] == version)


def _execution_task_snapshot(execution: TaskExecutionIdentity) -> str:
    return mcp_tools.task_snapshot_values({
        "input": execution.input, "checkpoint": execution.checkpoint, "step_cursor": execution.step_cursor,
        "chapter_id": execution.chapter_id, "retry_of_id": execution.retry_of_id,
        "resource_key": execution.resource_key, "serial_key": execution.serial_key,
    })


def _job_execution_matches(job: AgentJob) -> bool:
    original = job.execution
    current = job.execution_state.current if job.execution_state is not None else None
    return (type(original) is TaskExecutionIdentity and type(current) is TaskExecutionIdentity
            and same_execution(original, current) and current.step_cursor == original.step_cursor
            and original.task_id == job.task_id
            and original.task_key == job.task_key and original.actor_id == job.user_id
            and original.project_id == job.project_id and original.kind == "agent.turn"
            and original.queued_at == (job.queued_at.replace(tzinfo=UTC) if job.queued_at.tzinfo is None
                                       else job.queued_at.astimezone(UTC))
            and original.execution_key == job.execution_key and original.actor_token_version == job.actor_token_version
            and _task_actor_matches(original.input, job.actor_token_version))


async def _settle_external_claim(ctx: TaskContext, claim: _AutoExternalClaim, *, unknown: bool) -> None:
    """系统收尾仅可修改原执行体的原占用；不能读取当前任务身份来替换快照。"""
    from app.services.agent import service

    if not _job_execution_matches(claim.job) or claim.execution != claim.job.execution_state.current:
        return
    async with ctx.sessionmaker() as db:
        await project_lifecycle.lock_record(db, claim.job.project_id)
        locked = await db.execute(update(Task).where(*_task_conditions(claim.job, claim.execution)).values(
            updated_at=Task.updated_at,
        ).execution_options(synchronize_session=False))
        if locked.rowcount != 1:
            await db.rollback()
            return
        try:
            await require_live_worker(db, claim.execution.worker_key)
        except ConflictError:
            return
        current = await db.get(Task, claim.job.task_id, populate_existing=True)
        if (not _task_actor_matches(current.input, claim.job.actor_token_version)
                or mcp_tools.task_snapshot(current) != claim.task_snapshot
                or not await _claim_payload_matches(db, claim)):
            await db.rollback()
            return
        data = (service._unknown_external_result(claim.claim_id, claim.job.task_key) if unknown
                else {"error": service.EXTERNAL_NOT_SENT})
        if unknown:
            data["_external_execution"] = {"queued_at": claim.queued_at, "execution_key": claim.job.execution_key,
                                           "actor_token_version": claim.job.actor_token_version}
            data["_external_snapshot_mac"] = claim.snapshot_mac
        await db.execute(update(AgentChange).where(*_claim_conditions(claim)).values(
            status="failed", error=service.EXTERNAL_INTERRUPTED if unknown else service.EXTERNAL_NOT_SENT,
            result=data, external_worker_key=None,
        ).execution_options(synchronize_session=False))
        if claim.activity is not None:
            await claim.activity.finish(db)
        await db.commit()


async def _confirm_external_result(ctx: TaskContext, claim: _AutoExternalClaim, call: dict, value: dict) -> bool:
    """提交响应丢失时，只认可原执行体内已原子落库的确切工具结果。"""
    job = claim.job
    async with ctx.sessionmaker() as db:
        await _lock_execution(db, job)
        await require_visibility_stamp(db, job.stamp)
        stored = await db.scalar(select(AgentMessage).where(
            AgentMessage.conversation_id == job.conversation_id, AgentMessage.turn_task_id == job.task_id,
            AgentMessage.turn_task_key == job.task_key, AgentMessage.role == "tool",
            AgentMessage.tool_call_id == call["id"], AgentMessage.tool_name == call["name"],
        ))
        remaining = await db.scalar(select(AgentChange.id).where(
            AgentChange.conversation_id == job.conversation_id, AgentChange.turn_task_id == job.task_id,
            AgentChange.tool_call_id == call["id"],
        ))
        return stored is not None and stored.content == context.tool_result(value) and remaining is None


def _canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _job_grant(job: AgentJob):
    """轮次执行体绑定的协作授权（作者为 None），取自已证明的当前执行身份。"""
    return job.execution_state.current.grant_identity


def _collaboration_owner_id(job: AgentJob) -> int | None:
    """协作轮次的作品作者（取自领取时冻结的模型绑定）；作者轮次为 None。绑定与授权不一致时失败关闭。"""
    grant = _job_grant(job)
    binding = getattr(job, "model_binding", None)
    if grant is None:
        if binding is not None and (type(binding) is not ResolvedProjectModel or binding.grant_identity is not None):
            raise _StaleAgentExecution()
        return None
    if (type(binding) is not ResolvedProjectModel or binding.grant_identity != grant
            or binding.actor_id != job.user_id or binding.project_id != job.project_id
            or binding.project_owner_id == job.user_id):
        raise _StaleAgentExecution()
    return binding.project_owner_id


async def job_project(db, job: AgentJob):
    """读取轮次作品：作者沿用严格作者入口；协作轮次按原 grant 要求 accepted editor 的 ai.use（只读，不加锁）。"""
    grant = job.task_execution_identity.grant_identity
    if grant is None:
        return await project_service.get_owned_project(db, job.user_id, job.project_id)
    try:
        with db.no_autoflush:
            project = await project_access.require_project(
                db, job.user_id, job.project_id, "ai.use", grant_identity=grant,
            )
    except (ForbiddenError, NotFoundError):
        raise transactions.AgentAuthorizationChangedError() from None
    if project.user_id != _collaboration_owner_id(job):
        raise _StaleAgentExecution()
    return project


async def _lock_execution(db, job: AgentJob) -> None:
    """在实际写事务中锁定原执行体；不依赖可能已复用的整数任务 ID。"""
    if (not _canonical_uuid(job.task_key) or not _canonical_uuid(job.execution_key)
            or not isinstance(job.queued_at, datetime) or type(job.actor_token_version) is not int
            or job.actor_token_version < 0 or not _job_execution_matches(job)):
        raise _StaleAgentExecution()
    actor = AuthenticatedActor(job.user_id, job.actor_token_version)
    bound = db.info.get("authenticated_actor")
    if bound is not None and bound != actor:
        raise _StaleAgentExecution()
    db.info["authenticated_actor"] = actor
    # 保持 User → 可见性 → 项目 → Task 的既有顺序。no_autoflush 防止在
    # 验证旧执行体前，把调用方已经暂存的消息或提案提前写入数据库。
    # 协作轮次（E4i-b 裁决 16）：作者 User 须与 actor 一起在发送写门禁之前按 ID 升序锁定，
    # 后续工具的 lock_agent_write 再锁作者 User 只是重入，不会与作者发送 lease 交叉等待。
    with db.no_autoflush:
        await require_write_execution(
            db, job.execution_state.current, project_owner_id=_collaboration_owner_id(job),
        )


async def _checkpoint(db, job: AgentJob, values: dict, *, commit: Callable[[], Awaitable[None]] | None = None) -> None:
    from app.services.task_engine.execution_identity import advance_checkpoint

    await _lock_execution(db, job)
    next_identity = advance_checkpoint(job.execution_state.current, dict(values))
    task = await db.get(Task, job.task_id, populate_existing=True)
    task.checkpoint = dict(values)
    await (commit() if commit is not None else db.commit())
    # 不能从重读的当前 Task 补造身份；只认可本次已知写值在原执行上的提交事实。
    async with visibility_sessionmaker(db)() as verify:
        verify.info["authenticated_actor"] = AuthenticatedActor(job.user_id, job.actor_token_version)
        with verify.no_autoflush:
            await require_write_execution(verify, next_identity, project_owner_id=_collaboration_owner_id(job))
    job.execution_state.current = next_identity


async def _result(
    db, job: AgentJob, call: dict, data: dict, *, commit: Callable[[], Awaitable[None]] | None = None,
) -> None:
    await _lock_execution(db, job)
    exists = await db.scalar(
        select(AgentMessage.id).where(
            AgentMessage.conversation_id == job.conversation_id,
            AgentMessage.turn_task_id == job.task_id,
            AgentMessage.turn_task_key == job.task_key,
            AgentMessage.tool_call_id == call["id"],
        )
    )
    if exists is None:
        db.add(
            AgentMessage(
                conversation_id=job.conversation_id,
                turn_task_id=job.task_id,
                turn_task_key=job.task_key,
                role="tool",
                content=context.tool_result(data),
                tool_call_id=call["id"],
                tool_name=call["name"],
            )
        )
    # 即使结果已存在，也要提交已离开保存点的失败审计，不能让重试路径丢失记录。
    if exists is None or commit is not None:
        await (commit() if commit is not None else db.commit())


def _out(change: AgentChange) -> dict:
    # 内部事件保留私有引用证明；唯一公开任务流在出队时结合当前 actor 统一投影。
    return transactions.public_change_payload(ChangeOut.model_validate(change).model_dump(mode="json"))


def _can_auto_execute(change: AgentChange) -> bool:
    # 外部插件调用不可撤销，一律需要确认（免确认工具在 runner 中直接调用，不生成提案）。
    if change.kind == "external":
        return False
    # 整体同步可能修改或删除多条子记录，即使顶层只有一个 small 提案也必须确认。
    if change.tool_name == "update_chapter_plans":
        return False
    if change.tool_name == "update_career" and change.arguments.get("stages") is not None:
        return False
    return change.kind in ("small", "text")


async def _external_call(
    job: AgentJob,
    db,
    call: dict,
    args: dict,
    external: "mcp_tools.Resolved | None",
    sessions: mcp_tools.McpSessions,
    batch: list[AgentChange],
    commit: Callable[[], Awaitable[None]] | None = None,
    *,
    ctx: TaskContext | None = None,
) -> AsyncIterator[Event]:
    """MCP 工具先验证参数；自动调用也必须记录不可重放的执行占用。"""
    if _job_grant(job) is not None:
        # 裁决 2/15：编辑者 V1 不开放 MCP（含自动调用，它绕过 lock_agent_write）；不建提案、不发网。
        await _result(db, job, call, {"error": "工具不存在"}, commit=commit)
        return
    if external is None:
        await _result(db, job, call, {"error": mcp_tools.CHANGED}, commit=commit)
        return
    yield _stage(job, f"调用工具：{external.label}")
    yield "tool_start", {"call_id": call["id"], "name": call["name"], "label": external.label}
    invalid = mcp_tools.validate(external, args)
    if invalid is not None:
        await _result(db, job, call, invalid, commit=commit)
        yield "tool_end", {"call_id": call["id"], "ok": False, "summary": invalid["error"]}
        return
    if external.auto_confirm:
        _require_auto_external_transport()
        if ctx is None:
            raise _external_unknown_error()
        # cleanup_scope 覆盖 claim 提交、真实发送、网络关闭及结果收尾。
        with ctx.task_engine.cleanup_scope(job):
            await _lock_execution(db, job)
            await require_visibility_stamp(db, job.stamp)
            await require_no_unknown_external(db, project_id=job.project_id, conversation_id=job.conversation_id)
            count = await db.scalar(select(func.count(AgentChange.id)).where(
                AgentChange.turn_task_id == job.task_id, AgentChange.turn_task_key == job.task_key,
            ))
            queued = (job.queued_at.replace(tzinfo=UTC) if job.queued_at.tzinfo is None
                      else job.queued_at.astimezone(UTC)).isoformat()
            claim_id, change_key, started_at = str(uuid4()), str(uuid4()), datetime.now(UTC)
            current_execution = job.execution_state.current
            if not current_execution.worker_key:
                raise _StaleAgentExecution()
            change = AgentChange(
                change_key=change_key, conversation_id=job.conversation_id, project_id=job.project_id,
                turn_task_id=job.task_id, turn_task_key=job.task_key, tool_call_id=call["id"],
                tool_name=call["name"], arguments=args, kind="external",
                preview=mcp_tools.preview(external.entry, external, args), seq=(count or 0) + 1,
                status="executing", executed_at=started_at, external_worker_key=current_execution.worker_key,
                result={"_external_claim_id": claim_id, "_external_task_key": job.task_key,
                        "_external_execution": {"queued_at": queued, "execution_key": job.execution_key,
                                                "actor_token_version": job.actor_token_version}},
            )
            db.add(change)
            await db.flush()
            task_snapshot = _execution_task_snapshot(current_execution)
            original_task = await db.get(Task, job.task_id, populate_existing=True)
            if mcp_tools.task_snapshot(original_task) != task_snapshot:
                raise _StaleAgentExecution()
            change_snapshot = mcp_tools.change_snapshot(change)
            send_identity = mcp_tools.McpSendIdentity(
                sessionmaker=ctx.sessionmaker, actor=AuthenticatedActor(job.user_id, job.actor_token_version),
                project_id=job.project_id, conversation_id=job.conversation_id, task_id=job.task_id,
                task_key=job.task_key, queued_at=job.queued_at, execution_key=job.execution_key,
                change_id=change.id, change_key=change_key, claim_id=claim_id,
                tool_call_id=call["id"], started_at=started_at, stamp=job.stamp,
                execution=current_execution, task_snapshot=task_snapshot, change_snapshot=change_snapshot,
                worker_key=current_execution.worker_key,
            )
            mac = mcp_tools.identity_snapshot_mac(db.info.get("agent_view_hmac_key"), send_identity)
            activity = send_identity.activity()
            activity.register(db)
            change.result = {**change.result, "_external_snapshot_mac": mac}
            await db.flush()
            claim = _AutoExternalClaim(job, change.id, change_key, claim_id, call["id"], started_at, queued,
                                       current_execution, change_snapshot, task_snapshot, mac, activity)
            state = mcp_tools.McpSendState()
            value = None
            try:
                # 提交确认前绝不发送；事务结束后才进入网络。
                await (commit() if commit is not None else db.commit())
                value = await mcp_tools.call_data(
                    sessions, external, args, preserve_outcome=True, send_state=state, send_identity=send_identity,
                )
                await _lock_execution(db, job)
                await require_visibility_stamp(db, job.stamp)
                if not await _claim_payload_matches(db, claim):
                    raise _external_unknown_error()
                removed = await db.execute(delete(AgentChange).where(*_claim_conditions(claim)))
                if removed.rowcount != 1:
                    raise _external_unknown_error()
                await activity.finish(db)
                # 移除占用和持久工具结果同事务；成功时不改变公开提案列表。
                await _result(db, job, call, value, commit=commit)
            except BaseException as exc:
                # 事务确认未知、权限变化和取消均不能用现在的任务值冒认原调用。
                # 开始发送后不能继续模型或重放；未发送则记录明确的普通失败。
                try:
                    await drain(db.rollback())
                except Exception:  # noqa: BLE001
                    try:
                        await drain(db.invalidate())
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    await drain(sessions.aclose())
                except Exception:  # noqa: BLE001
                    pass
                confirmed = False
                if state.started and isinstance(value, dict) and not isinstance(exc, asyncio.CancelledError):
                    try:
                        confirmed = await drain(_confirm_external_result(ctx, claim, call, value))
                    except Exception:  # noqa: BLE001
                        pass
                if not confirmed:
                    try:
                        await drain(_settle_external_claim(ctx, claim, unknown=state.started))
                    except Exception:  # noqa: BLE001
                        # 收尾不可证明时保留 executing；原调用退出后只允许按原身份恢复。
                        pass
                    await drain(activity.confirm_exit(ctx.task_engine.lease))
                if confirmed:
                    # 已确证同事务的结果，不需要也不能重复发送。
                    pass
                elif state.started and not isinstance(exc, asyncio.CancelledError):
                    raise _external_unknown_error() from None
                else:
                    raise
        yield (
            "tool_end",
            {"call_id": call["id"], "ok": "error" not in value, "summary": str(value.get("error") or "调用完成")},
        )
        return
    await _lock_execution(db, job)
    count = await db.scalar(select(func.count(AgentChange.id)).where(
        AgentChange.turn_task_id == job.task_id, AgentChange.turn_task_key == job.task_key,
    ))
    if (count or 0) >= MAX_WRITE_PROPOSALS:
        await _result(db, job, call, {"error": "单轮最多 50 项写操作，请分批进行"}, commit=commit)
        return
    change = AgentChange(
        conversation_id=job.conversation_id,
        project_id=job.project_id,
        turn_task_id=job.task_id,
        turn_task_key=job.task_key,
        tool_call_id=call["id"],
        tool_name=call["name"],
        arguments=args,
        kind="external",
        preview=mcp_tools.preview(external.entry, external, args),
        seq=(count or 0) + 1,
    )
    db.add(change)
    await db.flush()
    await db.refresh(change)
    await (commit() if commit is not None else db.commit())
    batch.append(change)
    yield "proposal", {"change": _out(change)}


async def _process_calls(
    job: AgentJob, ctx: TaskContext, checkpoint: dict, sessions: mcp_tools.McpSessions, scope: "AiScope",
) -> AsyncIterator[Event]:
    from app.services.agent import service

    # 裁决 2/15：协作轮次不开放 MCP 工具，即使断点里残留映射也不解析。
    tool_map = (checkpoint.get("mcp_tool_map") or {}) if _job_grant(job) is None else {}

    async with (
        transactions.session(ctx.sessionmaker) as db,
        transactions.audit_scope(db, ctx.sessionmaker) as coordinator,
    ):
        async def commit() -> None:
            await coordinator.commit(db, ctx.sessionmaker)

        async def finish_read() -> None:
            # 仅用于下方明确的查询 / 只读工具出口，不能推广为任意写事务的提交。
            # 已 flush 的 Core 写入不一定留在 ORM 集合中；调用点的只读契约同样必要。
            # expire_on_commit=False 保留 assistant / project / batch 属性，避免 rollback
            # 令它们过期后在事件交付或下一轮工具中触发隐式异步 IO。
            if (db.sync_session.expire_on_commit or db.in_nested_transaction() or authz.holds_gate(db)
                    or db.new or db.dirty or db.deleted or coordinator.pending):
                raise RuntimeError("只能结束无待决写入和审计的 Agent 只读事务")
            if db.in_transaction():
                await db.commit()

        await _lock_execution(db, job)
        await require_visibility_stamp(db, job.stamp)
        # 重试 / 续跑时，停在 executing 的中断外部调用判为失败（不重新调用），否则这一轮会一直等待确认
        await service.recover_external(db, task_id=job.task_id, task_key=job.task_key, engine=ctx.task_engine)
        await require_no_unknown_external(db, project_id=job.project_id, conversation_id=job.conversation_id)
        # 不把占用任务行的事务持有到后续的插件调用或事件交付。
        await commit()
        assistant = await db.scalar(select(AgentMessage).where(
            AgentMessage.id == checkpoint["assistant_message_id"],
            AgentMessage.conversation_id == job.conversation_id, AgentMessage.role == "assistant",
            AgentMessage.turn_task_id == job.task_id, AgentMessage.turn_task_key == job.task_key,
        ))
        if assistant is None:
            raise _StaleAgentExecution()
        calls = assistant.tool_calls or []
        project = await job_project(db, job)
        # 工具写入按轮次执行体绑定的授权（作者为 None）在锁后复核，不按当前成员身份重新取得授权。
        grant = job.task_execution_identity.grant_identity
        tool_ctx = ToolCtx(
            db, project, job.user_id, ctx, scope, audit_coordinator=coordinator, grant=grant,
            # 检索按当前执行轮次记录用量；协作轮次另按冻结文本绑定重验发送授权。
            execution=lambda: job.task_execution_identity,
            text_binding=job.model_binding if grant is not None else None,
        )
        batch = []
        for index, call in enumerate(calls):
            existing = await db.scalar(
                select(AgentMessage.id).where(
                    AgentMessage.conversation_id == job.conversation_id,
                    AgentMessage.turn_task_id == job.task_id,
                    AgentMessage.turn_task_key == job.task_key,
                    AgentMessage.tool_call_id == call["id"],
                )
            )
            if existing is not None:
                continue
            change = await db.scalar(
                select(AgentChange).where(
                    AgentChange.conversation_id == job.conversation_id,
                    AgentChange.turn_task_id == job.task_id,
                    AgentChange.turn_task_key == job.task_key,
                    AgentChange.tool_call_id == call["id"],
                )
            )
            if change is not None:
                batch.append(change)
                continue
            # 进度事件会由引擎在另一事务写库。结束去重查询的 RR 快照，使之后的
            # 写门禁从新事务开始；错误结果、内部工具和外部工具分支均遵守此边界。
            await finish_read()
            if index >= checkpoint.get("allowed_calls", 0):
                await _result(db, job, call, {"error": "本轮最多调用 8 次工具，请直接回答或分批进行"}, commit=commit)
                continue
            tool = get_tool(call["name"])
            try:
                args = json.loads(call["arguments"])
                if not isinstance(args, dict):
                    raise ValueError("参数必须是对象")
            except (ValueError, TypeError):
                await _result(db, job, call, {"error": "工具参数必须是合法的 JSON 对象"}, commit=commit)
                continue
            if tool is None and call["name"] in tool_map:
                async with ctx.sessionmaker() as check_db:
                    await require_visibility_stamp(check_db, job.stamp)
                external = await mcp_tools.resolve(
                    db, ctx.settings, job.user_id, job.conversation_id, tool_map, call["name"]
                )
                await finish_read()
                async for event in _external_call(job, db, call, args, external, sessions, batch, commit, ctx=ctx):
                    yield event
                continue
            if tool is None:
                await _result(db, job, call, {"error": "工具不存在"}, commit=commit)
                continue
            yield _stage(job, f"调用工具：{tool.name}")
            yield "tool_start", {"call_id": call["id"], "name": tool.name, "label": tool.description}
            if tool.kind == "read":
                try:
                    value = await tool.run(tool_ctx, args)
                except VisibilityPolicyError:
                    raise
                except AppError as exc:
                    value = {"error": exc.detail}
                await finish_read()
                await _result(db, job, call, value, commit=commit)
                yield (
                    "tool_end",
                    {
                        "call_id": call["id"],
                        "ok": "error" not in value,
                        "summary": str(value.get("error") or "读取完成"),
                    },
                )
                continue
            await _lock_execution(db, job)
            count = await db.scalar(select(func.count(AgentChange.id)).where(
                AgentChange.turn_task_id == job.task_id, AgentChange.turn_task_key == job.task_key,
            ))
            if (count or 0) >= MAX_WRITE_PROPOSALS:
                await _result(db, job, call, {"error": "单轮最多 50 项写操作，请分批进行"}, commit=commit)
                continue
            preview = await tool.preview(tool_ctx, args)
            origin = None
            change_key = str(uuid4())
            if "error" not in preview and tool.name == "start_task":
                try:
                    # 受控受理任务在提案时签发来源证明（原轮次 UUID/入队代次、真实 actor 令牌版本、
                    # 提案身份与章节实例）；确认时只承认该证明，不用当前数据补签。
                    origin = await tasks_tool.sign_origin(
                        db, actor_id=job.user_id, token_version=job.actor_token_version,
                        project_id=job.project_id, conversation_id=job.conversation_id, turn_task_id=job.task_id,
                        turn_task_key=job.task_key, queued_at=job.queued_at, change_key=change_key,
                        tool_call_id=call["id"], arguments=args,
                    )
                except AppError as exc:
                    preview = {"error": exc.detail}
            if "error" in preview:
                await _result(db, job, call, preview, commit=commit)
                yield "tool_end", {"call_id": call["id"], "ok": False, "summary": str(preview["error"])}
                continue
            change = AgentChange(
                conversation_id=job.conversation_id,
                project_id=job.project_id,
                turn_task_id=job.task_id,
                turn_task_key=job.task_key,
                change_key=change_key,
                tool_call_id=call["id"],
                tool_name=tool.name,
                arguments=args,
                kind=tool.kind,
                preview=preview,
                result=origin,
                seq=(count or 0) + 1,
            )
            db.add(change)
            await db.flush()
            await db.refresh(change)
            await commit()
            batch.append(change)
            yield "proposal", {"change": _out(change)}
        # existing 消息 / 提案的 continue 可能留下只读快照。先结束它，再以原执行
        # 身份取得写门禁；自动执行判定与随后 checkpoint 共享这个受保护的事务。
        await finish_read()
        await _lock_execution(db, job)
        pending = [c for c in batch if c.status == "proposed"]
        conversation = await db.get(AgentConversation, job.conversation_id, populate_existing=True)
        total = await db.scalar(select(func.count(AgentChange.id)).where(
            AgentChange.turn_task_id == job.task_id, AgentChange.turn_task_key == job.task_key,
        ))
        auto = (
            conversation.auto_execute
            and pending
            and not checkpoint.get("auto_execute_used")
            and total == len(pending)
            and len(pending) <= AUTO_EXECUTE_LIMIT
            and all(_can_auto_execute(c) for c in pending)
        )
        if auto:
            checkpoint["auto_execute_used"] = True
            await _checkpoint(db, job, checkpoint, commit=commit)
            yield _refresh_stage(job)
            for change in pending:
                try:
                    await _lock_execution(db, job)
                    original_key = change.change_key
                    change = await db.scalar(select(AgentChange).where(
                        AgentChange.id == change.id, AgentChange.change_key == original_key,
                        AgentChange.conversation_id == job.conversation_id, AgentChange.project_id == job.project_id,
                        AgentChange.turn_task_id == job.task_id, AgentChange.turn_task_key == job.task_key,
                        AgentChange.status == "proposed",
                    ).with_for_update().execution_options(populate_existing=True))
                    if change is None:
                        raise _StaleAgentExecution()
                    await service.execute_change(
                        db, ctx, change, user_id=job.user_id, scope=scope, audit_coordinator=coordinator,
                    )
                except VisibilityPolicyError:
                    raise
                except ConflictError as exc:
                    if not exc.extra or exc.extra.get("code") != "preview_changed":
                        raise
                    # 数据在提案到自动执行之间变化：保留最新预览并等待明确确认。
                    # execute_change 已提交新预览，异常中的投影又开启了只读快照。
                    await finish_read()
                    break
                await db.flush()
                await db.refresh(change)
                await commit()
                yield "executed", {"change": _out(change)}
        pending = [c.id for c in batch if c.status in ("proposed", "executing")]
        checkpoint["pending_change_ids"] = pending
        for change in batch:
            if change.id not in pending:
                await _result(
                    db,
                    job,
                    {"id": change.tool_call_id, "name": change.tool_name},
                    context.change_result_for_model(change),
                    commit=commit,
                )
                yield (
                    "tool_end",
                    {
                        "call_id": change.tool_call_id,
                        "ok": change.status == "executed",
                        "summary": str((change.result or {}).get("message") or change.error or "修改已执行"),
                    },
                )
        if pending:
            await _checkpoint(db, job, checkpoint, commit=commit)
            yield _stage(job, f"等待确认 {len(pending)} 项修改")
            yield "paused", {"reason": "confirm", "change_ids": pending}
            return
        checkpoint["processed_assistant_id"] = assistant.id
        checkpoint["pending_change_ids"] = []
        await _checkpoint(db, job, checkpoint, commit=commit)
        yield _refresh_stage(job)


async def _save_log(
    job: AgentJob,
    ctx: TaskContext,
    *,
    messages: list,
    prompt: dict,
    round_no: int,
    text: str,
    calls: list,
    usage: dict,
    started: float,
    status: str,
    error: str = "",
) -> None:
    from app.services import generation_log_service

    async with transactions.session(ctx.sessionmaker) as db:
        await _lock_execution(db, job)
        await verify_job_resources(db, job)
        log = await generation_log_service.write_log(
            db,
            project_id=job.project_id,
            actor_user_id=job.user_id,
            chapter_id=None,
            kind="agent.turn",
            task_id=job.task_id,
            ai_config_id=job.config.id,
            provider=job.config.provider,
            model=job.config.model,
            params={
                "conversation_id": job.conversation_id,
                "round": round_no,
                "tool_calls": calls,
                "prompt": prompt,
            },
            messages=messages,
            status=status,
            error=error,
            output_chars=len(text),
            duration_ms=int((time.monotonic() - started) * 1000),
            model_binding=job.model_binding,
        )
        log.prompt_tokens = usage.get("input_tokens")
        log.completion_tokens = usage.get("output_tokens")
        await db.commit()


async def _run_agent(job: AgentJob, ctx: TaskContext) -> AsyncIterator[Event]:
    """一次运行内复用 MCP 连接；运行结束、暂停或异常时统一关闭。"""
    sessions = mcp_tools.McpSessions(
        ctx.mcp_connector, ctx.cipher, ctx.settings.allow_private_ai_endpoints, settings=ctx.settings,
    )
    try:
        async with aclosing(_turn(job, ctx, sessions)) as events:
            async for event in events:
                yield event
    finally:
        await sessions.aclose()


async def _turn(job: AgentJob, ctx: TaskContext, sessions: mcp_tools.McpSessions) -> AsyncIterator[Event]:
    async with transactions.session(ctx.sessionmaker) as db:
        await _lock_execution(db, job)
        await require_visibility_stamp(db, job.stamp)
        await require_no_unknown_external(db, project_id=job.project_id, conversation_id=job.conversation_id)
        user_message = await db.get(AgentMessage, job.input_message_id)
        if (user_message is None or user_message.conversation_id != job.conversation_id
                or user_message.role != "user" or user_message.turn_task_id != job.task_id
                or user_message.turn_task_key != job.task_key):
            raise ConflictError("本轮原始消息不可用，请重新发起")
        scope = await history.build_tool_scope(db, job.stamp, user_message.content)
        task = await db.get(Task, job.task_id)
        checkpoint = dict(task.checkpoint or {})
        if "mcp_tool_map" not in checkpoint:
            # 本轮 MCP 映射表：续跑 / 重试沿用，确认期间修改配置不会让名称漂移
            conversation = await db.get(AgentConversation, job.conversation_id)
            # 裁决 2：编辑者 V1 不开放 MCP 工具（外部效果不可撤销）。
            checkpoint["mcp_tool_map"] = (
                await mcp_tools.build_tool_map(db, ctx.settings, job.user_id, conversation)
                if _job_grant(job) is None else {}
            )
            await _checkpoint(db, job, checkpoint)
        history_text = "".join(
            await db.scalars(
                select(AgentMessage.content)
                .where(
                    AgentMessage.turn_task_id == job.task_id,
                    AgentMessage.turn_task_key == job.task_key,
                    AgentMessage.role == "assistant",
                )
                .order_by(AgentMessage.id)
            )
        )
    yield "reset", {}
    if history_text:
        yield "delta", {"text": history_text}
    if checkpoint.get("finished_message_id"):
        yield "done", {"message_id": checkpoint["finished_message_id"], "usage": checkpoint.get("usage", {})}
        return
    while True:
        if checkpoint.get("assistant_message_id") != checkpoint.get("processed_assistant_id"):
            paused = False
            async with aclosing(_process_calls(job, ctx, checkpoint, sessions, scope)) as events:
                async for event in events:
                    if event[0] == "paused":
                        paused = True
                    yield event
            if paused:
                return
        context_error = None
        async with transactions.session(ctx.sessionmaker) as db:
            await _lock_execution(db, job)
            await require_no_unknown_external(db, project_id=job.project_id, conversation_id=job.conversation_id)
            # 上下文压缩可能访问模型；不可持有任务或认证写锁跨网络调用。
            await db.commit()
            conversation = await db.get(AgentConversation, job.conversation_id)
            project = await job_project(db, job)
            user_message = await db.get(AgentMessage, job.input_message_id)
            try:
                messages, prompt = await context.build_messages(
                    db, conversation, project, job.config, job.provider, user_message.page_context, job.task_id,
                    scope=scope, execution=job,
                )
            except (AppError, AIProviderError) as exc:
                context_error = {"detail": str(exc)}
            else:
                external_specs = [] if _job_grant(job) is not None else (
                    await mcp_tools.tool_specs(
                        db,
                        ctx.settings,
                        job.user_id,
                        job.conversation_id,
                        checkpoint.get("mcp_tool_map") or {},
                        job.config.provider,
                    )
                )
                provider = await context.guard_agent_provider(
                    db, conversation, project, job.config, job.provider, execution=job,
                )
        if context_error is not None:
            yield "error", context_error
            return
        tools = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in list_tools()]
        tools.extend(external_specs)
        used = checkpoint.get("tool_calls_used", 0)
        choice = "none" if used >= MAX_TOOL_CALLS else "auto"
        round_no = checkpoint.get("round", 0) + 1
        pieces, calls, usage = [], [], {}
        started = time.monotonic()
        try:
            async with aclosing(
                provider.chat_tools_stream(
                    messages,
                    job.config.model,
                    tools,
                    tool_choice=choice,
                    max_tokens=job.config.max_output_k * 1000 if job.config.max_output_k else None,
                    thinking=job.config.thinking_level,
                )
            ) as stream:
                async for name, data in stream:
                    if name == "text":
                        pieces.append(data["text"])
                        yield "delta", {"text": data["text"]}
                    elif name == "tool_call":
                        calls.append(dict(data))
                    elif name == "usage":
                        usage.update(data)
        except asyncio.CancelledError:
            try:
                await _save_log(
                    job,
                    ctx,
                    messages=messages,
                    prompt=prompt,
                    round_no=round_no,
                    text="".join(pieces),
                    calls=calls,
                    usage=usage,
                    started=started,
                    status="cancelled",
                )
            except _StaleAgentExecution:
                # 原任务已结束或被替换，保留取消语义而不把日志写给新任务。
                pass
            raise
        except (AIProviderError, AppError) as exc:
            await _save_log(
                job,
                ctx,
                messages=[] if isinstance(exc, VisibilityPolicyError) else messages,
                prompt=prompt,
                round_no=round_no,
                text="".join(pieces),
                calls=calls,
                usage=usage,
                started=started,
                status="error",
                error=str(exc),
            )
            yield "error", {"detail": str(exc)}
            return
        text = "".join(pieces)
        await _save_log(
            job,
            ctx,
            messages=messages,
            prompt=prompt,
            round_no=round_no,
            text=text,
            calls=calls,
            usage=usage,
            started=started,
            status="success",
        )
        async with transactions.session(ctx.sessionmaker) as db:
            await _lock_execution(db, job)
            assistant = AgentMessage(
                conversation_id=job.conversation_id,
                turn_task_id=job.task_id,
                turn_task_key=job.task_key,
                role="assistant",
                content=text,
                tool_calls=calls or None,
                usage=usage,
            )
            db.add(assistant)
            await db.flush()
            allowed = max(0, min(len(calls), MAX_TOOL_CALLS - used))
            cumulative = dict(checkpoint.get("usage") or {})
            for key in ("input_tokens", "output_tokens"):
                cumulative[key] = cumulative.get(key, 0) + int(usage.get(key, 0))
            checkpoint.update(
                round=round_no,
                assistant_message_id=assistant.id,
                allowed_calls=allowed,
                tool_calls_used=used + allowed,
                usage=cumulative,
            )
            if not calls:
                checkpoint["finished_message_id"] = assistant.id
            await _checkpoint(db, job, checkpoint)
        if not calls:
            yield "done", {"message_id": assistant.id, "usage": cumulative}
            return
        if choice == "none":
            # 不遵守 tool_choice 的模型也不能导致无限循环；补全每个协议结果再明确结束。
            async with transactions.session(ctx.sessionmaker) as db:
                for call in calls:
                    await _result(db, job, call, {"error": "已达到工具调用上限，请直接回答"})
                await _lock_execution(db, job)
                final = AgentMessage(
                    conversation_id=job.conversation_id,
                    turn_task_id=job.task_id,
                    turn_task_key=job.task_key,
                    role="assistant",
                    content="本轮已达到工具调用上限，请分批继续。",
                )
                db.add(final)
                await db.flush()
                checkpoint["finished_message_id"] = final.id
                checkpoint["processed_assistant_id"] = assistant.id
                await _checkpoint(db, job, checkpoint)
            yield "delta", {"text": final.content}
            yield "done", {"message_id": final.id, "usage": cumulative}
            return
        yield _refresh_stage(job)


async def run_agent(job: AgentJob, ctx: TaskContext) -> AsyncIterator[Event]:
    from app.services.generation_log_service import GenerationLogWriteError

    try:
        async with aclosing(_run_agent(job, ctx)) as events:
            async for event in events:
                yield event
    except GenerationLogWriteError as exc:
        if not exc.serialization_conflict:
            raise
        yield "error", {"detail": transactions.CONCURRENT_CHANGE}
    except DBAPIError as exc:
        if not transactions.is_serialization_error(exc):
            # 原始 DBAPIError 可能携带正文或 SQL 参数，不能交给任务引擎的 traceback 日志。
            yield "error", {"detail": "服务器内部错误"}
            return
        yield "error", {"detail": transactions.CONCURRENT_CHANGE}
    except ConflictError as exc:
        yield "error", {"detail": exc.detail}
