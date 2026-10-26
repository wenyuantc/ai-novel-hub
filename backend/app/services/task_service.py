"""后台任务的 API 入口：创建、列表、详情、取消、重试、删除、清理与保留裁剪。

所有入口按 user_id 校验归属；他人任务与不存在统一返回 404。
"""

import bisect
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.task_engine.handlers  # noqa: F401  导入即注册内置任务类型
from app.core.errors import ConflictError, NotFoundError
from app.models import Task
from app.models.entities import ACTIVE_TASK_STATUSES, FINISHED_TASK_STATUSES
from app.schemas.task import TaskOut
from app.services.task_engine.registry import get_handler

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

# 每个用户保留的已结束任务数
MAX_FINISHED_PER_USER = 200
RETRYABLE_STATUSES = ("failed", "cancelled", "interrupted")
STAGE_LABELS = {
    "queued": "排队中",
    "running": "生成中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
ACTIVE_CONFLICT = "已有任务在进行中，请等待其完成后再试"


# ---------------- 序列化 ----------------


def _request_int(task: Task, key: str) -> int | None:
    request = (task.input or {}).get("request") if isinstance(task.input, dict) else None
    value = request.get(key) if isinstance(request, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _subject_character_id(task: Task) -> int | None:
    return _request_int(task, "character_id")


def _revise_analysis_id(task: Task) -> int | None:
    return _request_int(task, "analysis_id") if task.kind == "chapter.revise" else None


def to_out(task: Task, *, queue_position: int | None = None, engine: "TaskEngine | None" = None) -> TaskOut:
    out = TaskOut.model_validate(task)
    update_fields: dict[str, Any] = {
        "queue_position": queue_position if task.status == "queued" else None,
        "character_id": _subject_character_id(task),
        "analysis_id": _revise_analysis_id(task),
        "outline_id": _request_int(task, "outline_id") if task.kind == "outline.expand" else None,
    }
    hub = engine.hubs.get(task.id) if engine is not None else None
    if hub is not None and task.status in ACTIVE_TASK_STATUSES:
        # 进度 / 阶段以内存中的实时值为准（库里是节流刷新的）
        update_fields.update(progress=hub.progress, stage=hub.stage or out.stage)
    return out.model_copy(update=update_fields)


def out_json(task: Task, *, queue_position: int | None = None) -> dict[str, Any]:
    return to_out(task, queue_position=queue_position).model_dump(mode="json")


async def _queued_ids(db: AsyncSession) -> list[int]:
    return list(await db.scalars(select(Task.id).where(Task.status == "queued").order_by(Task.id)))


async def queue_position(db: AsyncSession, task: Task) -> int | None:
    if task.status != "queued":
        return None
    return await db.scalar(select(func.count(Task.id)).where(Task.status == "queued", Task.id < task.id))


async def task_out(db: AsyncSession, engine: "TaskEngine", task: Task) -> TaskOut:
    return to_out(task, queue_position=await queue_position(db, task), engine=engine)


async def out_json_live(db: AsyncSession, engine: "TaskEngine", task: Task) -> dict[str, Any]:
    return (await task_out(db, engine, task)).model_dump(mode="json")


# ---------------- 创建 / 重试 ----------------


async def create_task(
    db: AsyncSession,
    engine: "TaskEngine",
    user_id: int,
    kind: str,
    params: dict[str, Any],
    *,
    retry_of: Task | None = None,
) -> TaskOut:
    """同步校验并抢占资源，与任务插入同一事务提交；同资源已有活动任务 → 409。"""
    handler = get_handler(kind)
    spec = await handler.validate_and_claim(db, engine.ctx, user_id=user_id, kind=kind, params=params)
    task = Task(
        user_id=user_id,
        project_id=spec.project_id,
        chapter_id=spec.chapter_id,
        kind=kind,
        title=spec.title[:200],
        resource_key=spec.resource_key,
        serial_key=spec.serial_key,
        status="queued",
        input=spec.input,
        stage=STAGE_LABELS["queued"],
        retry_of_id=retry_of.id if retry_of is not None else None,
        step_cursor=retry_of.step_cursor if retry_of is not None else 0,
    )
    db.add(task)
    try:
        await db.commit()
    except IntegrityError as exc:
        # 部分唯一索引 uq_tasks_active_resource 兜底：同资源并发提交（章节抢占随回滚撤销）
        await db.rollback()
        raise ConflictError(ACTIVE_CONFLICT) from exc
    await db.refresh(task)
    engine.ensure_hub(task.id, status="queued", stage=STAGE_LABELS["queued"])
    engine.notify()
    return await task_out(db, engine, task)


async def retry_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    task = await get_owned_task(db, user_id, task_id)
    if task.status not in RETRYABLE_STATUSES:
        raise ConflictError("只有失败、已取消或已中断的任务可以重试")
    params = dict(task.input.get("request") or {})
    return await create_task(db, engine, user_id, task.kind, params, retry_of=task)


# ---------------- 查询 ----------------


async def get_owned_task(db: AsyncSession, user_id: int, task_id: int) -> Task:
    task = await db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user_id))
    if task is None:
        raise NotFoundError("任务不存在")
    return task


