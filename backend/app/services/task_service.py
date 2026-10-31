"""后台任务的 API 入口：创建、列表、详情、取消、重试、继续 / 结束（待审核）、删除、清理与保留裁剪。

所有入口按 user_id 校验归属；他人任务与不存在统一返回 404。
handler 可选钩子（getattr 探测）：
- validate_retry(db, ctx, *, user_id, task) -> TaskSpec：重试时代替 validate_and_claim（多步任务沿用原范围与断点）；
- revalidate_resume(db, ctx, task)：待审核任务继续前重新校验，业务错误抛 AppError；
- finish_result(task) -> dict：待审核任务被结束时的最终 result；
- checkpoint_out(task) -> dict | None：TaskOut.checkpoint 的取值（默认原样返回断点；断点体积大的类型只给摘要）。
"""

import asyncio
import bisect
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.task_engine.handlers  # noqa: F401  导入即注册内置任务类型
from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, Project, Task
from app.models.entities import ACTIVE_TASK_STATUSES, FINISHED_TASK_STATUSES, OPEN_TASK_STATUSES
from app.schemas.skill import SkillRef
from app.schemas.task import TaskOut
from app.services import project_lifecycle, task_statistics_service
from app.services.project_service import get_owned_project
from app.services.task_engine.registry import get_handler

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

# 每个用户保留的已结束任务数
MAX_FINISHED_PER_USER = 200
RETRYABLE_STATUSES = ("failed", "cancelled", "interrupted")
STAGE_LABELS = {
    "queued": "排队中",
    "running": "生成中",
    "paused": "待审核",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "interrupted": "已中断",
}
ACTIVE_CONFLICT = "已有任务在进行中，请等待其完成后再试"


def visible_project_condition():
    return or_(
        Task.project_id.is_(None),
        Project.id.is_not(None) & Project.deleted_at.is_(None) & Project.deletion_started_at.is_(None),
    )


def visible_task_project_exists():
    return or_(Task.project_id.is_(None), exists(
        select(Project.id).where(Project.id == Task.project_id, *project_lifecycle.active_conditions())
    ))


# ---------------- 序列化 ----------------


def _request_int(task: Task, key: str) -> int | None:
    request = (task.input or {}).get("request") if isinstance(task.input, dict) else None
    value = request.get(key) if isinstance(request, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _subject_character_id(task: Task) -> int | None:
    return _request_int(task, "character_id")


def _revise_analysis_id(task: Task) -> int | None:
    return _request_int(task, "analysis_id") if task.kind == "chapter.revise" else None


def _skill_ref(task: Task) -> SkillRef | None:
    snap = (task.input or {}).get("skill") if isinstance(task.input, dict) else None
    if not isinstance(snap, dict):
        return None
    # model_copy(update=) 不做校验：这里先构造成 SkillRef，结构不对的旧数据直接不返回
    try:
        return SkillRef.model_validate(snap)
    except ValidationError:
        return None


def to_out(task: Task, *, queue_position: int | None = None, engine: "TaskEngine | None" = None) -> TaskOut:
    out = TaskOut.model_validate(task)
    update_fields: dict[str, Any] = {
        "queue_position": queue_position if task.status == "queued" else None,
        "character_id": _subject_character_id(task),
        "analysis_id": _revise_analysis_id(task),
        "outline_id": _request_int(task, "outline_id") if task.kind == "outline.expand" else None,
        "skill": _skill_ref(task),
    }
    checkpoint_out = getattr(get_handler(task.kind), "checkpoint_out", None)
    if checkpoint_out is not None:
        update_fields["checkpoint"] = checkpoint_out(task)
    hub = engine.hubs.get(task.id) if engine is not None else None
    if (
        hub is not None and task.statistics_key is not None
        and hub.statistics_key == task.statistics_key and task.status in ACTIVE_TASK_STATUSES
    ):
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
    validate_retry = getattr(handler, "validate_retry", None) if retry_of is not None else None
    if validate_retry is not None:
        spec = await validate_retry(db, engine.ctx, user_id=user_id, task=retry_of)
    else:
        spec = await handler.validate_and_claim(db, engine.ctx, user_id=user_id, kind=kind, params=params)
    if spec.project_id is not None:
        await get_owned_project(db, user_id, spec.project_id, lock=True)
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
        retry_of_id=retry_of.id if retry_of is not None else spec.retry_of_id,
        step_cursor=retry_of.step_cursor if retry_of is not None else 0,
        checkpoint=retry_of.checkpoint if retry_of is not None else None,
    )
    db.add(task)
    try:
        await task_statistics_service.record_created(db, task)
        await db.commit()
    except IntegrityError as exc:
        # 部分唯一索引 uq_tasks_active_resource 兜底：同资源并发提交（章节抢占随回滚撤销）
        await db.rollback()
        if _active_resource_conflict(exc):
            raise ConflictError(ACTIVE_CONFLICT) from exc
        raise
    await refresh_same_task(db, task)
    engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="queued", stage=STAGE_LABELS["queued"])
    engine.notify()
    return await task_out(db, engine, task)


