"""agent.turn：同会话互斥，确认后续跑，重试复用已持久化的工具结果。"""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import exists, or_, select, update

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import AgentChange, AgentConversation, AgentMessage, AIConfig, Project, Task
from app.schemas.agent import SendMessage
from app.services import (
    ai_config_service,
    authorization_service,
    collaboration_ai_guard,
    project_ai_config,
    project_lifecycle,
    skill_service,
)
from app.services.agent import access as agent_access
from app.services.agent import context, history, service
from app.services.agent.runner import (
    AgentJob,
    _AgentExecutionState,
    message_snapshot,
    model_snapshot,
    require_no_unknown_external,
    run_agent,
    verify_job_resources,
)
from app.services.ai_dispatch import chapter_provider_factory, guard_provider, lock_visibility_write
from app.services.collaboration_task_access import GRANT_KEY, GrantIdentity, task_grant_identity
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    matches_running,
    require_live_worker,
    require_write_execution,
    same_execution,
)
from app.services.task_engine.registry import TaskSpec, register

logger = logging.getLogger(__name__)


async def _void_revoked_turn(
    ctx, task: Task, *, execution: TaskExecutionIdentity | None,
) -> str | None:
    """锁序 gate → actor/作者 User 升序 → 发送写门禁 → 作品父行 → Task 内复核原协作轮次已失效，再作废待确认提案。

    先做无锁预筛（与待审核复查同一谓词）：授权仍有效时不取任何写锁。返回锁内确认的失效原因
    （``COLLABORATION_FAILURE_MESSAGES`` 的键）并已提交作废；未失效 / 无法证明时返回 None。
    """
    from app.services.task_engine.engine import TaskEngine

    probe = SimpleNamespace(id=task.id, statistics_key=task.statistics_key, user_id=task.user_id,
                            project_id=task.project_id, input=task.input)
    grant = task_grant_identity(task.input)
    if grant is None:
        return None
    async with ctx.sessionmaker() as db:
        if not await TaskEngine._paused_grant_lost(db, probe):
            return None
        owner_id = await db.scalar(select(Project.user_id).where(Project.id == task.project_id))
        await db.rollback()
        if owner_id is None or owner_id == task.user_id:
            return None
        await authorization_service.gate(db)
        await authorization_service.lock_users(db, *sorted({task.user_id, owner_id}), missing_ok=True)
        await lock_visibility_write(db, task.project_id)
        try:
            project = await project_lifecycle.lock_active(db, task.project_id)
        except AppError:
            # 删除中 / 回收站：作品级排空负责收尾，这里不越过生命周期屏障。
            await db.rollback()
            return None
        owner = execution.worker_key if execution is not None else task.worker_key
        current = await db.scalar(select(Task).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.kind == "agent.turn",
            Task.queued_at == task.queued_at, Task.execution_key == task.execution_key,
            Task.worker_key == owner, Task.status == task.status,
        ).with_for_update().execution_options(populate_existing=True))
        failure = None
        if project.user_id == owner_id and current is not None and task_grant_identity(current.input) == grant:
            failure = await TaskEngine._collaboration_claim_failure(db, current)
        if failure is None:
            await db.rollback()
            return None
        if current.status == "running" or execution is not None:
            await require_live_worker(db, owner)
        await service.void_revoked_turn_changes(
            db, task_id=current.id, task_key=current.statistics_key, project_id=current.project_id,
        )
        await db.commit()
        return failure


def _retry_source_error() -> ConflictError:
    return ConflictError("原轮次仍有执行中的外部操作或身份无法确认，请稍后检查", extra={
        "code": "agent_retry_source_unavailable",
    })


