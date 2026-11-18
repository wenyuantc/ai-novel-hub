"""原生工具历史、上下文预算与不可信数据隔离。"""

import asyncio
import json
import time
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.ai import skill_inputs
from app.ai.prompts import format_project_info
from app.ai.prompts.agent import FORCED_SKILL_HEADER, SAFETY_RULES, SKILLS_CATALOG_LIMIT, SKILLS_CATALOG_MORE
from app.ai.prompts.registry import prompt_meta, render
from app.ai.providers import LLMProvider
from app.core.errors import BadRequestError
from app.core.security import SecretCipher
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    AIConfig,
    Chapter,
    Character,
    Outline,
    Project,
    Task,
    WritingStyle,
)
from app.schemas.project import ProjectOut
from app.services import (
    ai_dispatch,
    project_ai_config,
    project_lifecycle,
    project_service,
    prompt_service,
    setting_ai_visibility,
    skill_service,
)
from app.services.agent import history
from app.services.ai_dispatch import (
    VisibilityPolicyError,
    _GuardedProvider,
    authorize_send_actor,
    guard_provider,
    require_visibility_stamp,
    visibility_sessionmaker,
)
from app.services.ai_usage_recording import meter_provider
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import task_grant_identity
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    matches_running,
    require_write_execution,
)

if TYPE_CHECKING:
    from app.services.setting_ai_context import AiScope

RESULT_LIMIT = 8000
TRUNCATED = "内容过长已截断，请用分页参数读取"
_PAGE_SECTIONS = frozenset({
    "overview", "wizard", "world", "characters", "organizations", "relationships", "careers", "setting-fields",
    "setting-categories", "foreshadows", "knowledge", "notes", "mentions", "memories", "outlines", "planning",
    "chapters", "read",
})


class SummaryExecutionChanged(VisibilityPolicyError):
    """旧摘要或不完整任务不能续签为当前执行体。"""

    def __init__(self) -> None:
        super().__init__("智能体任务已变化，摘要已停止", extra={"code": "agent_execution_changed"})


class AgentHistoryUnverifiable(VisibilityPolicyError):
    """旧摘要没有可验证的私人风格来源，不能继续发送。"""

    def __init__(self) -> None:
        super().__init__("旧会话摘要无法验证来源，请新建会话", extra={"code": "agent_history_unverifiable"})


@dataclass(frozen=True, slots=True, repr=False)
class _HistoryProjectStyle:
    project_id: int
    owner_id: int
    actor_id: int
    style_id: int | None
    name: str | None
    description: str | None
    is_preset: bool | None


async def _history_project_style(db: AsyncSession, project_id: int, conversation_id: int, actor_id: int):
    """同一 SQL 快照核对会话真实 actor、作品和当前可见风格。"""
    if any(type(value) is not int or value <= 0 for value in (project_id, conversation_id, actor_id)):
        raise AgentHistoryUnverifiable()
    with db.no_autoflush:
        row = (await db.execute(select(
            Project.user_id.label("owner_id"), Project.default_style_id,
            WritingStyle.id.label("style_id"), WritingStyle.user_id.label("style_owner_id"),
            WritingStyle.name, WritingStyle.description,
        ).select_from(Project).join(AgentConversation, and_(
            AgentConversation.project_id == Project.id, AgentConversation.id == conversation_id,
            AgentConversation.user_id == actor_id,
        )).outerjoin(WritingStyle, WritingStyle.id == Project.default_style_id).where(
            Project.id == project_id, *project_lifecycle.active_conditions(),
        ))).one_or_none()
    if row is None:
        raise AgentHistoryUnverifiable()
    visible = (row.style_id is not None and row.style_id == row.default_style_id
               and (row.style_owner_id is None or row.style_owner_id == actor_id))
    return _HistoryProjectStyle(
        project_id=project_id, owner_id=row.owner_id, actor_id=actor_id,
        style_id=row.style_id if visible else None, name=row.name if visible else None,
        description=row.description if visible else None,
        is_preset=(row.style_owner_id is None) if visible else None,
    )


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _same_time(first: datetime, second: datetime) -> bool:
    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    return utc(first) == utc(second)


_CONFIG_FIELDS = (
    "user_id", "model_type", "provider", "base_url", "api_key_enc", "model", "thinking_level",
    "context_window_k", "max_output_k",
)


@dataclass(frozen=True, slots=True, repr=False)
class _SummaryExecution:
    task_id: int
    task_key: str
    queued_at: datetime
    execution_key: str
    actor_id: int
    actor_token_version: int
    project_id: int
    conversation_id: int
    message_id: int
    config_id: int
    config_fields: tuple[Any, ...]
    task_execution: TaskExecutionIdentity
    model_binding: project_ai_config.ResolvedProjectModel
    cipher: SecretCipher
    allow_private: bool
    summary: str
    summarized_until: int | None
    history_epoch: int
    conversation_created_at: datetime
    messages: tuple[tuple[Any, ...], ...]
    project_style: _HistoryProjectStyle
    # E4i-b：协作轮次的作者与原授权轮次（作者轮次 owner == actor、grant 为 None）；摘要来源随之区分（裁决 13）。
    project_owner_id: int = 0
    grant_identity: tuple[str, int] | None = None
    summary_source: str | None = None


def summary_source_for(grant: tuple[str, int] | None) -> str | None:
    """摘要来源标记：作者为 None；协作者绑定生成该摘要的邀请轮次，换轮次（撤权后重邀）即不再承认。"""
    if grant is None:
        return None
    return f"collaborator:{grant[0]}:{grant[1]}"