async def retry_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    task = await get_owned_task(db, user_id, task_id)
    if task.status not in RETRYABLE_STATUSES:
        raise ConflictError("只有失败、已取消或已中断的任务可以重试")
    params = dict(task.input.get("request") or {})
    return await create_task(db, engine, user_id, task.kind, params, retry_of=task)


async def resume_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    """待审核 → 排队：同一任务（id、断点不变）从下一步继续；下一步的上下文在运行时按最新数据重新组装。"""
    task = await get_owned_task(db, user_id, task_id)
    await refresh_same_task(db, task)
    if task.project_id is not None:
        await get_owned_project(db, user_id, task.project_id, lock=True)
    if task.status != "paused":
        raise ConflictError("只有待审核的任务可以继续")
    hook = getattr(get_handler(task.kind), "revalidate_resume", None)
    if hook is not None:
        await hook(db, engine.ctx, task)
    try:
        result = await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "paused")
            .values(status="queued", error="", result=None, finished_at=None, stage=STAGE_LABELS["queued"])
        )
        if result.rowcount == 1:
            await task_statistics_service.record_state_change(db, task, "queued")
        await db.commit()
    except IntegrityError as exc:
        # 部分唯一索引兜底：待审核期间同资源已有新的活动任务
        await db.rollback()
        if _active_resource_conflict(exc):
            raise ConflictError(ACTIVE_CONFLICT) from exc
        raise
    if result.rowcount != 1:
        raise ConflictError("任务状态已变化，请刷新后重试")
    await refresh_same_task(db, task)
    engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="queued", stage=STAGE_LABELS["queued"])
    engine.notify()
    return await task_out(db, engine, task)


async def finish_paused_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    """待审核 → 已完成（提前结束）：剩余步骤不再执行。"""
    task = await get_owned_task(db, user_id, task_id)
    await refresh_same_task(db, task)
    if task.project_id is not None:
        await get_owned_project(db, user_id, task.project_id, lock=True)
    if task.status != "paused":
        raise ConflictError("只有待审核的任务可以结束")
    hook = getattr(get_handler(task.kind), "finish_result", None)
    final = hook(task) if hook is not None else {**(task.result or {}), "ended_early": True}
    stamp = task_statistics_service.now_utc()
    result = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "paused")
        .values(status="succeeded", result=final, progress=100, stage=STAGE_LABELS["succeeded"], finished_at=stamp)
    )
    if result.rowcount == 1:
        await task_statistics_service.record_state_change(db, task, "succeeded", finished_at=stamp)
    await db.commit()
    if result.rowcount != 1:
        raise ConflictError("任务状态已变化，请刷新后重试")
    await refresh_same_task(db, task)
    await prune_finished(db, user_id)
    return to_out(task)


# ---------------- 查询 ----------------


async def get_owned_task(db: AsyncSession, user_id: int, task_id: int) -> Task:
    task = await db.scalar(select(Task).where(
        Task.id == task_id, Task.user_id == user_id, visible_task_project_exists()
    ))
    if task is None:
        raise NotFoundError("任务不存在")
    return task


async def refresh_same_task(db: AsyncSession, task: Task) -> Task:
    """只重读原UUID，不能让旧请求因SQLite整数ID复用改为操作另一个任务。"""
    if task.statistics_key is None:
        raise NotFoundError("任务不存在")
    row = await db.scalar(select(Task).where(
        Task.id == task.id, Task.statistics_key == task.statistics_key
    ).execution_options(populate_existing=True))
    if row is None:
        raise NotFoundError("任务不存在")
    return row


