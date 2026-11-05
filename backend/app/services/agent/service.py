"""会话与审核入口：业务效果和提案状态始终在同一事务中持久化。"""

import asyncio
import logging
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import AgentChange, AgentConversation, AgentMessage, GenerationLog, Task
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.agent import (
    ChangeOut,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageAccepted,
    MessageOut,
    SendMessage,
    TurnChangesOut,
)
from app.schemas.chapter_version import GenerationLogOut
from app.services import project_lifecycle, project_service, task_service
from app.services.agent import effects, mcp_tools, transactions
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import ToolCtx
from app.services.task_engine.registry import TaskContext

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)
_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


# 外部（MCP）调用先提交 executing 再发起网络请求；进程在调用期间崩溃或请求被取消时提案会停在 executing。
# 单次调用最长约 CALL_TIMEOUT + CONNECT_TIMEOUT（45 秒）加关闭连接的 5 秒，超过 2 分钟仍是 executing 的视为中断。
EXTERNAL_STALE_AFTER = timedelta(minutes=2)
EXTERNAL_INTERRUPTED = "执行中断，结果未知，请检查外部系统"


def _aware(value: datetime) -> datetime:
    # SQLite 读出的是 naive UTC
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def recover_external(db: AsyncSession, *, task_id: int | None = None, stale_only: bool = True) -> int:
    """把中断的外部调用（kind=external、仍为 executing）标为 failed，返回处理的条数；不 commit。

    只改状态、不重新调用外部服务（结果未知，重放可能重复执行发送类操作）。stale_only=False 用于进程启动：
    单进程部署下启动时不可能有正在进行的确认请求。
    """
    stmt = select(AgentChange.id, AgentChange.executed_at, AgentChange.updated_at).where(
        AgentChange.kind == "external", AgentChange.status == "executing"
    )
    if task_id is not None:
        stmt = stmt.where(AgentChange.turn_task_id == task_id)
    cutoff = datetime.now(UTC) - EXTERNAL_STALE_AFTER
    ids = [
        row.id
        for row in await db.execute(stmt)
        if not stale_only or _aware(row.executed_at or row.updated_at) <= cutoff
    ]
    if not ids:
        return 0
    result = await db.execute(
        update(AgentChange)
        .where(AgentChange.id.in_(ids), AgentChange.status == "executing")
        .values(status="failed", error=EXTERNAL_INTERRUPTED, result={"error": EXTERNAL_INTERRUPTED})
    )
    if result.rowcount:
        logger.warning("已将 %s 项中断的外部调用标记为失败：%s", result.rowcount, ids)
    return result.rowcount or 0


def conversation_lock(conversation_id: int) -> asyncio.Lock:
    # SQLite 无 FOR UPDATE；单进程任务引擎再以会话锁串行化 HTTP 操作。
    lock = _locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[conversation_id] = lock
    return lock


async def owned_conversation(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int, *, lock: bool = False
) -> AgentConversation:
    await project_service.get_owned_project(db, user_id, project_id, lock=lock)
    row = await db.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.id == conversation_id,
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError("会话不存在")
    return row


async def active_turn(db: AsyncSession, conversation_id: int) -> Task | None:
    return await db.scalar(
        select(Task)
        .where(
            Task.resource_key == f"agent:{conversation_id}",
            Task.status.in_(OPEN_TASK_STATUSES),
        )
        .order_by(Task.id.desc())
        .limit(1)
    )


async def list_conversations(db: AsyncSession, user_id: int, project_id: int) -> list[ConversationOut]:
    await project_service.get_owned_project(db, user_id, project_id)
    rows = await db.scalars(
        select(AgentConversation)
        .where(
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
        )
        .order_by(AgentConversation.last_message_at.desc(), AgentConversation.id.desc())
    )
    return [ConversationOut.model_validate(row) for row in rows]