async def get_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    return await task_out(db, engine, await get_owned_task(db, user_id, task_id))


async def get_output(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> str:
    task = await get_owned_task(db, user_id, task_id)
    hub = engine.hubs.get(task.id)
    if hub is not None and task.status in ACTIVE_TASK_STATUSES:
        return hub.text
    return task.output_text


async def list_tasks(
    db: AsyncSession,
    engine: "TaskEngine",
    user_id: int,
    *,
    project_id: int | None = None,
    chapter_id: int | None = None,
    kind: str | None = None,
    active: bool | None = None,
    limit: int = 50,
) -> list[TaskOut]:
    stmt = select(Task).where(Task.user_id == user_id)
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    if chapter_id is not None:
        stmt = stmt.where(Task.chapter_id == chapter_id)
    if kind is not None:
        stmt = stmt.where(Task.kind == kind)
    if active is True:
        stmt = stmt.where(Task.status.in_(ACTIVE_TASK_STATUSES))
    elif active is False:
        stmt = stmt.where(Task.status.in_(FINISHED_TASK_STATUSES))
    tasks = list(await db.scalars(stmt.order_by(Task.id.desc()).limit(limit)))
    queued = await _queued_ids(db) if any(t.status == "queued" for t in tasks) else []
    return [
        to_out(t, queue_position=bisect.bisect_left(queued, t.id) if t.status == "queued" else None, engine=engine)
        for t in tasks
    ]


# ---------------- 取消 / 删除 / 清理 ----------------


async def cancel_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    task = await get_owned_task(db, user_id, task_id)
    if task.status == "queued":
        result = await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.status == "queued")
            .values(status="cancelled", error="任务已取消", stage=STAGE_LABELS["cancelled"], finished_at=func.now())
        )
        await db.commit()
        if result.rowcount == 1:
            # 先把任务置为终态再还原资源：避免与调度器的领取竞争（领取成功则下面走运行中取消）
            await get_handler(task.kind).on_abort(engine.ctx, task)
            await db.refresh(task)
            await prune_finished(db, user_id)
            engine.close_hub(task.id, "error", {"task": out_json(task), "detail": task.error})
            engine.notify()
            return to_out(task)
        await db.refresh(task)
    if task.status == "running":
        await engine.cancel_running(task.id)
        await db.refresh(task)
        return await task_out(db, engine, task)
    raise ConflictError("任务已结束，无法取消")


async def delete_task(db: AsyncSession, user_id: int, task_id: int) -> None:
    task = await get_owned_task(db, user_id, task_id)
    if task.status not in FINISHED_TASK_STATUSES:
        raise ConflictError("任务进行中，无法删除，请先取消")
    await db.delete(task)
    await db.commit()


async def clear_finished(db: AsyncSession, user_id: int, *, project_id: int | None = None) -> int:
    stmt = delete(Task).where(Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES))
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


async def prune_finished(db: AsyncSession, user_id: int) -> None:
    """每个用户只保留最近 MAX_FINISHED_PER_USER 个已结束任务（按 id）。"""
    stale = list(
        await db.scalars(
            select(Task.id)
            .where(Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES))
            .order_by(Task.id.desc())
            .offset(MAX_FINISHED_PER_USER)
        )
    )
    if stale:
        await db.execute(delete(Task).where(Task.id.in_(stale)))
        await db.commit()