async def get_task(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> TaskOut:
    return await task_out(db, engine, await get_owned_task(db, user_id, task_id))


async def get_output(db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int) -> str:
    task = await get_owned_task(db, user_id, task_id)
    hub = engine.hubs.get(task.id)
    if (
        hub is not None and task.statistics_key is not None
        and hub.statistics_key == task.statistics_key and task.status in ACTIVE_TASK_STATUSES
    ):
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
    stmt = select(Task).where(Task.user_id == user_id, visible_task_project_exists())
    if project_id is not None:
        stmt = stmt.where(Task.project_id == project_id)
    if chapter_id is not None:
        stmt = stmt.where(Task.chapter_id == chapter_id)
    if kind is not None:
        stmt = stmt.where(Task.kind == kind)
    if active is True:
        # 「进行中」包含待审核：面板据此展示继续 / 结束
        stmt = stmt.where(Task.status.in_(OPEN_TASK_STATUSES))
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
    return await _cancel_loaded_task(db, engine, task)


async def _cancel_loaded_task(db: AsyncSession, engine: "TaskEngine", task: Task) -> TaskOut:
    """调用者已核实归属或删除项目范围；只共享取消，不伪装成目标 owner。"""
    with engine.cleanup_scope(task):
        return await _cancel_loaded_impl(db, engine, task)


async def _cancel_loaded_impl(db: AsyncSession, engine: "TaskEngine", task: Task) -> TaskOut:
    await refresh_same_task(db, task)
    if task.status == "queued":
        stamp = task_statistics_service.now_utc()
        result = await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "queued")
            .values(status="cancelled", error="任务已取消", stage=STAGE_LABELS["cancelled"], finished_at=stamp)
        )
        if result.rowcount == 1:
            await task_statistics_service.record_state_change(db, task, "cancelled", finished_at=stamp)
        await db.commit()
        if result.rowcount == 1:
            # 先把任务置为终态再还原资源：避免与调度器的领取竞争（领取成功则下面走运行中取消）
            await _abort_loaded_task(engine, task)
            await refresh_same_task(db, task)
            await prune_finished(db, task.user_id)
            engine.close_hub(
                task.id, "error", {"task": out_json(task), "detail": task.error}, statistics_key=task.statistics_key
            )
            engine.notify()
            return to_out(task)
        await refresh_same_task(db, task)
    if task.status == "paused":
        stamp = task_statistics_service.now_utc()
        result = await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "paused")
            .values(status="cancelled", error="任务已取消", stage=STAGE_LABELS["cancelled"], finished_at=stamp)
        )
        if result.rowcount == 1:
            await task_statistics_service.record_state_change(db, task, "cancelled", finished_at=stamp)
        await db.commit()
        if result.rowcount == 1:
            # 待审核时没有执行体，资源也已释放；on_abort 幂等，照常调用
            await _abort_loaded_task(engine, task)
            await refresh_same_task(db, task)
            await prune_finished(db, task.user_id)
            return to_out(task)
        await refresh_same_task(db, task)
        if task.status == "queued":
            # 与继续竞争：已重新排队，按排队中取消
            return await _cancel_loaded_task(db, engine, task)
    if task.status == "running":
        # 等待独立执行体收尾前结束请求侧事务，不能持有它需要的数据库锁。
        await db.commit()
        execution = engine._running.get(task.id)
        if execution is None or execution.done():
            # 结束状态落库曾失败的孤立 running；单进程中已无执行体，可重试幂等收尾。
            await _abort_loaded_task(engine, task)
            stamp = task_statistics_service.now_utc()
            result = await db.execute(update(Task).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "running"
            ).values(
                status="cancelled", error="任务已取消", stage=STAGE_LABELS["cancelled"], finished_at=stamp
            ))
            if result.rowcount == 1:
                await task_statistics_service.record_state_change(db, task, "cancelled", finished_at=stamp)
            await db.commit()
            await refresh_same_task(db, task)
            engine.close_hub(
                task.id, "error", {"task": out_json(task), "detail": task.error}, statistics_key=task.statistics_key
            )
            return to_out(task)
        await engine.cancel_running(task.id, statistics_key=task.statistics_key)
        await refresh_same_task(db, task)
        return await task_out(db, engine, task)
    raise ConflictError("任务已结束，无法取消")


async def delete_task(db: AsyncSession, user_id: int, task_id: int) -> None:
    task = await get_owned_task(db, user_id, task_id)
    await refresh_same_task(db, task)
    if task.project_id is not None:
        await get_owned_project(db, user_id, task.project_id, lock=True)
    if task.status == "paused":
        raise ConflictError("任务待审核，无法删除，请先结束或取消")
    if task.status not in FINISHED_TASK_STATUSES:
        raise ConflictError("任务进行中，无法删除，请先取消")
    await task_statistics_service.mark_removed(
        db, Task.id == task.id, Task.statistics_key == task.statistics_key
    )
    result = await db.execute(delete(Task).where(
        Task.id == task.id, Task.statistics_key == task.statistics_key
    ))
    if result.rowcount != 1:
        raise NotFoundError("任务不存在")
    await db.commit()


async def _lock_history_projects(db: AsyncSession, user_id: int, project_ids: set[int]) -> set[int]:
    locked = set()
    for project_id in sorted(project_ids):
        try:
            await get_owned_project(db, user_id, project_id, lock=True)
        except (NotFoundError, ConflictError):
            # 查询候选行后可能刚建立删除屏障；跳过该项目而不是破坏回收站历史。
            continue
        locked.add(project_id)
    return locked