def _canonical_key(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _actor_version(data) -> int:
    version = data.get("_agent_actor_token_version") if isinstance(data, dict) else None
    if (type(version) is not int or version < 0 or type(data.get("_task_actor_token_version")) is not int
            or data["_task_actor_token_version"] != version):
        raise _retry_source_error()
    return version


async def _require_original_actor(db, user_id: int, data) -> int:
    version = _actor_version(data)
    await authorization_service.require_authenticated(
        db, authorization_service.AuthenticatedActor(user_id, version), write=True,
    )
    return version


def _task_grant(data) -> GrantIdentity | None:
    """轮次任务绑定的协作授权：键存在但破损时失败关闭，不能退化为作者轮次。"""
    if not isinstance(data, dict):
        raise _retry_source_error()
    grant = task_grant_identity(data)
    if GRANT_KEY in data and grant is None:
        raise _retry_source_error()
    return grant


EMBEDDING_KEY = agent_access.EMBEDDING_KEY


async def _freeze_shared_embedding(
    db, ctx, *, user_id: int, project_id: int, owner_id: int, grant: GrantIdentity | None,
) -> dict:
    """协作轮次在受理事务冻结作者共享向量选择（E4-i 裁决 12，与大纲 / 实体同口径）。

    作者轮次不冻结，检索沿用本人默认向量（行为不变）。协作轮次：未授权 / 作者未配置 → 冻结为关键词；
    作者配置本地不可用（解密 / 端点等 400）→ 不冻结，运行时只用关键词；协作者本人的向量配置永不参与。
    运行时每次查询嵌入经 for_task 守卫逐次重验（tools.read._collaborator_embedder）。
    """
    if grant is None:
        return {}
    try:
        selection = await project_ai_config.resolve_shared_embedding(
            db, user_id, project_id, cipher=ctx.cipher,
            allow_private=ctx.settings.allow_private_ai_endpoints, for_update=True,
        )
    except BadRequestError:
        return {}
    if (selection.grant_identity != grant or selection.project_owner_id != owner_id
            or selection.actor_id != user_id or selection.project_id != project_id):
        raise _retry_source_error()
    return {EMBEDDING_KEY: project_ai_config.serialize_shared_embedding(selection)}


def _frozen_shared_embedding(data, *, user_id: int, project_id: int, owner_id: int, grant: GrantIdentity | None):
    """读取轮次冻结的共享向量选择：作者轮次不得携带；协作轮次缺失表示只用关键词，破损失败关闭。"""
    if not isinstance(data, dict):
        raise _retry_source_error()
    if EMBEDDING_KEY not in data:
        return None
    if grant is None:
        raise _retry_source_error()
    try:
        selection = project_ai_config.deserialize_shared_embedding(
            data[EMBEDDING_KEY], actor_id=user_id, project_id=project_id, project_owner_id=owner_id,
            task_grant=data.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        )
    except BadRequestError:
        raise _retry_source_error() from None
    if selection is None or selection.grant_identity != grant:
        raise _retry_source_error()
    return selection


def _binding_grant_payload(grant: GrantIdentity | None):
    return {"invitation_id": grant[0], "revision": grant[1]} if grant is not None else None


async def _lock_turn_conversation(
    db, user_id: int, project_id: int, conversation_id: int, grant: GrantIdentity | None, *,
    lock: bool = True, visibility_write: bool = False, denied: str = "not_found",
) -> tuple[int, AgentConversation]:
    """作者轮次沿用原入口；协作轮次按原 grant 锁后判定（accepted editor + ai.use，管理员来源不并入）。

    返回（作品作者 ID，会话）。会话只按真实 actor 查找：不属于本人仍为 404。
    """
    if grant is None and not visibility_write:
        conversation = await service.owned_conversation(db, user_id, project_id, conversation_id, lock=lock)
        return user_id, conversation
    project = await agent_access.lock_agent_write(
        db, user_id, project_id, grant=grant, visibility_write=visibility_write,
        allow_collaborator=grant is not None, on_denied=denied,
    )
    if (project.user_id == user_id) != (grant is None):
        raise _retry_source_error()
    conversation = await db.scalar(select(AgentConversation).where(
        AgentConversation.id == conversation_id, AgentConversation.project_id == project_id,
        AgentConversation.user_id == user_id,
    ).execution_options(populate_existing=True))
    if conversation is None:
        raise NotFoundError("会话不存在")
    return project.user_id, conversation


def _check_turn_binding(
    binding, *, user_id: int, project_id: int, owner_id: int, grant: GrantIdentity | None,
) -> project_ai_config.ResolvedProjectModel:
    """裁决 1 / R4：本人配置（source=actor），或授权开启且本人无配置时回退作者配置（source=owner）。

    选择本身由中央预检完成（本人无效配置不回退）；这里只核对冻结绑定与锁内作者、授权轮次一致。
    """
    if (type(binding) is not project_ai_config.ResolvedProjectModel or binding.actor_id != user_id
            or binding.project_id != project_id or binding.project_owner_id != owner_id
            or binding.grant_identity != grant or binding.model_type != "text"
            or binding.source not in ("actor", "owner")
            or (grant is None and binding.source != "actor")
            or binding.config_owner_id != (user_id if binding.source == "actor" else owner_id)):
        raise _retry_source_error()
    try:
        project_ai_config.serialize_project_model(binding)
    except BadRequestError:
        raise _retry_source_error() from None
    return binding


async def _bound_config(db, user_id: int, binding: project_ai_config.ResolvedProjectModel) -> AIConfig:
    """按冻结绑定的配置归属读取；作者轮次沿用原 resolve_config。"""
    if binding.grant_identity is None:
        return await ai_config_service.resolve_config(db, user_id, binding.config_id)
    cfg = await db.scalar(select(AIConfig).where(
        AIConfig.id == binding.config_id, AIConfig.user_id == binding.config_owner_id,
        AIConfig.model_type == "text",
    ))
    if cfg is None or cfg.instance_key != binding.config_instance_key:
        raise BadRequestError("模型配置已失效，请重新选择")
    return cfg


async def bind_queued_turn(db, task: Task) -> None:
    """首次发起在创建 Task 的事务内，同时绑定 user 消息与任务 UUID。"""
    data = task.input if isinstance(task.input, dict) else {}
    version = _actor_version(data)
    if db.info.get("authenticated_actor") != authorization_service.AuthenticatedActor(task.user_id, version):
        raise _retry_source_error()
    request = data.get("request")
    conversation_id = request.get("conversation_id") if isinstance(request, dict) else None
    message_id = data.get("message_id")
    if (task.kind != "agent.turn" or task.status != "queued" or task.retry_of_id is not None
            or not _canonical_key(task.statistics_key) or not isinstance(task.queued_at, datetime)
            or type(conversation_id) is not int or type(message_id) is not int):
        raise _retry_source_error()
    # 协作轮次的授权只能由中央按已受理模型绑定写入；两处必须一致。
    grant, model = _task_grant(data), data.get("_project_model")
    if (grant is not None or model is not None) and (
        not isinstance(model, dict) or model.get("grant_identity") != _binding_grant_payload(grant)
    ):
        raise _retry_source_error()
    owned_conversation = exists(select(AgentConversation.id).where(
        AgentConversation.id == conversation_id, AgentConversation.user_id == task.user_id,
        AgentConversation.project_id == task.project_id,
    ))
    result = await db.execute(update(AgentMessage).where(
        AgentMessage.id == message_id, AgentMessage.conversation_id == conversation_id,
        AgentMessage.role == "user", AgentMessage.turn_task_id.is_(None), AgentMessage.turn_task_key.is_(None),
        owned_conversation,
    ).values(turn_task_id=task.id, turn_task_key=task.statistics_key))
    if result.rowcount != 1:
        raise _retry_source_error()


async def _require_matched_turn_rows(db, *, task_id, task_key, conversation_id, project_id):
    """存在无法证明属于此任务的行时，拒绝整轮重放或迁移。"""
    for model in (AgentMessage, AgentChange):
        uncertain = await db.scalar(select(model.id).where(
            model.turn_task_id == task_id,
            or_(model.conversation_id != conversation_id, model.turn_task_key.is_(None),
                model.turn_task_key != task_key),
        ).limit(1))
        if uncertain is not None:
            raise _retry_source_error()
    if await db.scalar(select(AgentChange.id).where(
        AgentChange.conversation_id == conversation_id, AgentChange.turn_task_id == task_id,
        AgentChange.turn_task_key == task_key, AgentChange.project_id != project_id,
    ).limit(1)) is not None:
        raise _retry_source_error()


async def _recover_retry_source(
    db, ctx, *, source_id, source_key, user_id, project_id, conversation_id, message_id, actor_token_version,
    model_binding, grant: GrantIdentity | None = None,
):
    """持作品锁核验旧轮次，再按旧身份恢复；调用方负责整事务提交或回滚。"""
    if type(source_id) is not int or not _canonical_key(source_key):
        raise _retry_source_error()
    source = await db.scalar(select(Task).where(
        Task.id == source_id, Task.statistics_key == source_key, Task.user_id == user_id,
        Task.project_id == project_id, Task.kind == "agent.turn",
        Task.status.in_(("failed", "cancelled", "interrupted")), Task.execution_key.is_(None),
        Task.admin_cancel_operation_id.is_(None),
    ).with_for_update())
    source_input = source.input if source is not None and isinstance(source.input, dict) else {}
    request = source_input.get("request")
    if (source is None or source_input.get("message_id") != message_id or not isinstance(request, dict)
            or request.get("conversation_id") != conversation_id
            or (GRANT_KEY in source_input) != (grant is not None) or task_grant_identity(source_input) != grant
            or _actor_version(source_input) != actor_token_version
            or source_input.get("_project_model") != project_ai_config.serialize_project_model(model_binding)):
        raise _retry_source_error()
    message = await db.scalar(select(AgentMessage.id).where(
        AgentMessage.id == message_id, AgentMessage.conversation_id == conversation_id, AgentMessage.role == "user",
        AgentMessage.turn_task_id == source_id, AgentMessage.turn_task_key == source_key,
    ))
    if message is None:
        raise _retry_source_error()
    await _require_matched_turn_rows(
        db, task_id=source_id, task_key=source_key, conversation_id=conversation_id, project_id=project_id,
    )
    # 部分证据不完整时，即使上层捕获冲突后继续提交，也不得留下半批恢复结果。
    async with db.begin_nested():
        await service.recover_external(
            db, task_id=source_id, task_key=source_key, engine=ctx.task_engine, stale_only=True,
        )
        if await db.scalar(select(AgentChange.id).where(
            AgentChange.turn_task_id == source_id, AgentChange.status == "executing",
        ).limit(1)) is not None:
            raise _retry_source_error()
        await require_no_unknown_external(db, project_id=project_id, conversation_id=conversation_id)


class AgentHandler:
    kinds = ("agent.turn",)
    execution_identity_aware = True
    admission_preflight_aware = True
    # E4i-b：受理 / 重试 / 续跑 / 构建接受 accepted editor（原 grant，本人模型或授权回退作者模型）；
    # 每次模型发送以 for_task guard 重验；协作者实时订阅经 stream_guard 走中央受控管线。
    collaboration_execution_ready = True

    async def validate_and_claim(self, db, ctx, *, user_id, kind, params, admission=None, accepted_model=None):
        pid, cid = int(params["project_id"]), int(params["conversation_id"])
        grant = (accepted_model.grant_identity
                 if type(accepted_model) is project_ai_config.ResolvedProjectModel else None)
        # 发送写门禁先于作品父行（中央随后按 ai.use + visibility_write 重入同一组锁）。
        owner_id, conversation = await _lock_turn_conversation(db, user_id, pid, cid, grant, visibility_write=True)
        # 裁决 7（E4i-d 复核）：会话须属于本次受理的邀请轮次（作者会话无标记）。入口 owned_conversation 已判定，
        # 中央受理再锁内核对，使任何创建入口都不能向旧轮次 / 未经协作入口创建的会话发起新轮次。
        if conversation.collaboration_invitation_id != (None if grant is None else grant[0]):
            agent_access.deny_changed()
        await db.execute(select(AgentConversation.id).where(AgentConversation.id == cid).with_for_update())
        await require_no_unknown_external(db, project_id=pid, conversation_id=cid)
        stamp = await history.require_open_history(db, conversation, adopt_empty=True)
        if await service.active_turn(db, cid):
            raise ConflictError("当前会话已有进行中或待确认的轮次")
        req = SendMessage.model_validate(params)
        original = project_ai_config.require_original_preflight(admission)
        binding = accepted_model
        if (type(binding) is not project_ai_config.ResolvedProjectModel or binding != original.binding
                or original.requested_config_id != req.ai_config_id):
            raise _retry_source_error()
        _check_turn_binding(binding, user_id=user_id, project_id=pid, owner_id=owner_id, grant=grant)
        cfg = await _bound_config(db, user_id, binding)
        embedding = await _freeze_shared_embedding(
            db, ctx, user_id=user_id, project_id=pid, owner_id=owner_id, grant=grant,
        )
        page = await context.validate_page_context(db, pid, req.page_context.model_dump() if req.page_context else None)
        # 强制启用的 Skill：显式 skill_id（不可见 → 404）或消息开头的 /触发词；快照随消息保存，本轮续跑沿用。
        # 手动指定时用请求里的 skill_inputs 校验并填充；触发词启用时正文不填充，附输入项定义由智能体代填
        manual = req.skill_id is not None
        skill = (
            await skill_service.get_visible_skill(db, user_id, req.skill_id)
            if manual
            else await skill_service.match_trigger(db, user_id, req.content)
        )
        message = AgentMessage(
            conversation_id=cid,
            role="user",
            content=req.content,
            page_context=page,
            skill_id=skill.id if skill else None,
            skill_snapshot=(
                await skill_service.snapshot_of(db, skill, req.skill_inputs, fill=manual) if skill else None
            ),
        )
        db.add(message)
        conversation.last_message_at = datetime.now(UTC)
        if not conversation.title:
            conversation.title = req.content[:40]
        await db.flush()
        return TaskSpec(
            project_id=pid,
            chapter_id=None,
            resource_key=f"agent:{cid}",
            title=f"智能体：{req.content[:20]}",
            input={"request": params, "ai_config_id": cfg.id, "message_id": message.id, "history_epoch": stamp.epoch,
                   "_project_model": project_ai_config.serialize_project_model(binding), **embedding},
        )

    async def validate_retry(self, db, ctx, *, user_id, task, admission=None, accepted_model=None):
        if not isinstance(task.input, dict) or not _canonical_key(task.statistics_key):
            raise _retry_source_error()
        # 裁决 7：只能沿用原轮次授权；撤权后重邀（新邀请轮次）锁内与成员当前轮次不符即拒绝。
        grant = _task_grant(task.input)
        actor_token_version = await _require_original_actor(db, user_id, task.input)
        if await db.scalar(select(Task.id).where(Task.retry_of_id == task.id).limit(1)):
            raise ConflictError("此轮已有重试任务，请使用最新任务继续")
        req = task.input.get("request")
        if (not isinstance(req, dict) or type(req.get("conversation_id")) is not int
                or type(task.input.get("message_id")) is not int):
            raise _retry_source_error()
        cid = int(req["conversation_id"])
        owner_id, conversation = await _lock_turn_conversation(db, user_id, task.project_id, cid, grant)
        await require_no_unknown_external(db, project_id=task.project_id, conversation_id=cid)
        await history.require_open_history(db, conversation, expected_epoch=task.input.get("history_epoch"))
        if await service.active_turn(db, cid):
            raise ConflictError("当前会话已有进行中或待确认的轮次")
        binding = project_ai_config.deserialize_project_model(
            task.input.get("_project_model"), actor_id=user_id, project_id=task.project_id,
            project_owner_id=owner_id,
        )
        if binding is None or binding.config_id != task.input.get("ai_config_id"):
            raise _retry_source_error()
        _check_turn_binding(binding, user_id=user_id, project_id=task.project_id, owner_id=owner_id, grant=grant)
        original = project_ai_config.require_original_preflight(admission)
        if (type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model != binding or original.binding != binding):
            raise _retry_source_error()
        cfg = await _bound_config(db, user_id, binding)
        # 重试沿用原冻结的共享向量选择（与大纲重试一致），锁内重验原授权与配置仍成立。
        selection = _frozen_shared_embedding(
            task.input, user_id=user_id, project_id=task.project_id, owner_id=owner_id, grant=grant,
        )
        if selection is not None:
            await project_ai_config.revalidate_shared_embedding(
                db, selection, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
                for_update=True, lock_config=True,
            )
        message = await db.scalar(select(AgentMessage).where(
            AgentMessage.id == task.input["message_id"], AgentMessage.conversation_id == cid,
        ))
        if message is None or message.role != "user" or not _canonical_key(message.turn_task_key):
            raise _retry_source_error()
        # 上一次重试可能在排队中已被取消，尚未 build_job；以已持久化消息的实际归属为准。
        source_id = message.turn_task_id
        if source_id != task.id and source_id != task.input.get("retry_source_id"):
            raise _retry_source_error()
        source_key = task.statistics_key if source_id == task.id else task.input.get("_retry_source_key")
        if not _canonical_key(source_key) or message.turn_task_key != source_key:
            raise _retry_source_error()
        await _recover_retry_source(
            db, ctx, source_id=source_id, source_key=source_key, user_id=user_id, project_id=task.project_id,
            conversation_id=cid, message_id=message.id, actor_token_version=actor_token_version, model_binding=binding,
            grant=grant,
        )
        return TaskSpec(
            project_id=task.project_id,
            chapter_id=None,
            resource_key=f"agent:{cid}",
            title=task.title,
            input={**task.input, "ai_config_id": cfg.id, "retry_source_id": source_id,
                   "_retry_source_key": source_key},
        )

    async def revalidate_resume(self, db, ctx, task):
        data = task.input if isinstance(task.input, dict) else {}
        req = data.get("request")
        if (not _canonical_key(task.statistics_key) or not isinstance(req, dict)
                or type(req.get("conversation_id")) is not int or type(data.get("message_id")) is not int):
            raise _retry_source_error()
        grant = _task_grant(data)
        await _require_original_actor(db, task.user_id, data)
        owner_id, conversation = await _lock_turn_conversation(
            db, task.user_id, task.project_id, req["conversation_id"], grant, lock=False,
        )
        await require_no_unknown_external(db, project_id=task.project_id, conversation_id=conversation.id)
        await history.require_open_history(db, conversation, expected_epoch=data.get("history_epoch"))
        binding = project_ai_config.deserialize_project_model(
            data.get("_project_model"), actor_id=task.user_id, project_id=task.project_id,
            project_owner_id=owner_id,
        )
        if binding is None or binding.config_id != data.get("ai_config_id"):
            raise _retry_source_error()
        _check_turn_binding(binding, user_id=task.user_id, project_id=task.project_id, owner_id=owner_id, grant=grant)
        await project_ai_config.revalidate_project_model(
            db, binding, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
            for_update=False, lock_config=True,
        )
        message = await db.scalar(select(AgentMessage.id).where(
            AgentMessage.id == data["message_id"], AgentMessage.conversation_id == conversation.id,
            AgentMessage.role == "user", AgentMessage.turn_task_id == task.id,
            AgentMessage.turn_task_key == task.statistics_key,
        ))
        if message is None:
            raise _retry_source_error()
        await _require_matched_turn_rows(
            db, task_id=task.id, task_key=task.statistics_key,
            conversation_id=conversation.id, project_id=task.project_id,
        )
        pending = await db.scalar(
            select(AgentChange.id)
            .where(AgentChange.turn_task_id == task.id, AgentChange.status.in_(("proposed", "executing")))
            .limit(1)
        )
        if pending is not None:
            raise ConflictError("请先确认或拒绝本轮全部修改")

    def finish_result(self, task):
        raise ConflictError("请确认或拒绝全部修改，或取消本轮智能体任务")

    async def build_job(self, ctx, task, *, execution: TaskExecutionIdentity | None = None):
        if (not isinstance(task.input, dict)
                or not _canonical_key(task.statistics_key) or not _canonical_key(task.execution_key)
                or not isinstance(task.queued_at, datetime) or not isinstance(task.input.get("request"), dict)
                or type(task.input["request"].get("conversation_id")) is not int
                or type(task.input.get("message_id")) is not int or type(execution) is not TaskExecutionIdentity
                or not matches_running(execution, task) or task.admin_cancel_operation_id is not None):
            raise _retry_source_error()
        actor_token_version = _actor_version(task.input)
        if actor_token_version != execution.actor_token_version:
            raise _retry_source_error()
        captured = execution.input
        grant = execution.grant_identity
        if _task_grant(captured) != grant or _task_grant(task.input) != grant:
            raise _retry_source_error()
        # 协作轮次的作者取自领取时冻结的绑定，锁内由 require_write_execution 核对作品作者一致。
        raw_model = captured.get("_project_model")
        owner_id = raw_model.get("project_owner_id") if grant is not None and isinstance(raw_model, dict) else None
        if grant is not None and (type(owner_id) is not int or owner_id <= 0 or owner_id == execution.actor_id):
            raise _retry_source_error()
        binding = project_ai_config.deserialize_project_model(
            raw_model, actor_id=execution.actor_id, project_id=execution.project_id,
            project_owner_id=execution.actor_id if grant is None else owner_id,
        )
        if binding is None or binding.config_id != captured.get("ai_config_id"):
            raise _retry_source_error()
        _check_turn_binding(
            binding, user_id=execution.actor_id, project_id=execution.project_id,
            owner_id=binding.project_owner_id, grant=grant,
        )
        selection = _frozen_shared_embedding(
            captured, user_id=execution.actor_id, project_id=execution.project_id,
            owner_id=binding.project_owner_id, grant=grant,
        )
        if _frozen_shared_embedding(
            task.input, user_id=execution.actor_id, project_id=execution.project_id,
            owner_id=binding.project_owner_id, grant=grant,
        ) != selection:
            raise _retry_source_error()
        async with ctx.sessionmaker() as db:
            db.info["authenticated_actor"] = authorization_service.AuthenticatedActor(task.user_id, actor_token_version)
            await require_write_execution(db, execution, project_owner_id=owner_id)
            req = execution.input["request"]
            locked_owner, conversation = await _lock_turn_conversation(
                db, execution.actor_id, execution.project_id, req["conversation_id"], grant, denied="changed",
            )
            if locked_owner != binding.project_owner_id:
                raise _retry_source_error()
            # SQLite 的 FOR UPDATE 不持有行锁；条件 no-op UPDATE 同时证明并锁定真实执行体。
            matched = await db.execute(update(Task).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
                Task.execution_key == task.execution_key, Task.user_id == task.user_id,
                Task.project_id == task.project_id, Task.kind == "agent.turn", Task.status == "running",
                Task.admin_cancel_operation_id.is_(None),
            ).values(updated_at=Task.updated_at).execution_options(synchronize_session=False))
            if matched.rowcount != 1:
                raise _retry_source_error()
            target = await db.scalar(select(Task).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key,
            ).execution_options(populate_existing=True))
            if target is None or not matches_running(execution, target):
                raise _retry_source_error()
            await require_no_unknown_external(db, project_id=task.project_id, conversation_id=conversation.id)
            stamp = await history.require_open_history(db, conversation, expected_epoch=captured.get("history_epoch"))
            if grant is not None:
                # 核对冻结绑定与原执行身份可形成受控 guard；每轮实际发送再以当前已证明的执行身份
                # 新建 for_task guard 逐次重验（context._summary_guard），撤权后零模型请求。
                collaboration_ai_guard.for_task(
                    execution, binding, stamp, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
                    extra_bindings=(selection,) if selection is not None else (),
                )
                if selection is not None and selection.mode == "vector":
                    # 冻结的作者共享向量须同样能与原执行身份构成受控 guard（检索工具每次发送再重建）。
                    collaboration_ai_guard.for_task(
                        execution, selection, stamp, cipher=ctx.cipher,
                        allow_private=ctx.settings.allow_private_ai_endpoints, extra_bindings=(binding,),
                    )
            if selection is not None and selection.mode == "vector":
                # 与 SendGuard._verify_bindings 同序：先按 ID 升序取全部配置选择门禁，再锁配置行；否则先持文本
                # 配置行共享锁再等作者向量门禁，会与作者配置写入（门禁 → 行）交叉等待。
                for config_owner in sorted({binding.config_owner_id, selection.project_owner_id}):
                    await ai_config_service.lock_config_owner(db, config_owner, write=False)
            await project_ai_config.revalidate_project_model(
                db, binding, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
                for_update=False, lock_config=True,
            )
            if selection is not None:
                # 首次运行与 paused → queued 续跑的每次领取都在本锁内重验冻结的共享向量选择（E4i-c 遗留，E4i-e）：
                # 作者向量配置改动 / 授权变化即在构建时失败关闭，runner 不启动，零模型与作者向量调用。
                await project_ai_config.revalidate_shared_embedding(
                    db, selection, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
                    for_update=False, lock_config=True,
                )
            selected = await _bound_config(db, task.user_id, binding)
            # provider/DNS 可能 await：先冻结整份配置，再结束持锁事务，避免拼接不同代次。
            cfg = AIConfig(**{column.key: getattr(selected, column.key) for column in AIConfig.__table__.columns})
            # 重试仍属同一逻辑轮次：沿用消息和提案，不创建第二条 user 消息。
            old_id = captured.get("retry_source_id")
            message = await db.scalar(select(AgentMessage).where(
                AgentMessage.id == captured["message_id"], AgentMessage.conversation_id == conversation.id,
                AgentMessage.role == "user",
            ).execution_options(populate_existing=True))
            if message is None:
                raise _retry_source_error()
            if old_id is not None:
                old_key = captured.get("_retry_source_key")
                if type(old_id) is not int or not _canonical_key(old_key):
                    raise _retry_source_error()
                if message.turn_task_id == old_id and message.turn_task_key == old_key:
                    await _recover_retry_source(
                        db, ctx, source_id=old_id, source_key=old_key,
                        user_id=task.user_id, project_id=task.project_id, conversation_id=conversation.id,
                        message_id=message.id, actor_token_version=actor_token_version, model_binding=binding,
                        grant=grant,
                    )
                    for model in (AgentMessage, AgentChange):
                        await db.execute(
                            update(model)
                            .where(
                                model.conversation_id == conversation.id,
                                model.turn_task_id == old_id,
                                model.turn_task_key == old_key,
                            )
                            .values(turn_task_id=task.id, turn_task_key=task.statistics_key)
                        )
                elif message.turn_task_id != task.id or message.turn_task_key != task.statistics_key:
                    raise _retry_source_error()
            elif message.turn_task_id != task.id or message.turn_task_key != task.statistics_key:
                # 历史未绑定的消息无法证明来源，不能读取当前任务后补猜。
                raise _retry_source_error()
            await _require_matched_turn_rows(
                db, task_id=task.id, task_key=task.statistics_key,
                conversation_id=conversation.id, project_id=task.project_id,
            )
            await db.refresh(message)
            original_message = message_snapshot(message)
            original_config = model_snapshot(cfg)
            conversation_created_at = conversation.created_at
            await db.commit()
            provider = await ai_config_service.build_provider(
                cfg,
                ctx.cipher,
                chapter_provider_factory(ctx.factory),
                ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
            )
            return AgentJob(
                execution.task_id, execution.actor_id, execution.project_id, req["conversation_id"],
                captured["message_id"], cfg,
                guard_provider(provider, stamp, ctx.sessionmaker), stamp,
                task_key=execution.task_key, queued_at=execution.queued_at, execution_key=execution.execution_key,
                actor_token_version=actor_token_version, execution=execution,
                execution_state=_AgentExecutionState(execution),
                model_snapshot=original_config, conversation_created_at=conversation_created_at,
                message_snapshot=original_message,
                model_binding=binding, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
            )

    async def verify_event(self, db, task, *, job, execution, current_execution, event, data):
        if (not same_execution(execution, current_execution) or current_execution != job.task_execution_identity
                or not matches_running(current_execution, task)):
            raise _retry_source_error()
        await verify_job_resources(db, job)
        await require_no_unknown_external(db, project_id=job.project_id, conversation_id=job.conversation_id)

    def stream_guard(
        self, job, *, execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data,
    ) -> collaboration_ai_guard.SendGuard:
        """把本执行刚通过 verify_event 的断点状态绑定成仅发布 guard 交给中央 ASGI 帧租约；不补签、不可发送。"""
        if (type(job) is not AgentJob or job.execution_state is None or job.execution != execution
                or not same_execution(execution, current_execution)
                or current_execution != job.task_execution_identity
                or job.model_binding is None or job.cipher is None or job.stamp is None):
            raise collaboration_ai_guard.CollaborationSendBlocked()
        extras = agent_access.text_guard_extras(current_execution, job.model_binding)
        inputs = (current_execution, job.model_binding, job.stamp, job.cipher, job.allow_private, extras)
        cached = job.execution_state.publication_guard
        if cached is not None and cached[0] == inputs:
            return cached[1]
        guard = collaboration_ai_guard.for_task(
            current_execution, job.model_binding, job.stamp,
            cipher=job.cipher, allow_private=job.allow_private, extra_bindings=extras, publication_only=True,
        )
        job.execution_state.publication_guard = (inputs, guard)
        return guard

    async def verify_finalization(self, db, task, *, job, execution, current_execution, status, result, text):
        await self.verify_event(
            db, task, job=job, execution=execution, current_execution=current_execution, event=status, data=result,
        )
        checkpoint = current_execution.checkpoint
        if not isinstance(checkpoint, dict) or not isinstance(result, dict):
            raise _retry_source_error()
        parts = list(await db.scalars(select(AgentMessage.content).where(
            AgentMessage.conversation_id == job.conversation_id, AgentMessage.turn_task_id == job.task_id,
            AgentMessage.turn_task_key == job.task_key, AgentMessage.role == "assistant",
        ).order_by(AgentMessage.id).with_for_update()))
        if not isinstance(text, str) or text != "".join(parts):
            raise _retry_source_error()
        if status == "succeeded":
            message_id = checkpoint.get("finished_message_id")
            if (type(message_id) is not int or type(result.get("message_id")) is not int
                    or result["message_id"] != message_id):
                raise _retry_source_error()
            message = await db.scalar(select(AgentMessage.id).where(
                AgentMessage.id == message_id, AgentMessage.conversation_id == job.conversation_id,
                AgentMessage.turn_task_id == job.task_id, AgentMessage.turn_task_key == job.task_key,
                AgentMessage.role == "assistant",
            ).with_for_update())
            if message is None:
                raise _retry_source_error()
        elif status == "paused":
            pending = checkpoint.get("pending_change_ids")
            result_ids = result.get("change_ids")
            if (not isinstance(pending, list) or not pending or any(type(value) is not int for value in pending)
                    or len(set(pending)) != len(pending) or not isinstance(result_ids, list)
                    or any(type(value) is not int for value in result_ids) or result_ids != pending
                    or result.get("reason") != "confirm"):
                raise _retry_source_error()
            found = set(await db.scalars(select(AgentChange.id).where(
                AgentChange.id.in_(pending), AgentChange.conversation_id == job.conversation_id,
                AgentChange.project_id == job.project_id, AgentChange.turn_task_id == job.task_id,
                AgentChange.turn_task_key == job.task_key, AgentChange.status == "proposed",
            ).with_for_update()))
            if found != set(pending):
                raise _retry_source_error()
        else:
            raise _retry_source_error()

    def run(self, job, ctx):
        return run_agent(job, ctx)

    def progress(self, task, text):
        return min(90, 5 + len(text) // 100)

    async def on_abort(self, ctx, task, *, execution: TaskExecutionIdentity | None = None):
        # 每个工具效果与状态同事务；取消只停止后续步骤，已执行效果保留且可撤销。
        # 协作轮次因授权失效而停止（运行中检查点 / 撤权复查取消 / 管理员取消待确认轮次）时，作废本轮待确认提案
        # （E4-i 裁决 11）；授权仍有效（用户取消、普通失败）不动，保留同轮次重试复用提案的既有语义。作者轮次不受影响。
        data = getattr(task, "input", None)
        if (not isinstance(data, dict) or GRANT_KEY not in data or task.project_id is None
                or not _canonical_key(task.statistics_key)):
            return None
        try:
            await _void_revoked_turn(ctx, task, execution=execution)
        except Exception:  # noqa: BLE001  作废失败不阻断收尾：轮次结束后不再是 paused，残留 proposed 也不能确认。
            logger.error("协作轮次提案作废未完成 task_key=%s", task.statistics_key)
        return None

    async def finalize_revoked_pause(self, ctx, task, *, execution: TaskExecutionIdentity) -> str | None:
        """暂停终态核验因协作授权失效而失败（断点已提交、paused 未提交）时，引擎据此受限收尾为 failed。

        与 on_abort 相同的锁内复核与作废；返回失效原因，授权仍有效 / 无法证明时 None（保持原屏障）。
        """
        data = getattr(task, "input", None)
        if (not isinstance(data, dict) or GRANT_KEY not in data or task.project_id is None
                or not _canonical_key(task.statistics_key)):
            return None
        return await _void_revoked_turn(ctx, task, execution=execution)


register(AgentHandler())
