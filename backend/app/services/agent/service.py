"""会话与审核入口：业务效果和提案状态始终在同一事务中持久化。"""

import asyncio
import logging
from datetime import UTC, datetime
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
from app.services import project_service, task_service
from app.services.agent import effects, transactions
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import ToolCtx
from app.services.task_engine.registry import TaskContext

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)
_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def conversation_lock(conversation_id: int) -> asyncio.Lock:
    # SQLite 无 FOR UPDATE；单进程任务引擎再以会话锁串行化 HTTP 操作。
    lock = _locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[conversation_id] = lock
    return lock


async def owned_conversation(
    db: AsyncSession, user_id: int, project_id: int, conversation_id: int
) -> AgentConversation:
    await project_service.get_owned_project(db, user_id, project_id)
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
    await project_service.get_owned_project(db, user_id, project_id)
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
    row = await owned_conversation(db, user_id, project_id, conversation_id)
    if req.title is not None:
        if not req.title.strip():
            raise BadRequestError("会话标题不能为空")
        row.title = req.title.strip()
    if req.auto_execute is not None:
        row.auto_execute = req.auto_execute
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
        row = await owned_conversation(db, user_id, project_id, conversation_id)
        if await active_turn(db, row.id):
            raise ConflictError("会话有进行中或待确认的轮次，无法删除")
        await db.delete(row)
        await db.commit()


async def send_message(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, conversation_id: int, req: SendMessage
) -> MessageAccepted:
    async with conversation_lock(conversation_id):
        row = await owned_conversation(db, user_id, project_id, conversation_id)
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
        stored = await db.get(Task, task.id)
        message = await db.get(AgentMessage, stored.input["message_id"])
        message.turn_task_id = task.id
        await db.commit()
        await db.refresh(message)
        return MessageAccepted(message=MessageOut.model_validate(message), task=task)


async def owned_change(db: AsyncSession, user_id: int, project_id: int, change_id: int) -> AgentChange:
    await project_service.get_owned_project(db, user_id, project_id)
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


async def _turn(db: AsyncSession, user_id: int, project_id: int, task_id: int) -> Task:
    await project_service.get_owned_project(db, user_id, project_id)
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
    project = await project_service.get_owned_project(db, user_id, change.project_id)
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


async def maybe_resume(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int | None) -> None:
    if task_id is None:
        return
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
    initial = await owned_change(db, user_id, project_id, change_id)
    cid = initial.conversation_id
    await db.rollback()  # 在等待进程锁前释放只读事务和旧快照。
    async with conversation_lock(cid):
        change = await owned_change(db, user_id, project_id, change_id)
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
    await _turn(db, user_id, project_id, task_id)
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
            changes.append(ChangeOut.model_validate(await owned_change(db, user_id, project_id, change_id)))
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
    if any(row.kind == "task" for row in rows):
        raise ConflictError("已启动的任务不能撤销，请在任务面板取消")
    memory_changed = False
    try:
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
    initial = await owned_change(db, user_id, project_id, change_id)
    cid = initial.conversation_id
    await db.rollback()
    async with conversation_lock(cid):
        row = await owned_change(db, user_id, project_id, change_id)
        if row.status == "undone":
            return ChangeOut.model_validate(row)
        task = await active_turn(db, cid)
        if task and task.status != "paused":
            raise ConflictError("请等待智能体暂停或结束后撤销")
        return (await _undo_changes(db, user_id, project_id, [row], engine))[0]


async def undo_turn(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, task_id: int
) -> TurnChangesOut:
    task = await _turn(db, user_id, project_id, task_id)
    cid = task.input["request"]["conversation_id"]
    await db.rollback()
    async with conversation_lock(cid):
        await _turn(db, user_id, project_id, task_id)
        active = await active_turn(db, cid)
        if active and active.status != "paused":
            raise ConflictError("请等待智能体暂停或结束后撤销")
        rows = list(
            await db.scalars(
                select(AgentChange)
                .where(
                    AgentChange.turn_task_id == task_id,
                    AgentChange.status == "executed",
                    AgentChange.kind != "task",
                )
                .order_by(AgentChange.seq.desc(), AgentChange.id.desc())
            )
        )
        changes = await _undo_changes(db, user_id, project_id, rows, engine)
    return TurnChangesOut(changes=changes, task=await task_service.get_task(db, engine, user_id, task_id))