async def clear_finished(db: AsyncSession, user_id: int, *, project_id: int | None = None) -> int:
    statement = select(Task.project_id).where(
        Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES), visible_task_project_exists()
    )
    if project_id is not None:
        statement = statement.where(Task.project_id == project_id)
    candidates = set(await db.scalars(statement))
    locked = await _lock_history_projects(db, user_id, candidates - {None})
    allowed = or_(Task.project_id.in_(locked), Task.project_id.is_(None) if None in candidates else False)
    conditions = (
        Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES), visible_task_project_exists(), allowed
    )
    keys = await task_statistics_service.mark_removed(db, *conditions)
    statement = delete(Task).where(*conditions, Task.statistics_key.in_(keys))
    result = await db.execute(statement)
    await db.commit()
    return result.rowcount or 0


async def prune_finished(db: AsyncSession, user_id: int) -> None:
    """回收站历史不参与普通用户已结束任务裁剪；锁住将被裁剪的父项目直到提交。"""
    stale = (await db.execute(
        select(Task.id, Task.statistics_key, Task.project_id)
        .where(Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES), visible_task_project_exists())
        .order_by(Task.id.desc()).offset(MAX_FINISHED_PER_USER)
    )).all()
    if not stale:
        return
    locked = await _lock_history_projects(db, user_id, {pid for _, _, pid in stale if pid is not None})
    eligible = [(tid, key) for tid, key, pid in stale if pid is None or pid in locked]
    if eligible:
        identities = or_(
            Task.statistics_key.in_([key for _, key in eligible if key is not None]),
            and_(Task.id.in_([tid for tid, key in eligible if key is None]), Task.statistics_key.is_(None)),
        )
        conditions = (Task.user_id == user_id, Task.status.in_(FINISHED_TASK_STATUSES), visible_task_project_exists())
        keys = await task_statistics_service.mark_removed(db, identities, *conditions)
        await db.execute(delete(Task).where(Task.statistics_key.in_(keys), *conditions))
        await db.commit()


async def stop_project_tasks(db: AsyncSession, engine: "TaskEngine", project_id: int) -> int:
    """准入已冻结后排空整个项目执行链；统一超时但不二次 cancel 引擎执行体。"""
    stopped: set[int] = set()
    retry_abort = project_id in engine._abort_failures
    engine._abort_failures.discard(project_id)
    try:
        async with asyncio.timeout(engine.cancel_wait_seconds):
            if retry_abort:
                aborted = list(await db.scalars(select(Task).where(
                    Task.project_id == project_id, Task.status.in_(("cancelled", "failed", "interrupted"))
                )))
                for task in aborted:
                    with engine.cleanup_scope(task):
                        await _abort_loaded_task(engine, task)
            while True:
                tasks = list(await db.scalars(select(Task).where(
                    Task.project_id == project_id, Task.status.in_(OPEN_TASK_STATUSES)
                ).execution_options(populate_existing=True)))
                for task in tasks:
                    try:
                        await _cancel_loaded_task(db, engine, task)
                        stopped.add(task.id)
                    except ConflictError:
                        # 状态可能刚被其他取消/正常完成改变；下一轮重新检查。
                        await db.rollback()
                work = engine.project_work(project_id)
                if work:
                    await db.rollback()
                    await asyncio.wait(work)
                    continue
                remaining = await db.scalar(select(Task.id).where(
                    Task.project_id == project_id, Task.status.in_(OPEN_TASK_STATUSES)
                ).limit(1))
                if remaining is None:
                    break
                await db.rollback()
                await asyncio.sleep(0)
            if project_id in engine._abort_failures:
                raise ConflictError("任务收尾失败，作品未删除", extra={"code": "project_tasks_not_stopped"})
            generating = await db.scalar(select(Chapter.id).where(
                Chapter.project_id == project_id, Chapter.status == "generating"
            ).limit(1))
            if generating is not None:
                raise ConflictError("章节生成状态尚未安全收尾，作品未删除", extra={"code": "project_tasks_not_stopped"})
    except TimeoutError as exc:
        raise ConflictError(
            "等待任务停止超时，作品未删除；已取消任务不会自动恢复，请稍后重试",
            extra={"code": "project_tasks_not_stopped"},
        ) from exc
    except ConflictError:
        raise
    except Exception:
        raise ConflictError(
            "任务收尾失败，作品未删除；已取消任务不会自动恢复",
            extra={"code": "project_tasks_not_stopped"},
        ) from None
    return len(stopped)


async def _abort_loaded_task(engine: "TaskEngine", task: Task) -> None:
    try:
        await get_handler(task.kind).on_abort(engine.ctx, task)
    except Exception:
        if task.project_id is not None:
            engine._abort_failures.add(task.project_id)
        raise


def _active_resource_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    cause = getattr(original, "__cause__", None)
    name = getattr(original, "constraint_name", None) or getattr(cause, "constraint_name", None)
    name = name or getattr(getattr(cause, "diag", None), "constraint_name", None)
    return name == "uq_tasks_active_resource" or str(original) == "UNIQUE constraint failed: tasks.resource_key"