def _message_snapshot(message: AgentMessage) -> tuple[Any, ...]:
    # 每项仅供进程内比对，不输出；深拷贝防止会话内 JSON 原地变更。
    return (
        message.id, message.conversation_id, message.turn_task_id, message.turn_task_key, message.role,
        message.content, deepcopy(message.tool_calls), message.tool_call_id, message.tool_name,
        deepcopy(message.page_context), deepcopy(message.skill_snapshot), message.created_at,
    )


def _summary_execution(execution, task_id, conversation, project, cfg, older, project_style) -> _SummaryExecution:
    try:
        task_key, execution_key, queued_at = execution.task_key, execution.execution_key, execution.queued_at
        actor_token_version = execution.actor_token_version
        original = execution.task_execution_identity
        binding = execution.model_binding
        cipher, allow_private = execution.cipher, execution.allow_private
        ids = (
            execution.task_id, execution.user_id, execution.project_id, execution.conversation_id,
            execution.input_message_id, cfg.id,
        )
        if (any(type(value) is not int or value <= 0 for value in ids)
                or not _valid_uuid(task_key) or not _valid_uuid(execution_key) or task_key == execution_key
                or type(actor_token_version) is not int or not 0 <= actor_token_version <= 2**31 - 1
                or not isinstance(queued_at, datetime) or not isinstance(conversation.created_at, datetime)
                or task_id != execution.task_id or execution.user_id != conversation.user_id
                or execution.project_id != project.id or execution.project_id != conversation.project_id
                or execution.conversation_id != conversation.id
                or type(original) is not TaskExecutionIdentity
                or (original.task_id, original.task_key, original.actor_id, original.project_id, original.kind,
                    original.execution_key, original.actor_token_version)
                != (execution.task_id, task_key, execution.user_id, execution.project_id, "agent.turn",
                    execution_key, actor_token_version)
                or not _same_time(original.queued_at, queued_at)
                or type(binding) is not project_ai_config.ResolvedProjectModel
                or type(cipher) is not SecretCipher or type(allow_private) is not bool
                or binding.actor_id != execution.user_id or binding.project_id != execution.project_id
                or binding.project_owner_id != project.user_id
                # 作者轮次不带授权、只能用本人配置；协作轮次绑定原授权，配置属本人或（授权回退）作品作者。
                or (project.user_id == execution.user_id) != (original.grant_identity is None)
                or binding.grant_identity != original.grant_identity
                or binding.config_owner_id != (execution.user_id if binding.source == "actor" else project.user_id)
                or (original.grant_identity is None and binding.source != "actor")
                or cfg.user_id != binding.config_owner_id or binding.model_type != "text"
                or binding.config_id != cfg.id or binding.config_instance_key != cfg.instance_key
                or project_ai_config._fingerprint(cfg) != binding.config_fingerprint):  # noqa: SLF001
            raise ValueError
        selected = project_ai_config.deserialize_project_model(
            original.input.get("_project_model", project_ai_config.PROJECT_MODEL_ABSENT),
            actor_id=execution.user_id, project_id=project.id, project_owner_id=project.user_id,
        )
        if selected != binding:
            raise ValueError
        return _SummaryExecution(
            task_id=execution.task_id, task_key=task_key, queued_at=queued_at, execution_key=execution_key,
            actor_id=execution.user_id, actor_token_version=actor_token_version,
            project_id=project.id, conversation_id=conversation.id,
            message_id=execution.input_message_id, config_id=cfg.id,
            config_fields=tuple(getattr(cfg, field) for field in _CONFIG_FIELDS),
            task_execution=original, model_binding=binding, cipher=cipher, allow_private=allow_private,
            summary=conversation.summary, summarized_until=conversation.summarized_until,
            history_epoch=conversation.ai_history_epoch, conversation_created_at=conversation.created_at,
            messages=tuple(_message_snapshot(message) for message in older),
            project_style=project_style,
            project_owner_id=project.user_id, grant_identity=original.grant_identity,
            summary_source=conversation.summary_source,
        )
    except Exception:  # noqa: BLE001  缺失原绑定、身份或配置均不能续签。
        pass
    raise SummaryExecutionChanged()