async def create_conversation(
    db: AsyncSession, user_id: int, project_id: int, req: ConversationCreate
) -> ConversationOut:
    await project_service.get_owned_project(db, user_id, project_id, lock=True)
    previous = await db.scalar(
        select(AgentConversation)
        .where(
            AgentConversation.project_id == project_id,
            AgentConversation.user_id == user_id,
        )
        .order_by(AgentConversation.id.desc())
        .limit(1)
    )
    row = AgentConversation(
        user_id=user_id,
        project_id=project_id,
        title=req.title.strip(),
        auto_execute=previous.auto_execute if previous else False,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return ConversationOut.model_validate(row)


async def get_conversation(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, conversation_id: int
) -> ConversationDetail:
    row = await owned_conversation(db, user_id, project_id, conversation_id)
    messages = await db.scalars(
        select(AgentMessage).where(AgentMessage.conversation_id == row.id).order_by(AgentMessage.id)
    )
    changes = await db.scalars(
        select(AgentChange).where(AgentChange.conversation_id == row.id).order_by(AgentChange.id)
    )
    task = await active_turn(db, row.id)
    return ConversationDetail(
        **ConversationOut.model_validate(row).model_dump(),
        messages=[MessageOut.model_validate(m) for m in messages],
        changes=[ChangeOut.model_validate(c) for c in changes],
        active_task=await task_service.task_out(db, engine, task) if task else None,
    )


async def update_conversation(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int, req: ConversationUpdate
) -> ConversationOut:
    row = await owned_conversation(db, user_id, project_id, conversation_id, lock=True)
    if req.title is not None:
        if not req.title.strip():
            raise BadRequestError("会话标题不能为空")
        row.title = req.title.strip()
    if req.auto_execute is not None:
        row.auto_execute = req.auto_execute
    if req.mcp_plugin_ids is not None:
        from app.models import McpPlugin

        ids = list(dict.fromkeys(req.mcp_plugin_ids))
        owned = set(await db.scalars(select(McpPlugin.id).where(McpPlugin.id.in_(ids), McpPlugin.user_id == user_id)))
        if len(owned) != len(ids):
            raise NotFoundError("插件不存在")
        row.mcp_plugin_ids = ids or None
    await db.commit()
    await db.refresh(row)
    return ConversationOut.model_validate(row)


async def list_generation_logs(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int
) -> list[GenerationLogOut]:
    await owned_conversation(db, user_id, project_id, conversation_id)
    rows = await db.scalars(
        select(GenerationLog)
        .where(
            GenerationLog.project_id == project_id,
            GenerationLog.kind == "agent.turn",
            GenerationLog.params["conversation_id"].as_integer() == conversation_id,
        )
        .order_by(GenerationLog.id.desc())
    )

    def public_calls(calls: list[dict]) -> list[dict]:
        return [{key: call[key] for key in ("id", "name", "arguments") if key in call} for call in calls]

    result = []
    for row in rows:
        log = GenerationLogOut.model_validate(row)
        # 完整原生签名只用于 provider 重放；生成记录与消息详情一样隐藏 thinking 等元数据。
        log.params = {**log.params}
        if isinstance(log.params.get("tool_calls"), list):
            log.params["tool_calls"] = public_calls(log.params["tool_calls"])
        log.messages = [
            {**message, "tool_calls": public_calls(message["tool_calls"])} if message.get("tool_calls") else message
            for message in log.messages
        ]
        result.append(log)
    return result


async def delete_conversation(db: AsyncSession, user_id: int, project_id: int, conversation_id: int) -> None:
    async with conversation_lock(conversation_id):
        row = await owned_conversation(db, user_id, project_id, conversation_id, lock=True)
        if await active_turn(db, row.id):
            raise ConflictError("会话有进行中或待确认的轮次，无法删除")
        await db.delete(row)
        await db.commit()


async def send_message(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, conversation_id: int, req: SendMessage
) -> MessageAccepted:
    async with conversation_lock(conversation_id):
        row = await owned_conversation(db, user_id, project_id, conversation_id, lock=True)
        # handler 内再次持锁校验，避免其他创建入口绕过约束。
        if await active_turn(db, row.id):
            raise ConflictError("当前会话已有进行中或待确认的轮次")
        task = await task_service.create_task(
            db,
            engine,
            user_id,
            "agent.turn",
            {
                **req.model_dump(mode="json"),
                "project_id": project_id,
                "conversation_id": conversation_id,
            },
        )
        await project_service.get_owned_project(db, user_id, project_id, lock=True)
        stored = await db.get(Task, task.id)
        message = await db.get(AgentMessage, stored.input["message_id"])
        message.turn_task_id = task.id
        await db.commit()
        await db.refresh(message)
        return MessageAccepted(message=MessageOut.model_validate(message), task=task)


async def owned_change(
    db: AsyncSession, user_id: int, project_id: int, change_id: int, *, lock: bool = False
) -> AgentChange:
    await project_service.get_owned_project(db, user_id, project_id, lock=lock)
    change = await db.scalar(
        select(AgentChange)
        .join(
            AgentConversation,
            AgentChange.conversation_id == AgentConversation.id,
        )
        .where(AgentChange.id == change_id, AgentChange.project_id == project_id, AgentConversation.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if change is None:
        raise NotFoundError("修改提案不存在")
    return change


async def _turn(db: AsyncSession, user_id: int, project_id: int, task_id: int, *, lock: bool = False) -> Task:
    await project_service.get_owned_project(db, user_id, project_id, lock=lock)
    task = await task_service.get_owned_task(db, user_id, task_id)
    if task.kind != "agent.turn" or task.project_id != project_id:
        raise NotFoundError("智能体轮次不存在")
    return task


async def execute_change(
    db: AsyncSession, task_context: TaskContext, change: AgentChange, *, user_id: int, compare_preview: bool = True
) -> AgentChange:
    """调用方负责会话互斥与提交；savepoint 保证返回 error 的工具也不会留下部分修改。"""
    if change.status != "proposed":
        return change
    await project_service.get_owned_project(db, user_id, change.project_id, lock=True)
    if change.kind == "external":
        return await _execute_external(db, task_context, change, user_id=user_id, compare_preview=compare_preview)
    project = await project_service.get_owned_project(db, user_id, change.project_id, lock=True)
    tool = get_tool(change.tool_name)
    if tool is None:
        raise BadRequestError("工具不存在")
    ctx = ToolCtx(db, project, user_id, task_context)
    fresh = await tool.preview(ctx, change.arguments)
    if "error" in fresh:
        change.status, change.error, change.result = "failed", fresh["error"], fresh
        await db.flush()
        return change
    if compare_preview and fresh != change.preview:
        change.preview = fresh
        await db.commit()
        await db.refresh(change)
        raise ConflictError(
            "数据已经变化，请检查最新预览后重新确认",
            extra={
                "code": "preview_changed",
                "change": ChangeOut.model_validate(change).model_dump(mode="json"),
            },
        )
    claimed = await db.execute(
        update(AgentChange)
        .where(
            AgentChange.id == change.id,
            AgentChange.status == "proposed",
        )
        .values(status="executing")
    )
    if claimed.rowcount != 1:
        raise ConflictError("修改正在处理中，请刷新后重试")
    change.status = "executing"
    failure = None
    try:
        async with db.begin_nested():
            before = await effects.snapshot(db, project.id)
            result = await tool.execute(ctx, change.arguments)
            if "error" in result:
                raise BadRequestError(str(result["error"]))
            await db.flush()
            after = await effects.snapshot(db, project.id)
            captured = effects.diff(before, after)
            # 正文版本效果由写工具提供；不向模型 / 面板泄露内部快照辅助字段。
            if change.tool_name == "replace_chapter_text":
                captured.append(
                    effects.chapter_text_effect(
                        **{
                            key: result[key]
                            for key in (
                                "chapter_id",
                                "before_version_id",
                                "after_version_id",
                                "before_hash",
                                "after_hash",
                            )
                        }
                    )
                )
            captured.extend(result.pop("_effects", []))
    except AppError as exc:
        failure = exc.detail
    except DBAPIError as exc:
        await transactions.rollback_conflict(db, exc)
    except Exception:  # noqa: BLE001
        logger.exception("智能体提案 %s 执行失败", change.id)
        failure = "修改执行失败：服务器内部错误"
    if failure:
        change.status, change.error, change.result = "failed", failure, {"error": failure}
    else:
        change.status, change.error = "executed", ""
        change.result, change.effects = result, captured
        change.executed_at = datetime.now(UTC)
    await db.flush()
    return change


def _preview_changed(change: AgentChange) -> ConflictError:
    return ConflictError(
        "数据已经变化，请检查最新预览后重新确认",
        extra={"code": "preview_changed", "change": ChangeOut.model_validate(change).model_dump(mode="json")},
    )


async def _execute_external(db, task_context, change, *, user_id, compare_preview):
    task = await db.get(Task, change.turn_task_id) if change.turn_task_id else None
    engine = task_context.task_engine
    scope = engine.cleanup_scope(task) if engine is not None and task is not None else nullcontext()
    with scope:
        return await _execute_external_impl(db, task_context, change, user_id=user_id, compare_preview=compare_preview)


async def _execute_external_impl(
    db: AsyncSession, task_context: TaskContext, change: AgentChange, *, user_id: int, compare_preview: bool
) -> AgentChange:
    """外部（MCP）调用：重新读取插件配置生成预览比较；一致才在本请求内调用（单独连接，30 秒超时）。

    先提交 executing 再发起网络调用，避免在调用期间持有数据库写锁；结果由调用方提交。
    """
    task = await db.get(Task, change.turn_task_id) if change.turn_task_id else None
    tool_map = ((task.checkpoint or {}) if task is not None else {}).get("mcp_tool_map") or {}
    entry = tool_map.get(change.tool_name)
    if entry is None:
        change.status, change.error, change.result = "failed", mcp_tools.CHANGED, {"error": mcp_tools.CHANGED}
        await db.flush()
        return change
    settings = task_context.settings
    resolved = await mcp_tools.resolve(db, settings, user_id, change.conversation_id, tool_map, change.tool_name)
    fresh = mcp_tools.preview(entry, resolved, change.arguments)
    if compare_preview and fresh != change.preview:
        change.preview = fresh
        await db.commit()
        await db.refresh(change)
        raise _preview_changed(change)
    failure = None
    if resolved is None:
        failure = mcp_tools.CHANGED
    elif (invalid := mcp_tools.validate(resolved, change.arguments)) is not None:
        failure = invalid["error"]
    if failure:
        change.status, change.error, change.result = "failed", failure, {"error": failure}
        await db.flush()
        return change
    # executing 期间 executed_at 记录开始调用的时间（完成时改为完成时间），供 recover_external 判断是否中断；
    # 不用 updated_at：重试时把提案移到新任务的 UPDATE 会刷新它
    claimed = await db.execute(
        update(AgentChange)
        .where(AgentChange.id == change.id, AgentChange.status == "proposed")
        .values(status="executing", executed_at=func.now())
    )
    if claimed.rowcount != 1:
        raise ConflictError("修改正在处理中，请刷新后重试")
    await db.commit()
    sessions = mcp_tools.McpSessions(
        task_context.mcp_connector,
        task_context.cipher,
        settings.allow_private_ai_endpoints,
        settings=settings,
    )
    try:
        data = await mcp_tools.call_data(sessions, resolved, change.arguments)
    except Exception:  # noqa: BLE001
        # 不知道外部服务是否已执行：按中断处理，不重试
        logger.exception("外部调用提案 %s 执行失败", change.id)
        data = {"error": EXTERNAL_INTERRUPTED}
    finally:
        await sessions.aclose()
    project_id, change_id = change.project_id, change.id
    try:
        await project_service.get_owned_project(db, user_id, project_id, lock=True)
    except (ConflictError, NotFoundError):
        # 远端已可能执行，不可撤回；在删除协调等待的请求收尾内保存“结果未知”，不续跑。
        await db.rollback()
        try:
            parent = await project_lifecycle.lock_record(db, project_id)
        except NotFoundError:
            raise ConflictError("作品已不可访问，外部调用结果不再写入") from None
        if parent.deleted_at is None:
            row = await db.get(AgentChange, change_id, populate_existing=True)
            if row is not None and row.status == "executing":
                row.status, row.error, row.result = "failed", EXTERNAL_INTERRUPTED, {"error": EXTERNAL_INTERRUPTED}
                await db.commit()
        raise ConflictError("作品正在删除，外部调用结果不再应用") from None
    change = await db.get(AgentChange, change_id, populate_existing=True)
    if change.status != "executing":
        # 调用期间已被 recover_external 判为中断（只会发生在调用异常地超过时限时）：保留已告知模型的状态
        logger.warning("外部调用提案 %s 完成时已不是 executing（%s），结果不再写入", change.id, change.status)
        return change
    if "error" in data:
        change.status, change.error, change.result = "failed", str(data["error"]), data
    else:
        change.status, change.error, change.result, change.effects = "executed", "", data, []
        change.executed_at = datetime.now(UTC)
    await db.flush()
    return change


async def maybe_resume(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int | None) -> None:
    if task_id is None:
        return
    linked = await db.get(Task, task_id)
    if linked is not None and linked.project_id is not None:
        await project_service.get_owned_project(db, user_id, linked.project_id, lock=True)
    if await recover_external(db, task_id=task_id):
        await db.commit()
    remaining = await db.scalar(
        select(func.count(AgentChange.id)).where(
            AgentChange.turn_task_id == task_id, AgentChange.status.in_(("proposed", "executing"))
        )
    )
    task = await db.get(Task, task_id, populate_existing=True)
    if not remaining and task is not None and task.status == "paused":
        await task_service.resume_task(db, engine, user_id, task.id)


async def decide_change(
    db: AsyncSession,
    engine: "TaskEngine",
    user_id: int,
    project_id: int,
    change_id: int,
    *,
    confirm: bool,
    resume: bool = True,
) -> ChangeOut:
    initial = await owned_change(db, user_id, project_id, change_id, lock=True)
    cid, turn_id = initial.conversation_id, initial.turn_task_id
    await db.rollback()  # 在等待进程锁前释放只读事务和旧快照。
    async with conversation_lock(cid):
        await owned_change(db, user_id, project_id, change_id, lock=True)
        # 中断的外部调用（进程崩溃 / 请求被取消时停在 executing）先判为失败，否则本轮永远无法继续
        if turn_id is not None and await recover_external(db, task_id=turn_id):
            await db.commit()
        change = await owned_change(db, user_id, project_id, change_id, lock=True)
        await db.execute(select(AgentConversation.id).where(AgentConversation.id == cid).with_for_update())
        if change.status == "executing":
            raise ConflictError("修改正在处理中，请刷新后重试")
        if change.status == "proposed":
            task = await db.get(Task, change.turn_task_id) if change.turn_task_id else None
            if task is None or task.status != "paused":
                raise ConflictError("请等待本轮暂停确认，或先重试中断的轮次")
            if confirm:
                await execute_change(db, engine.ctx, change, user_id=user_id)
            else:
                change.status, change.result = "rejected", {"message": "用户拒绝了该修改"}
            await db.commit()
            engine.notify()
            await db.refresh(change)
        out = ChangeOut.model_validate(change)
        if resume:
            await maybe_resume(db, engine, user_id, change.turn_task_id)
        return out


async def decide_turn(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, task_id: int, *, confirm: bool
) -> TurnChangesOut:
    await _turn(db, user_id, project_id, task_id, lock=True)
    ids = list(
        await db.scalars(
            select(AgentChange.id)
            .where(
                AgentChange.turn_task_id == task_id,
                AgentChange.status == "proposed",
            )
            .order_by(AgentChange.seq, AgentChange.id)
        )
    )
    changes, error = [], None
    for change_id in ids:
        try:
            change = await decide_change(db, engine, user_id, project_id, change_id, confirm=confirm, resume=False)
        except AppError as exc:
            error = exc.detail
            changes.append(ChangeOut.model_validate(await owned_change(db, user_id, project_id, change_id, lock=True)))
            break
        changes.append(change)
        if change.status == "failed":
            error = change.error
            break
    await maybe_resume(db, engine, user_id, task_id)
    task = await task_service.get_task(db, engine, user_id, task_id)
    return TurnChangesOut(changes=changes, task=task, error=error)


async def _undo_changes(
    db: AsyncSession, user_id: int, project_id: int, rows: list[AgentChange], engine: "TaskEngine | None" = None
) -> list[ChangeOut]:
    await project_service.get_owned_project(db, user_id, project_id, lock=True)
    if any(row.kind == "external" for row in rows):
        raise BadRequestError("外部操作无法撤销")
    if any(row.kind == "task" for row in rows):
        raise ConflictError("已启动的任务不能撤销，请在任务面板取消")
    memory_changed = False
    try:
        # 整轮先预检，再开始恢复；旧记录不能导致前几项先执行副作用。
        effects.validate_effect_versions([effect for row in rows for effect in row.effects])
        for row in rows:
            if row.status != "executed":
                raise ConflictError("只有已执行的修改可以撤销")
            memory_changed = await effects.undo(db, project_id, row.effects) or memory_changed
            row.status, row.undone_at = "undone", datetime.now(UTC)
            db.add(
                AgentMessage(
                    conversation_id=row.conversation_id,
                    turn_task_id=row.turn_task_id,
                    role="tool",
                    content=f"用户撤销了：{row.preview.get('title', row.tool_name)}",
                )
            )
            await db.flush()
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    if memory_changed and engine is not None:
        from app.services import kb_index_service

        await kb_index_service.enqueue_memory(engine.ctx.sessionmaker, project_id)
        engine.notify()
    for row in rows:
        await db.refresh(row)
    return [ChangeOut.model_validate(row) for row in rows]


async def undo_change(
    db: AsyncSession, user_id: int, project_id: int, change_id: int, *, engine: "TaskEngine | None" = None
) -> ChangeOut:
    initial = await owned_change(db, user_id, project_id, change_id, lock=True)
    cid = initial.conversation_id
    await db.rollback()
    async with conversation_lock(cid):
        row = await owned_change(db, user_id, project_id, change_id, lock=True)
        if row.status == "undone":
            return ChangeOut.model_validate(row)
        task = await active_turn(db, cid)
        if task and task.status != "paused":
            raise ConflictError("请等待智能体暂停或结束后撤销")
        return (await _undo_changes(db, user_id, project_id, [row], engine))[0]


async def undo_turn(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, task_id: int
) -> TurnChangesOut:
    task = await _turn(db, user_id, project_id, task_id, lock=True)
    cid = task.input["request"]["conversation_id"]
    await db.rollback()
    async with conversation_lock(cid):
        await _turn(db, user_id, project_id, task_id, lock=True)
        active = await active_turn(db, cid)
        if active and active.status != "paused":
            raise ConflictError("请等待智能体暂停或结束后撤销")
        rows = list(
            await db.scalars(
                select(AgentChange)
                .where(
                    AgentChange.turn_task_id == task_id,
                    AgentChange.status == "executed",
                    AgentChange.kind.not_in(("task", "external")),
                )
                .order_by(AgentChange.seq.desc(), AgentChange.id.desc())
            )
        )
        skipped = await db.scalar(
            select(func.count(AgentChange.id)).where(
                AgentChange.turn_task_id == task_id,
                AgentChange.status == "executed",
                AgentChange.kind == "external",
            )
        )
        changes = await _undo_changes(db, user_id, project_id, rows, engine)
    return TurnChangesOut(
        changes=changes,
        task=await task_service.get_task(db, engine, user_id, task_id),
        notice=f"已跳过 {skipped} 项外部操作（无法撤销）" if skipped else None,
    )
