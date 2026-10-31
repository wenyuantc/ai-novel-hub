"""作品删除协调、管理员恢复/清理与单进程到期清理。"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.models import Chapter, ChapterVersion, Project, Task, User
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.recycle_bin import RecycleProjectList, RecycleProjectOut
from app.services import project_lifecycle, retrieval_service, task_service, task_statistics_service
from app.services.admin_service import _escape_like
from app.services.audit_writer import AuditOperation
from app.services.project_service import chapter_stats_subquery, get_manageable_project

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)
CLEANUP_INTERVAL = 3600.0
CLEANUP_BATCH = 100


async def _actor(db: AsyncSession, actor_id: int, *, admin: bool = False) -> User:
    actor = await db.scalar(select(User).where(User.id == actor_id).execution_options(populate_existing=True))
    if actor is None or not actor.is_active:
        raise UnauthorizedError("未登录或登录已过期")
    if admin and not actor.is_admin:
        raise ForbiddenError("需要管理员权限")
    return actor


async def _release_intent(sessionmaker, project_id: int, token: str) -> None:
    try:
        async with sessionmaker() as db:
            await db.execute(
                update(Project)
                .where(Project.id == project_id, Project.deletion_token == token, Project.deleted_at.is_(None))
                .values(deletion_started_at=None, deletion_token=None, updated_at=Project.updated_at)
            )
            await db.commit()
    except Exception:
        # 数据库不可用时下次启动恢复，不记录包含业务 SQL 参数的异常。
        logger.error("作品删除意图清理失败 project=%s", project_id)


async def move_to_recycle_bin(db: AsyncSession, engine: "TaskEngine", actor_id: int, project_id: int) -> None:
    token = str(uuid4())
    started = False
    try:
        async with AuditOperation(
            db, actor_id, "project.delete", "project", project_id, project_id=project_id
        ) as audit:
            project = await get_manageable_project(db, actor_id, project_id, lock=True)
            await _actor(db, actor_id)
            audit.target(project)
            project.deletion_started_at = project_lifecycle.now_utc()
            project.deletion_token = token
            started = True
            await db.commit()
            cancelled = await task_service.stop_project_tasks(db, engine, project_id)
            await db.rollback()
            project = await project_lifecycle.lock_record(db, project_id)
            if project.deletion_token != token or project.deleted_at is not None:
                raise ConflictError("作品状态已变化，请刷新后重试")
            actor = await _actor(db, actor_id)
            if project.user_id != actor.id and not actor.is_admin:
                raise ForbiddenError("需要管理员权限")
            await _ensure_idle(db, engine, project_id)
            stamp = project_lifecycle.now_utc()
            if project.last_deleted_at is not None:
                stamp = max(stamp, project_lifecycle.as_utc(project.last_deleted_at) + timedelta(microseconds=1))
            project.deleted_at = project.last_deleted_at = stamp
            project.deleted_by_user_id = actor.id
            project.deleted_by_username = actor.username
            project.deletion_started_at = None
            project.deletion_token = None
            audit.summary.lifecycle_action = "soft_delete"
            audit.summary.retention_days = project_lifecycle.RETENTION_DAYS
            audit.summary.cancelled_task_count = cancelled
            if audit.enabled:
                await _counts(db, audit, project_id)
            await audit.commit()
            retrieval_service.invalidate(project_id)
    finally:
        # 先释放请求事务再用独立会话清理，避免异常/取消路径等待自己持有的锁。
        await db.rollback()
        if started:
            cleanup = asyncio.create_task(_release_intent(engine.ctx.sessionmaker, project_id, token))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise


async def _counts(db: AsyncSession, audit: AuditOperation, project_id: int) -> None:
    audit.summary.chapter_count = int(
        await db.scalar(select(func.count(Chapter.id)).where(Chapter.project_id == project_id)) or 0
    )
    audit.summary.version_count = int(
        await db.scalar(
            select(func.count(ChapterVersion.id))
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.project_id == project_id)
        )
        or 0
    )


async def _ensure_idle(db: AsyncSession, engine: "TaskEngine", project_id: int) -> None:
    if engine.project_work(project_id) or project_id in engine._abort_failures:
        raise ConflictError("任务尚未安全收尾，作品未删除", extra={"code": "project_tasks_not_stopped"})
    if (
        await db.scalar(
            select(Task.id).where(Task.project_id == project_id, Task.status.in_(OPEN_TASK_STATUSES)).limit(1)
        )
        is not None
    ):
        raise ConflictError("作品仍有未结束任务", extra={"code": "project_tasks_not_stopped"})


async def list_projects(
    db: AsyncSession, *, q: str | None, owner_user_id: int | None, page: int, page_size: int
) -> RecycleProjectList:
    conditions = [Project.deleted_at.is_not(None)]
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{_escape_like(keyword)}%"
        conditions.append(or_(Project.title.ilike(pattern, escape="\\"), User.username.ilike(pattern, escape="\\")))
    if owner_user_id is not None:
        conditions.append(Project.user_id == owner_user_id)
    total = await db.scalar(select(func.count(Project.id)).join(User, User.id == Project.user_id).where(*conditions))
    stats = chapter_stats_subquery()
    rows = (
        await db.execute(
            select(Project, User.username, stats.c.chapter_count, stats.c.total_words)
            .join(User, User.id == Project.user_id)
            .outerjoin(stats, stats.c.project_id == Project.id)
            .where(*conditions)
            .order_by(Project.deleted_at.desc(), Project.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    now = project_lifecycle.now_utc()
    return RecycleProjectList(
        items=[
            RecycleProjectOut(
                id=p.id,
                title=p.title,
                genre=p.genre,
                status=p.status,
                owner_user_id=p.user_id,
                owner_username=username,
                deleted_by_user_id=p.deleted_by_user_id,
                deleted_by_username=p.deleted_by_username,
                deleted_at=p.deleted_at,
                expires_at=project_lifecycle.expires_at(p.deleted_at),
                restorable=now < project_lifecycle.expires_at(p.deleted_at),
                chapter_count=int(chapters or 0),
                total_words=int(words or 0),
            )
            for p, username, chapters, words in rows
        ],
        total=int(total or 0),
    )


def _check_record(project: Project, deleted_at: datetime) -> None:
    if project.deleted_at is None:
        raise ConflictError("作品不在回收站，请刷新后重试", extra={"code": "project_not_deleted"})
    if project_lifecycle.as_utc(project.deleted_at) != project_lifecycle.as_utc(deleted_at):
        raise ConflictError("回收站记录已变化，请刷新后重试", extra={"code": "recycle_record_stale"})


async def restore(db: AsyncSession, actor_id: int, project_id: int, deleted_at: datetime) -> None:
    await _actor(db, actor_id, admin=True)
    async with AuditOperation(
        db, actor_id, "project.restore", "project", project_id, project_id=project_id, include_own_project=True
    ) as audit:
        project = await project_lifecycle.lock_record(db, project_id)
        _check_record(project, deleted_at)
        if project_lifecycle.now_utc() >= project_lifecycle.expires_at(project.deleted_at):
            raise ConflictError("作品已超过 30 天保留期，无法恢复", extra={"code": "recycle_expired"})
        if await db.get(User, project.user_id) is None:
            raise ConflictError("所属用户不存在，无法恢复作品")
        await _actor(db, actor_id, admin=True)
        audit.target(project)
        audit.summary.lifecycle_action = "restore"
        project.deleted_at = None
        project.deleted_by_user_id = None
        project.deleted_by_username = None
        await audit.commit()
        retrieval_service.invalidate(project_id)


async def purge(
    db: AsyncSession,
    engine: "TaskEngine",
    actor_id: int | None,
    project_id: int,
    deleted_at: datetime,
    *,
    title: str | None = None,
    cutoff: datetime | None = None,
) -> None:
    system = actor_id is None and cutoff is not None
    if not system:
        if actor_id is None:
            raise ForbiddenError("需要管理员权限")
        await _actor(db, actor_id, admin=True)
    action = "project.expire" if system else "project.purge"
    async with AuditOperation(
        db,
        actor_id,
        action,
        "project",
        project_id,
        project_id=project_id,
        include_own_project=True,
        system_expiry=system,
    ) as audit:
        project = await project_lifecycle.lock_record(db, project_id)
        _check_record(project, deleted_at)
        if system:
            if project_lifecycle.expires_at(project.deleted_at) > cutoff:
                raise ConflictError("作品尚未到期")
        else:
            await _actor(db, actor_id, admin=True)
            if title != project.title:
                raise BadRequestError("确认名称与作品名称不一致")
        await _ensure_idle(db, engine, project_id)
        audit.target(project)
        audit.summary.lifecycle_action = "expire" if system else "purge"
        audit.summary.retention_days = project_lifecycle.RETENTION_DAYS
        await _counts(db, audit, project_id)
        await task_statistics_service.mark_removed(db, Task.project_id == project_id)
        task_keys = dict((await db.execute(
            select(Task.id, Task.statistics_key).where(Task.project_id == project_id)
        )).all())
        await db.delete(project)
        await audit.commit()
        engine.forget_project_hubs(list(task_keys), statistics_keys=task_keys)
        engine._abort_failures.discard(project_id)
        retrieval_service.invalidate(project_id)


async def recover_pending_deletions(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """仅在单进程启动、引擎尚未运行时调用：失败的旧请求不会自动完成删除。"""
    async with sessionmaker() as db:
        await db.execute(
            update(Project)
            .where(Project.deleted_at.is_(None))
            .values(deletion_started_at=None, deletion_token=None, updated_at=Project.updated_at)
        )
        await db.commit()


async def purge_expired(engine: "TaskEngine", *, now: datetime | None = None) -> int:
    cutoff = project_lifecycle.as_utc(now) if now is not None else project_lifecycle.now_utc()
    deleted_before = cutoff - timedelta(days=project_lifecycle.RETENTION_DAYS)
    cursor: tuple[datetime, int] | None = None
    count = 0
    while True:
        async with engine.ctx.sessionmaker() as db:
            statement = select(Project.deleted_at, Project.id).where(Project.deleted_at <= deleted_before)
            if cursor is not None:
                stamp, pid = cursor
                statement = statement.where(
                    or_(Project.deleted_at > stamp, and_(Project.deleted_at == stamp, Project.id > pid))
                )
            rows = (await db.execute(statement.order_by(Project.deleted_at, Project.id).limit(CLEANUP_BATCH))).all()
        if not rows:
            break
        for stamp, pid in rows:
            cursor = (stamp, pid)
            try:
                async with engine.ctx.sessionmaker() as db:
                    await purge(db, engine, None, pid, stamp, cutoff=cutoff)
                count += 1
            except (ConflictError, NotFoundError):
                logger.warning("到期作品清理跳过 project=%s", pid)
            except Exception:
                logger.error("到期作品清理失败 project=%s", pid)
    return count


class RecycleBinCleaner:
    def __init__(self, engine: "TaskEngine", *, interval: float = CLEANUP_INTERVAL):
        self.engine = engine
        self.interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        await self._pass()
        self._task = asyncio.create_task(self._run(), name="recycle-bin-cleaner")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with asyncio.timeout(self.interval):
                    await self._stop.wait()
            except TimeoutError:
                await self._pass()

    async def _pass(self) -> None:
        try:
            await purge_expired(self.engine)
        except Exception:
            logger.error("回收站定时清理暂时失败，下个周期重试")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task