async def _check_summary_execution(db: AsyncSession, expected: _SummaryExecution, *, write: bool) -> None:
    """调用方已按 User → 发送门禁取锁，再按项目 → 任务 → 会话 → 消息 → 配置验证。"""
    with db.no_autoflush:
        statement = select(Project.id).where(
            Project.id == expected.project_id, Project.user_id == expected.project_owner_id,
            *project_lifecycle.active_conditions(),
        )
        if db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=not write)
        if await db.scalar(statement) is None:
            raise SummaryExecutionChanged()
        if await _history_project_style(
            db, expected.project_id, expected.conversation_id, expected.actor_id,
        ) != expected.project_style:
            raise SummaryExecutionChanged()
        statement = select(Task).where(
            Task.id == expected.task_id, Task.statistics_key == expected.task_key,
            Task.queued_at == expected.queued_at, Task.execution_key == expected.execution_key,
            Task.user_id == expected.actor_id, Task.project_id == expected.project_id,
            Task.kind == "agent.turn", Task.status == "running", Task.admin_cancel_operation_id.is_(None),
        )
        if db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=not write)
        task = await db.scalar(statement.execution_options(populate_existing=True))
        if not matches_running(expected.task_execution, task):
            raise SummaryExecutionChanged()
        task_input = task.input
        request = task_input.get("request") if isinstance(task_input, dict) else None
        if (not isinstance(task_input, dict)
                or ("_collaboration_grant" in task_input) != (expected.grant_identity is not None)
                or task_grant_identity(task_input) != expected.grant_identity
                or type(task_input.get("_task_actor_token_version")) is not int
                or task_input["_task_actor_token_version"] != expected.actor_token_version
                or type(task_input.get("_agent_actor_token_version")) is not int
                or task_input["_agent_actor_token_version"] != expected.actor_token_version
                or not isinstance(request, dict) or type(request.get("conversation_id")) is not int
                or request["conversation_id"] != expected.conversation_id
                or type(request.get("project_id")) is not int or request["project_id"] != expected.project_id
                or type(task_input.get("history_epoch")) is not int
                or task_input["history_epoch"] != expected.history_epoch
                or type(task_input.get("message_id")) is not int or task_input["message_id"] != expected.message_id
                or type(task_input.get("ai_config_id")) is not int or task_input["ai_config_id"] != expected.config_id):
            raise SummaryExecutionChanged()
        statement = select(
            AgentConversation.summary, AgentConversation.summarized_until, AgentConversation.ai_history_epoch,
            AgentConversation.created_at, AgentConversation.summary_source,
        ).where(
            AgentConversation.id == expected.conversation_id, AgentConversation.user_id == expected.actor_id,
            AgentConversation.project_id == expected.project_id,
        )
        if db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=not write)
        conversation = (await db.execute(statement)).one_or_none()
        if conversation is None or tuple(conversation) != (
            expected.summary, expected.summarized_until, expected.history_epoch, expected.conversation_created_at,
            expected.summary_source,
        ):
            raise SummaryExecutionChanged()
        ids = {value[0] for value in expected.messages} | {expected.message_id}
        statement = select(AgentMessage).where(
            AgentMessage.conversation_id == expected.conversation_id, AgentMessage.id.in_(ids),
        )
        if db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=not write)
        messages = {row.id: row for row in await db.scalars(statement)}
        current = messages.get(expected.message_id)
        if (current is None or current.role != "user" or current.turn_task_id != expected.task_id
                or current.turn_task_key != expected.task_key
                or any(messages.get(old[0]) is None or _message_snapshot(messages[old[0]]) != old
                       for old in expected.messages)):
            raise SummaryExecutionChanged()
        statement = select(*(getattr(AIConfig, name) for name in _CONFIG_FIELDS)).where(
            AIConfig.id == expected.config_id, AIConfig.user_id == expected.model_binding.config_owner_id,
        )
        if db.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(read=not write)
        config = (await db.execute(statement)).one_or_none()
        if config is None or tuple(config) != expected.config_fields:
            raise SummaryExecutionChanged()
        await project_ai_config.revalidate_project_model(
            db, expected.model_binding, cipher=expected.cipher, allow_private=expected.allow_private,
            for_update=write, lock_config=True,
        )


def _summary_guard(provider, stamp, maker, expected):
    # handler 已包了仅有可见性戳的旧 guard；不丢弃任何额外回调。
    if isinstance(provider, _GuardedProvider):
        if (type(provider) is not _GuardedProvider or provider._stamp != stamp
                or provider._verify is not None or provider._authorize is not None
                or provider._verify_preflight is not None or provider._chapter is not None
                or provider._sessionmaker.kw.get("bind") is not maker.kw.get("bind")):
            raise SummaryExecutionChanged()
        provider = provider._provider

    # 模型只能由受控工厂构造；绑定原配置的密钥、端点和实际发送模型。
    try:
        transport, origin = ai_dispatch._chapter_transport(provider)  # noqa: SLF001
        if (transport is not provider or origin.provider_name != expected.config_fields[2]
                or provider.api_key != expected.cipher.decrypt(expected.config_fields[4])
                or provider.base_url != (expected.config_fields[3] or None)
                or provider.follow_redirects is not expected.allow_private):
            raise SummaryExecutionChanged()
        from app.services.collaboration_ai_guard import _BoundModelProvider

        provider = _BoundModelProvider(provider, expected.config_fields[5], "text")
    except Exception:
        provider = None
    if provider is None:
        raise SummaryExecutionChanged()
    if expected.grant_identity is not None:
        return _collaboration_summary_guard(provider, stamp, maker, expected)

    async def authorize(db):
        try:
            await authorize_send_actor(db, AuthenticatedActor(expected.actor_id, expected.actor_token_version))
            return
        except Exception:
            pass
        raise SummaryExecutionChanged()

    async def check(db, *, preflight: bool):
        try:
            if preflight:
                await ai_dispatch.require_preflight_lease(db, expected.actor_id, expected.project_id)
            else:
                await ai_dispatch.require_actual_send_lease(db, expected.actor_id, expected.project_id)
            await _check_summary_execution(db, expected, write=False)
            return
        except SummaryExecutionChanged:
            raise
        except Exception:
            pass
        raise SummaryExecutionChanged()

    async def verify(db):
        await check(db, preflight=False)

    async def verify_preflight(db):
        await check(db, preflight=True)

    return guard_provider(provider, stamp, maker, authorize=authorize, verify=verify, verify_preflight=verify_preflight)


def _collaboration_summary_guard(provider, stamp, maker, expected):
    """协作轮次（E4i-b）：每次发送以当前已证明的执行身份新建 for_task guard。

    guard 在发送 lease 内重验原认证令牌、accepted editor 与原邀请轮次、作者模型授权（source=owner 时）、
    原 Task 执行身份与配置指纹；随后在同一 lease 事务内核对会话、消息、私人风格与摘要来源。
    任一变化即失败关闭，不发出模型请求。智能体断点逐步推进，故不能沿用构建时的 guard。
    """
    from app.services import collaboration_ai_guard
    from app.services.agent.access import text_guard_extras

    guard = collaboration_ai_guard.for_task(
        expected.task_execution, expected.model_binding, stamp, cipher=expected.cipher,
        allow_private=expected.allow_private,
        # 受理时冻结的共享向量选择（检索用）属同一任务绑定，文本发送须一并重验（E4i-c）。
        extra_bindings=text_guard_extras(expected.task_execution, expected.model_binding),
    )

    async def check(db, *, preflight: bool):
        if preflight:
            await guard.verify_without_one_shot(db)
        else:
            await guard.verify(db)
        try:
            await _check_summary_execution(db, expected, write=False)
            return
        except SummaryExecutionChanged:
            raise
        except Exception:
            pass
        raise SummaryExecutionChanged()

    async def verify(db):
        await check(db, preflight=False)

    async def verify_preflight(db):
        await check(db, preflight=True)

    return guard_provider(
        provider, stamp, maker, authorize=guard.authorize, verify=verify, verify_preflight=verify_preflight,
    )


async def guard_agent_provider(db, conversation, project, cfg, provider, *, execution):
    """绑定主模型本轮身份；调用方须在发送前结束自己的读事务。"""
    if db.new or db.dirty or db.deleted:
        raise SummaryExecutionChanged()
    with db.no_autoflush:
        stamp = await history.require_open_history(db, conversation)
        rows = list(await db.scalars(select(AgentMessage).where(
            AgentMessage.conversation_id == conversation.id,
        ).order_by(AgentMessage.id)))
        style = await _history_project_style(db, project.id, conversation.id, conversation.user_id)
        expected = _summary_execution(
            execution, getattr(execution, "task_id", None), conversation, project, cfg, rows, style,
        )
        maker = visibility_sessionmaker(db)
        return meter_provider(
            _summary_guard(provider, stamp, maker, expected), cfg, maker, actor_id=expected.actor_id,
            kind="agent.turn", project_id=expected.project_id, project_owner_id=expected.project_owner_id,
            task_id=expected.task_id, task_key=expected.task_key,
        )


async def _finish_summary(
    maker, expected, stamp, *, summary, summarized_until, status, error, log_messages, prompt,
    output_chars, duration_ms,
) -> None:
    """摘要和本人日志在短事务内重验原执行体后原子提交。"""
    from app.services import generation_log_service

    try:
        async with maker() as db:
            actor = AuthenticatedActor(expected.actor_id, expected.actor_token_version)
            bound = db.info.get("authenticated_actor")
            if bound is not None and bound != actor:
                raise SummaryExecutionChanged()
            db.info["authenticated_actor"] = actor
            with db.no_autoflush:
                if expected.grant_identity is None:
                    await setting_ai_visibility.lock_author_visibility_write(
                        db, expected.actor_id, expected.project_id, manage=False,
                    )
                else:
                    # 协作轮次：与运行器同一锁序（gate → actor/作者 User → 发送写门禁 → 作品 → Task），
                    # 按原领取身份重验 accepted editor、原授权轮次与作者模型授权后才保存本人摘要与日志。
                    await require_write_execution(
                        db, expected.task_execution, project_owner_id=expected.project_owner_id,
                    )
                await require_visibility_stamp(db, stamp)
                await _check_summary_execution(db, expected, write=True)
                if status == "success":
                    conversation = await db.get(AgentConversation, expected.conversation_id)
                    conversation.summary = summary
                    conversation.summarized_until = summarized_until
                    # 裁决 13：来源标记与作者摘要区分；作者写入保持 NULL。
                    conversation.summary_source = summary_source_for(expected.grant_identity)
                await generation_log_service.write_log(
                    db,
                    project_id=expected.project_id,
                    actor_user_id=expected.actor_id,
                    chapter_id=None,
                    task_id=expected.task_id,
                    kind="agent.turn",
                    ai_config_id=expected.config_id,
                    provider=expected.config_fields[2],
                    model=expected.config_fields[5],
                    model_binding=expected.model_binding,
                    params={"conversation_id": expected.conversation_id, "stage": "summary", "prompt": prompt},
                    messages=log_messages,
                    status=status,
                    error=error,
                    output_chars=output_chars,
                    duration_ms=duration_ms,
                )
                await db.commit()
        return
    except SummaryExecutionChanged:
        raise
    except VisibilityPolicyError:
        raise
    except Exception:
        # 不在任务、日志或 traceback 中传播可能携带私密内容的 SQL 参数。
        pass
    raise SummaryExecutionChanged()


def untrusted_text(text: str) -> str:
    # 转义边界标记，防止用户数据自行关闭数据区。
    text = text.replace("【不可信数据开始】", "［不可信数据开始］").replace("【不可信数据结束】", "［不可信数据结束］")
    return f"【不可信数据开始】\n{text}\n【不可信数据结束】"


def _model_value(value: Any, depth: int = 0, *, in_preview: bool = False) -> Any:
    """模型工具回执不能重放内部身份；返回新结构，不修改持久效果。"""
    from app.services.personal_projection import (
        has_private_guard,
        normalized_private_key,
        normalized_private_path,
        partial_private,
    )

    if depth >= 64:
        return "[工具结果已隐藏]"
    if isinstance(value, dict):
        field = next((item for key, item in value.items()
                      if isinstance(key, str) and key.casefold() == "field"), None)
        if normalized_private_path(field):
            return {"warning": "内部字段已隐藏"}
        known_guard = has_private_guard(value)
        return {
            key: _model_value(item, depth + 1,
                              in_preview=in_preview or (isinstance(key, str) and key.casefold() == "preview"))
            for key, item in value.items()
            if not normalized_private_key(key) and not (
                isinstance(key, str) and "".join(c for c in key.casefold() if c.isalnum()) == "guards" and known_guard
            )
        }
    if isinstance(value, list):
        return [_model_value(item, depth + 1, in_preview=in_preview) for item in value]
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("{", "[", '"')):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return "[工具结果已隐藏]" if partial_private(value, in_preview=in_preview) else value
            else:
                if isinstance(parsed, (dict, list, str)):
                    safe = _model_value(parsed, depth + 1, in_preview=in_preview)
                    return value if safe == parsed else json.dumps(safe, ensure_ascii=False, default=str)
    return value


def tool_result(data: Any) -> str:
    data = _model_value(data)
    payload = json.dumps({"untrusted": True, "data": data}, ensure_ascii=False, default=str)
    if len(payload) <= RESULT_LIMIT:
        return payload
    # 截断嵌套 JSON 后作为字符串重新编码，始终保持外层合法 JSON。
    raw = json.dumps(data, ensure_ascii=False, default=str)
    lo, hi = 0, min(len(raw), RESULT_LIMIT)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = json.dumps({"untrusted": True, "data": raw[:mid], "warning": TRUNCATED}, ensure_ascii=False)
        if len(candidate) <= RESULT_LIMIT:
            lo = mid
        else:
            hi = mid - 1
    return json.dumps({"untrusted": True, "data": raw[:lo], "warning": TRUNCATED}, ensure_ascii=False)


async def skills_catalog(db: AsyncSession, user_id: int) -> str:
    """可见 Skill 清单（本人优先）；说明可能由他人编写，整体作为不可信数据。"""
    rows = await skill_service.visible_skills(db, user_id)
    if not rows:
        return ""
    lines = [
        "｜".join(
            [
                s.slug,
                s.name,
                s.description.replace("\n", " "),
                " ".join(f"/{t}" for t in s.triggers or []) or "-",
            ]
        )
        for s in rows[:SKILLS_CATALOG_LIMIT]
    ]
    if len(rows) > SKILLS_CATALOG_LIMIT:
        lines.append(SKILLS_CATALOG_MORE.format(count=len(rows) - SKILLS_CATALOG_LIMIT))
    return untrusted_text("\n".join(lines))


def forced_skill_message(snapshot: dict | None) -> dict | None:
    if not snapshot or not snapshot.get("body"):
        return None
    header = FORCED_SKILL_HEADER.format(name=snapshot.get("name", ""))
    content = f"{header}\n\n{snapshot['body'][: skill_service.SNAPSHOT_LIMIT]}"
    defs = snapshot.get("input_defs")
    if isinstance(defs, list) and defs:
        # 触发词启用带输入项的 Skill：正文未填充，附上输入项定义由智能体按对话代填
        content += f"\n\n【该 Skill 的输入项】\n{skill_inputs.describe(defs)}\n{skill_inputs.AGENT_FILL_INSTRUCTIONS}"
    return {"role": "system", "content": content}


async def validate_page_context(
    db: AsyncSession, project_id: int, raw: dict | None, *, scope: "AiScope | None" = None,
) -> dict:
    if not raw:
        return {}
    cleaned = {}
    route = str(raw.get("route") or "")[:500]
    if route.startswith(f"/projects/{project_id}/") or route == f"/projects/{project_id}":
        if scope is None:
            cleaned["route"] = route
        else:
            # 深链、查询串和浏览对象不是作者显式引用；只发送已知页面类别。
            base = f"/projects/{project_id}"
            section = route.removeprefix(base).lstrip("/").split("/", 1)[0]
            cleaned["route"] = f"{base}/{section}" if section in _PAGE_SECTIONS else base
    for key, model in (("chapter_id", Chapter), ("outline_id", Outline), ("character_id", Character)):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            if model is Character and scope is not None and not scope.includes("character", value):
                continue
            exists = await db.scalar(select(model.id).where(model.id == value, model.project_id == project_id))
            if exists is not None:
                cleaned[key] = value
    if cleaned.get("chapter_id") and isinstance(raw.get("selection"), str):
        chapter = await db.get(Chapter, cleaned["chapter_id"])
        selection = raw["selection"][:2000]
        if chapter is not None and selection and selection in chapter.content:
            cleaned["selection"] = selection
    return cleaned


_PROJECT_TOOLS = frozenset({"get_project_overview", "update_project"})
_STYLE_FIELDS = frozenset({"default_style_id", "default_style"})
_IMAGE_LIST_TOOLS = frozenset({"list_characters", "list_world_settings", "list_organizations", "list_careers"})
_IMAGE_ITEM_TOOLS = frozenset({
    "get_character", "get_organization", "create_character", "update_character", "create_world_setting",
    "update_world_setting", "create_organization", "update_organization", "create_career", "update_career",
})


def _project_style_for_history(value: Any, style: _HistoryProjectStyle | None, depth: int = 0) -> Any:
    """历史回执只认当前同快照可见且内容匹配的项目风格，返回副本。"""
    if depth >= 64:
        return "[历史项目结果已隐藏]"
    if isinstance(value, dict):
        result = {
            key: item if key == "field" else _project_style_for_history(item, style, depth + 1)
            for key, item in value.items()
        }
        field = value.get("field")
        if isinstance(field, str):
            parts = field.split(".")
            is_project = style is not None and len(parts) == 3 and parts[:2] == ["projects", str(style.project_id)]
            if parts[-1] == "default_style_id":
                for key in ("before", "after"):
                    if key in result and not (is_project and style.style_id is not None
                                              and type(value.get(key)) is int and value[key] == style.style_id):
                        result[key] = None
            elif parts[-1] in ("default_style", "style") and parts[0] == "projects":
                if parts[-1] == "default_style" or not is_project or style.owner_id != style.actor_id:
                    for key in ("before", "after"):
                        if key in result:
                            result[key] = None
        if _STYLE_FIELDS.intersection(value):
            brief = value.get("default_style")
            old_id = value.get("default_style_id")
            expected = {
                "id": style.style_id, "name": style.name, "description": style.description,
                "is_preset": style.is_preset,
            } if style is not None and style.style_id is not None else None
            id_valid = (expected is not None and type(old_id) is int and old_id == style.style_id
                        and ("id" not in value or (type(value.get("id")) is int
                                                   and value["id"] == style.project_id)))
            valid = (id_valid and type(value.get("id")) is int and value["id"] == style.project_id
                     and isinstance(brief, dict) and type(brief.get("id")) is int
                     and all(brief.get(key) == val for key, val in expected.items()))
            if "default_style_id" in value:
                result["default_style_id"] = style.style_id if (valid or (id_valid and "default_style" not in value)) \
                    else None
            if "default_style" in value:
                result["default_style"] = expected if valid else None
        return result
    if isinstance(value, list):
        return [_project_style_for_history(item, style, depth + 1) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return "[历史项目结果已隐藏]" if any(key in value for key in _STYLE_FIELDS) else value
            return json.dumps(_project_style_for_history(parsed, style, depth + 1), ensure_ascii=False, default=str)
        if any(key in value for key in _STYLE_FIELDS):
            return "[历史项目结果已隐藏]"
    return value


def _history_image_tool_content(content: Any, *, list_tool: bool) -> str:
    """按已知工具的结果结构移除实体级图片元数据，无法验证的片段失败关闭。"""
    try:
        parsed = json.loads(content) if isinstance(content, str) else None
    except (ValueError, RecursionError):
        parsed = None
    data = parsed.get("data") if isinstance(parsed, dict) and parsed.get("untrusted") is True else None
    if not isinstance(data, dict):
        return tool_result({"error": "历史设定工具结果无法验证"})

    def without_image(item: dict) -> dict:
        return {key: value for key, value in item.items() if key not in {"thumbnail", "thumbnail_editable"}}

    if list_tool and "items" in data:
        items = data["items"]
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return tool_result({"error": "历史设定工具结果无法验证"})
        data = {**data, "items": [without_image(item) for item in items]}
    elif list_tool and "error" not in data:
        return tool_result({"error": "历史设定工具结果无法验证"})
    else:
        data = without_image(data)
    return tool_result(data)


def _history_tool_content(
    content: Any, style: _HistoryProjectStyle | None, *, project_tool: bool, image_tool: str | None = None,
) -> Any:
    content = _model_value(content)
    if project_tool:
        # 项目工具输出应是原生 tool_result 包装的 ProjectOut；截断或无法证明结构时整段隐藏。
        try:
            parsed = json.loads(content) if isinstance(content, str) else None
        except (ValueError, RecursionError):
            parsed = None
        data = parsed.get("data") if isinstance(parsed, dict) and parsed.get("untrusted") is True else None
        if (style is None or not isinstance(data, dict) or type(data.get("id")) is not int
                or data["id"] != style.project_id
                or not _STYLE_FIELDS.issubset(data)):
            return tool_result({"error": "历史项目工具结果无法验证"})
        try:
            safe = ProjectOut.model_validate(_project_style_for_history(data, style)).model_dump(
                mode="json", exclude={"pen_name"},
            )
        except (ValidationError, ValueError, TypeError):
            return tool_result({"error": "历史项目工具结果无法验证"})
        # 重建白名单响应，丢弃不可信包装及扩展字段，不修改原始回执。
        return tool_result(safe)
    content = _project_style_for_history(content, style)
    if image_tool is not None:
        return _history_image_tool_content(content, list_tool=image_tool in _IMAGE_LIST_TOOLS)
    return content


def _history_tool_calls(calls: Any, style: _HistoryProjectStyle | None) -> Any:
    result = _model_value(calls)
    if not isinstance(result, list):
        return result
    return [
        {**call, "arguments": _project_style_for_history(call["arguments"], style)}
        if isinstance(call, dict) and "arguments" in call else call
        for call in result
    ]


def message_dict(
    row: AgentMessage, style: _HistoryProjectStyle | None = None, *, expected_tool_name: str | None = None,
) -> dict:
    image_tool = next((name for name in (expected_tool_name, row.tool_name)
                       if name in _IMAGE_LIST_TOOLS or name in _IMAGE_ITEM_TOOLS), None)
    if row.role == "tool" and not row.tool_call_id:
        # 撤销备注没有对应的 assistant.tool_calls，不能冒充原生 tool_result。
        return {"role": "user", "content": untrusted_text(_history_tool_content(
            row.content, style, project_tool=row.tool_name in _PROJECT_TOOLS, image_tool=image_tool,
        ))}
    if row.role == "tool":
        content = _history_tool_content(
            row.content, style, project_tool=row.tool_name in _PROJECT_TOOLS or expected_tool_name in _PROJECT_TOOLS,
            image_tool=image_tool,
        )
    else:
        content = (_project_style_for_history(_model_value(row.content), style)
                   if row.role == "assistant" else row.content)
    value: dict[str, Any] = {"role": row.role, "content": content}
    if row.tool_calls:
        # 保留 Provider 的签名等协议元数据，只过滤可重放的工具参数。
        value["tool_calls"] = _history_tool_calls(row.tool_calls, style)
    if row.tool_call_id:
        value.update(tool_call_id=row.tool_call_id, name=row.tool_name)
    return value


def _groups(rows: list[AgentMessage]) -> list[list[AgentMessage]]:
    groups: list[list[AgentMessage]] = []
    for row in rows:
        if row.role == "tool" and groups and (row.tool_call_id or groups[-1][0].tool_calls):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def change_result_for_model(change: AgentChange) -> dict:
    """审批记录面向人类；模型只拿最小回执，不能经写工具读回未提交给它的资料。"""
    if change.status == "undone":
        return {"message": "用户已撤销该修改"}
    if change.kind == "external":
        if change.status == "executing":
            return {"error": "外部操作未完成"}
        return _model_value(change.result) if change.result else {"error": "外部操作未完成"}
    if change.status == "rejected":
        return {"message": "用户拒绝了该修改"}
    if change.status != "executed":
        return {"error": "修改未完成，请检查允许的对象与输入"}
    result = {"ok": True}
    raw = change.result or {}
    task_id = raw.get("task_id")
    if change.kind == "task" and type(task_id) is int:
        result["task_id"] = task_id
    if change.tool_name.startswith("create_") and type(raw.get("id")) is int:
        # 仅返回刚创建的对象 ID，方便引用本次写入；此回执不授予后续读取资格。
        result["id"] = raw["id"]
    return result


def _group_messages(
    group: list[AgentMessage], changes: dict[tuple, AgentChange], style: _HistoryProjectStyle | None = None,
) -> list[dict]:
    first = group[0]
    if not first.tool_calls:
        return [message_dict(row, style) for row in group]
    results = {row.tool_call_id: row for row in group[1:] if row.tool_call_id}
    messages = [message_dict(first, style)]
    for call in first.tool_calls:
        existing = results.get(call["id"])
        if existing is None:
            change = changes.get((first.turn_task_id, call["id"]))
            data = {"error": "这次工具调用未完成，请重新查询状态，不要假定操作已执行"}
            if change is not None:
                data = change_result_for_model(change)
            result = {"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                      "content": tool_result(_project_style_for_history(data, style))}
        else:
            result = message_dict(existing, style, expected_tool_name=call.get("name"))
        messages.append(result)
    # 用户在待确认时撤销已完成修改，备注须位于本次原生 tool_result 组之后。
    messages.extend(message_dict(row, style) for row in group[1:] if not row.tool_call_id)
    return messages


def _fit_group(items: list[dict], budget: int) -> list[dict]:
    """只裁内容，不裁原生工具参数或签名；完整协议本身放不下时明确报错。"""
    fitted = deepcopy(items)
    for item in fitted:
        item["content"] = "" if item["role"] != "tool" else tool_result({"warning": TRUNCATED})
    overhead = len(json.dumps(fitted, ensure_ascii=False))
    if overhead > budget:
        raise BadRequestError("工具调用上下文超过模型预算，请换用更大上下文的模型或开启新会话")
    allowance = max(0, (budget - overhead - 40 * len(items)) // max(1, len(items)))
    for item, original in zip(fitted, items, strict=True):
        content = original.get("content", "")[:allowance]
        item["content"] = tool_result({"excerpt": content, "warning": TRUNCATED}) if item["role"] == "tool" else content
    # 转义字符会增大序列化开销，必要时使用最小合法工具结果。
    if len(json.dumps(fitted, ensure_ascii=False)) > budget:
        for item in fitted:
            item["content"] = "" if item["role"] != "tool" else tool_result({"warning": TRUNCATED})
    return fitted


def _summary_source_verified(conversation, style: _HistoryProjectStyle, execution: Any | None) -> bool:
    """作者会话：摘要来源须为空（作者 / 升级前）。协作者会话（裁决 13）：只承认本授权轮次在受控路径
    生成并标记的摘要；无标记（来源不明）或来自其他邀请轮次（撤权后重邀）一律不可验证。"""
    source = conversation.summary_source
    if style.owner_id == style.actor_id:
        return source is None
    identity = execution.task_execution_identity if execution is not None else None
    grant = identity.grant_identity if type(identity) is TaskExecutionIdentity else None
    return (grant is not None and source is not None and source == summary_source_for(grant)
            and identity.actor_id == style.actor_id and identity.project_id == style.project_id)


async def build_messages(
    db: AsyncSession,
    conversation: AgentConversation,
    project: Project,
    cfg: AIConfig,
    provider: LLMProvider,
    page_context: dict | None = None,
    task_id: int | None = None,
    *,
    scope: "AiScope | None" = None,
    execution: Any | None = None,
) -> tuple[list[dict], dict]:
    # 任何身份检查前不得自动 flush 调用方待写数据。
    if execution is not None and (db.new or db.dirty or db.deleted):
        raise SummaryExecutionChanged()
    history_style = await _history_project_style(db, project.id, conversation.id, conversation.user_id)
    if conversation.summary and not _summary_source_verified(conversation, history_style, execution):
        # 摘要是不可追溯的自由文本，无法证明其中没有旧的私人风格回执。
        raise AgentHistoryUnverifiable()
    stamp = await history.require_open_history(db, conversation)
    if scope is not None:
        await require_visibility_stamp(db, scope.stamp)
    maker = visibility_sessionmaker(db)
    budget = int((cfg.context_window_k or 32) * 1000 * 0.6)
    rows = list(
        await db.scalars(
            select(AgentMessage)
            .where(
                AgentMessage.conversation_id == conversation.id,
                AgentMessage.id > (conversation.summarized_until or 0),
            )
            .order_by(AgentMessage.id)
        )
    )
    latest_user = await db.scalar(
        select(AgentMessage)
        .where(
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.role == "user",
        )
        .order_by(AgentMessage.id.desc())
        .limit(1)
    )
    if scope is None:
        scope = await history.build_tool_scope(db, stamp, latest_user.content if latest_user is not None else "")
    changes = {
        (c.turn_task_id, c.tool_call_id): c
        for c in await db.scalars(select(AgentChange).where(AgentChange.conversation_id == conversation.id))
    }
    groups = _groups(rows)
    size = (sum(len(json.dumps(message_dict(r, history_style), ensure_ascii=False)) for r in rows)
            + len(conversation.summary))
    if size > budget and len(groups) > 1:
        split = max(1, len(groups) // 2)
        older = [r for group in groups[:split] for r in group]
        tool_names = {}
        for group in groups[:split]:
            calls = {call.get("id"): call.get("name") for call in (group[0].tool_calls or [])}
            for item in group[1:]:
                if item.tool_call_id in calls:
                    tool_names[item.id] = calls[item.tool_call_id]
        if latest_user is None or latest_user.id != getattr(execution, "input_message_id", None):
            raise SummaryExecutionChanged()
        protected_rows = older if latest_user in older else [*older, latest_user]
        expected = _summary_execution(execution, task_id, conversation, project, cfg, protected_rows, history_style)
        if db.new or db.dirty or db.deleted:
            raise SummaryExecutionChanged()
        override = await prompt_service.get_override(db, conversation.user_id, "agent.summarize")
        summary_messages = render(
            "agent.summarize",
            override,
            {
                "summary": untrusted_text(conversation.summary),
                "history": untrusted_text(
                    json.dumps(
                        [
                            {
                                **message_dict(r, history_style, expected_tool_name=tool_names.get(r.id)),
                                "tool_calls": _history_tool_calls([
                                    {k: v for k, v in call.items() if k != "provider_metadata"}
                                    for call in (r.tool_calls or [])
                                ], history_style),
                            }
                            for r in older
                        ],
                        ensure_ascii=False,
                    )[: max(100, budget - len(conversation.summary) - 200)]
                ),
            },
        )
        pieces = []
        started = time.monotonic()
        status, error = "success", ""
        log_messages = summary_messages
        summary = ""
        # 上游可能按 RR 读过作品；网络期间不得持有其锁或旧写快照。
        await db.commit()
        provider = meter_provider(
            _summary_guard(provider, stamp, maker, expected), cfg, maker, actor_id=expected.actor_id,
            kind="agent.summarize", project_id=expected.project_id, project_owner_id=expected.project_owner_id,
            task_id=expected.task_id, task_key=expected.task_key,
        )
        try:
            async with aclosing(
                provider.generate_stream(summary_messages, cfg.model, max_tokens=min(2000, budget // 4))
            ) as stream:
                async for piece in stream:
                    pieces.append(piece)
            summary = "".join(pieces)[: max(100, budget // 4)]
        except asyncio.CancelledError:
            status, error = "cancelled", "摘要已取消"
            raise
        except Exception as exc:
            status, error = "error", str(exc)[:500]
            if isinstance(exc, VisibilityPolicyError):
                log_messages = []
            raise
        finally:
            await _finish_summary(
                maker, expected, stamp, summary=summary, summarized_until=older[-1].id,
                status=status,
                error=error,
                log_messages=log_messages,
                prompt=prompt_meta("agent.summarize", override),
                output_chars=len("".join(pieces)),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        set_committed_value(conversation, "summary", summary)
        set_committed_value(conversation, "summarized_until", older[-1].id)
        # 同一轮随后主对话的发送守卫按会话对象重建期望值，来源标记须与刚提交的一致（E4i-b 复核）。
        set_committed_value(conversation, "summary_source", summary_source_for(expected.grant_identity))
        groups = groups[split:]
    # 最新作者消息始终保留；工具消息只能和对应的 assistant 作为一组保留。
    summary_message = (
        {"role": "user", "content": "较早对话的摘要：\n" + untrusted_text(conversation.summary)}
        if conversation.summary
        else None
    )
    latest = message_dict(latest_user, history_style) if latest_user is not None else None
    selected = []
    remaining = budget - (len(json.dumps(summary_message, ensure_ascii=False)) if summary_message else 0)
    if latest:
        if len(json.dumps(latest, ensure_ascii=False)) > remaining // 2:
            latest["content"] = latest["content"][: max(100, remaining // 2 - 100)] + "\n（消息按上下文预算截断）"
        remaining -= len(json.dumps(latest, ensure_ascii=False))
    for group in reversed(groups):
        if latest_user is not None and any(r.id == latest_user.id for r in group):
            continue
        items = _group_messages(group, changes, history_style)
        cost = len(json.dumps(items, ensure_ascii=False))
        if cost > remaining:
            if selected:
                break
            items = _fit_group(items, remaining)
            cost = len(json.dumps(items, ensure_ascii=False))
        selected.insert(0, (group[0].id, items))
        remaining -= cost
    if latest:
        selected.append((latest_user.id, [latest]))
    selected_messages = [item for _, group in sorted(selected, key=lambda entry: entry[0]) for item in group]
    override = await prompt_service.get_override(db, conversation.user_id, "agent.system")
    info = format_project_info(await project_service.project_info(db, project, actor_user_id=conversation.user_id))
    safe_page = await validate_page_context(db, project.id, page_context, scope=scope)
    system = render(
        "agent.system",
        override,
        {
            "safety_rules": SAFETY_RULES,
            "project_info": untrusted_text(info),
            "page_hint": untrusted_text(json.dumps(safe_page, ensure_ascii=False)),
            "skills_catalog": await skills_catalog(db, conversation.user_id),
        },
    )
    # 兼容旧的损坏覆盖：通用模板兜底把缺失锁定段补到 user，此处提升回 system 权限。
    if not any(m["role"] == "system" and SAFETY_RULES in m["content"] for m in system):
        system = [{"role": "system", "content": SAFETY_RULES}] + [
            {**m, "content": m["content"].replace(SAFETY_RULES, "")} for m in system
        ]
    messages = list(system)
    # 强制启用的 Skill 正文排在全部 system（含锁定安全规则）之后，覆盖模板也无法把它放到安全规则之前
    forced = forced_skill_message(latest_user.skill_snapshot if latest_user is not None else None)
    if forced is not None:
        messages.append(forced)
    if summary_message:
        messages.append(summary_message)
    messages.extend(selected_messages)
    await require_visibility_stamp(db, scope.stamp)
    return messages, prompt_meta("agent.system", override)
