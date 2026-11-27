"""作品删除协调、管理员恢复/清理与到期清理。"""

import asyncio
import logging
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4
from weakref import WeakKeyDictionary

from sqlalchemy import and_, delete, func, or_, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.media import cleanup as media_cleanup
from app.models import (
    AnalysisChange,
    Career,
    Chapter,
    ChapterAnalysis,
    ChapterVersion,
    Character,
    Organization,
    Project,
    Scene,
    SettingCategory,
    SettingCategoryRule,
    SettingFieldDefinition,
    SettingFieldOption,
    SettingProgression,
    SettingReferenceHandle,
    SettingTextField,
    SettingTimeline,
    Task,
    User,
    WorldSetting,
)
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.recycle_bin import RecycleProjectList, RecycleProjectOut
from app.services import authorization_service as authz
from app.services import project_lifecycle, retrieval_service, task_service, task_statistics_service
from app.services.admin_service import _escape_like
from app.services.agent import external_activity
from app.services.audit_writer import AuditOperation
from app.services.periodic_database import retry_contended_write
from app.services.project_service import chapter_stats_subquery, get_manageable_project, has_unsettled_external_change
from app.services.task_engine import cleanup_work, workers

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)
CLEANUP_INTERVAL = 3600.0
CLEANUP_BATCH = 100
# 只有请求自己的 finally 能登记已退出、但 token 释放回执不明的操作。
_failed_releases: WeakKeyDictionary[async_sessionmaker, set[project_lifecycle.DeletionIntent]] = WeakKeyDictionary()


def _has_pending_session_writes(db: AsyncSession) -> bool:
    """协调器会自行提交，不能把调用方的待写对象一起 flush 或提交。"""
    return bool(db.new or db.deleted or any(db.is_modified(item) for item in db.dirty))


async def _reject_pending_soft_delete(db: AsyncSession, actor_id: int, project_id: int) -> None:
    """先按持久事实做无副作用鉴权，再拒绝暂存；不回滚调用方的事务。"""
    with db.no_autoflush:
        record = (await db.execute(select(
            Project.user_id, Project.deleted_at, Project.deletion_started_at,
        ).where(Project.id == project_id))).one_or_none()
        if record is None:
            raise NotFoundError("项目不存在")
        await authz.require_authenticated(db, actor_id)
        if record.user_id != actor_id:
            if not await authz.allows(db, actor_id, "projects.read"):
                raise NotFoundError("项目不存在")
            await authz.require(db, actor_id, "projects.recycle")
        if record.deleted_at is not None:
            raise NotFoundError("项目不存在")
        if record.deletion_started_at is not None:
            raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
    raise ConflictError("当前事务含未保存的修改，无法删除作品", extra={"code": "project_tasks_not_stopped"})


async def _reject_pending_purge(db: AsyncSession, actor_id: int | None, project_id: int, *, system: bool) -> None:
    """保留既有管理员鉴权优先级，不提交或覆盖其他待写对象。"""
    if not system:
        if actor_id is None:
            raise ForbiddenError("需要管理员权限")
        with db.no_autoflush:
            owner = await db.scalar(select(Project.user_id).where(Project.id == project_id))
            if owner is None:
                raise NotFoundError("项目不存在")
            await authz.require(db, actor_id, "recycle_bin.purge")
    raise ConflictError("当前事务含未保存的修改，无法删除作品", extra={"code": "project_tasks_not_stopped"})


async def _actor(db: AsyncSession, actor_id: int, *, admin: bool = False, permission: str | None = None) -> User:
    actor = await db.scalar(select(User).where(User.id == actor_id).execution_options(populate_existing=True))
    if actor is None or not actor.is_active:
        raise UnauthorizedError("未登录或登录已过期")
    if admin:
        await authz.require(db, actor_id, permission)
    return actor


async def _release_intent(sessionmaker, intent: project_lifecycle.DeletionIntent) -> None:
    try:
        async with sessionmaker() as db:
            await db.execute(
                update(Project)
                .where(Project.id == intent.project_id, Project.deletion_token == intent.token,
                       Project.deletion_worker_key == intent.worker_key, Project.deleted_at.is_(None))
                .values(deletion_started_at=None, deletion_token=None, deletion_worker_key=None,
                        updated_at=Project.updated_at)
            )
            await db.commit()
    except Exception:
        # 活着的 worker 不能由对端代为释放；自己恢复可写后仍需归还原 token。
        _failed_releases.setdefault(sessionmaker, set()).add(intent)
        logger.error("作品删除意图清理失败 project=%s", intent.project_id)
    else:
        if pending := _failed_releases.get(sessionmaker):
            pending.discard(intent)
            if not pending:
                del _failed_releases[sessionmaker]


async def move_to_recycle_bin(db: AsyncSession, engine: "TaskEngine", actor_id: int, project_id: int) -> None:
    if _has_pending_session_writes(db):
        await _reject_pending_soft_delete(db, actor_id, project_id)
    token = str(uuid4())
    intent = None
    try:
        async with AuditOperation(
            db, actor_id, "project.delete", "project", project_id, project_id=project_id
        ) as audit:
            record = await project_lifecycle.load_record(db, project_id)
            if record is None:
                raise NotFoundError("项目不存在")
            await authz.require_authenticated(db, actor_id, write=True, targets=(record.user_id,))
            project = await get_manageable_project(db, actor_id, project_id, lock=True, permission="projects.recycle")
            await _actor(db, actor_id)
            audit.target(project)
            # 本地 work 只能提示有机会等到收尾，不能证明任何具体 claim 属于该进程。
            unsettled = await has_unsettled_external_change(db, project_id=project_id)
            if unsettled and not engine.project_work(project_id) and not await external_activity.has_active_call(
                db, project_id
            ):
                raise ConflictError("作品仍有操作尚未安全收尾", extra={"code": "project_tasks_not_stopped"})
            worker_key = await engine.request_worker.require(db)
            owner_id = project.user_id
            intent = project_lifecycle.DeletionIntent(project_id, owner_id, actor_id, token, worker_key)
            db.info["project_deletion_intent"] = intent
            project.deletion_started_at = project_lifecycle.now_utc()
            project.deletion_token = token
            project.deletion_worker_key = worker_key
            await db.commit()
            if unsettled:
                # 意图只冻结新写入；等待时绝不取消任务。等待后必须重新锁内检查持久状态。
                await _wait_for_external_work(db, engine, intent)
            cancelled = await task_service.stop_project_tasks(db, engine, project_id)
            await db.rollback()
            project = await intent.lock(db, engine)
            actor = await _actor(db, actor_id)
            await _ensure_idle(db, engine, project_id)
            stamp = project_lifecycle.now_utc()
            if project.last_deleted_at is not None:
                stamp = max(stamp, project_lifecycle.as_utc(project.last_deleted_at) + timedelta(microseconds=1))
            project.deleted_at = project.last_deleted_at = stamp
            project.deleted_by_user_id = actor.id
            project.deleted_by_username = actor.username
            project.deletion_started_at = None
            project.deletion_token = None
            project.deletion_worker_key = None
            audit.summary.lifecycle_action = "soft_delete"
            audit.summary.retention_days = project_lifecycle.RETENTION_DAYS
            audit.summary.cancelled_task_count = cancelled
            if audit.enabled:
                await _counts(db, audit, project_id)
            await audit.commit()
            retrieval_service.invalidate(project_id)
    finally:
        # 先释放请求事务再用独立会话清理，避免异常/取消路径等待自己持有的锁。
        try:
            await db.rollback()
        finally:
            if intent is not None:
                db.info.pop("project_deletion_intent", None)
                cleanup = asyncio.create_task(_release_intent(engine.ctx.sessionmaker, intent))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise


async def _wait_for_external_work(
    db: AsyncSession, engine: "TaskEngine", intent: project_lifecycle.DeletionIntent,
) -> None:
    project_id = intent.project_id
    try:
        async with asyncio.timeout(engine.cancel_wait_seconds):
            while True:
                await intent.lock(db, engine)
                if not await has_unsettled_external_change(db, project_id=project_id):
                    await db.rollback()
                    return
                work = engine.project_work(project_id)
                if not work and not await external_activity.has_active_call(db, project_id):
                    raise ConflictError("作品仍有操作尚未安全收尾", extra={"code": "project_tasks_not_stopped"})
                await db.rollback()
                if work:
                    await asyncio.wait(work, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(0.05)
    except TimeoutError as exc:
        raise ConflictError("等待操作安全收尾超时，作品未删除", extra={"code": "project_tasks_not_stopped"}) from exc


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
    await cleanup_work.reap_settled_requests(db, project_id)
    if (engine.project_work(project_id) or await cleanup_work.has_failure(db, engine, project_id)
            or await task_service.project_work_exists(db, project_id)):
        raise ConflictError("任务尚未安全收尾，作品未删除", extra={"code": "project_tasks_not_stopped"})
    if await has_unsettled_external_change(db, project_id=project_id):
        raise ConflictError("作品仍有操作尚未安全收尾", extra={"code": "project_tasks_not_stopped"})
    if (
        await db.scalar(
            select(Task.id).outerjoin(Chapter, Chapter.id == Task.chapter_id).where(
                or_(Task.project_id == project_id, Chapter.project_id == project_id),
                or_(Task.status.in_(OPEN_TASK_STATUSES), Task.admin_cancel_operation_id.is_not(None),
                    Task.execution_key.is_not(None)),
            ).limit(1)
        )
        is not None
    ):
        raise ConflictError("作品仍有未结束任务", extra={"code": "project_tasks_not_stopped"})
    candidates = list(await db.scalars(select(Task).outerjoin(Chapter, Chapter.id == Task.chapter_id).where(
        or_(Task.project_id == project_id, Chapter.project_id == project_id),
        ~task_service._without_unresolved_task_receipt(db),
    ).order_by(Task.id).with_for_update(of=Task)))
    for task in candidates:
        if task.project_id != project_id or await task_service.has_unresolved_receipt(db, task):
            raise ConflictError("任务完成事实尚未核对，作品未删除", extra={"code": "project_tasks_not_stopped"})


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


async def _has_unsafe_setting_references(db: AsyncSession, project_id: int, *, incoming_only: bool = False) -> bool:
    """只读检查，避免清理与其他作品互相引用的字段、事件或主体。"""

    def scoped(statement, left, right):
        # 分成出站／入站两个目标范围，避免跨表 OR 使 PostgreSQL 放弃项目及 FK 索引。
        incoming = statement.where(right == project_id, left != project_id)
        return [incoming] if incoming_only else [statement.where(left == project_id, right != project_id), incoming]

    # 错误的跨作品引用既可能由目标指向外部，也可能从外部指入目标；两者均不能安全清理。
    issues = []
    if not incoming_only:
        issues.extend(scoped(
            select(SettingTextField.id).join(SettingTimeline, SettingTextField.timeline_id == SettingTimeline.id)
            .join(SettingFieldDefinition, SettingTextField.definition_id == SettingFieldDefinition.id),
            SettingTimeline.project_id, SettingFieldDefinition.project_id))
        issues.extend(scoped(
            select(SettingCategoryRule.id).join(SettingCategory, SettingCategoryRule.category_id == SettingCategory.id)
            .join(SettingFieldDefinition, SettingCategoryRule.definition_id == SettingFieldDefinition.id),
            SettingCategory.project_id, SettingFieldDefinition.project_id))
        issues.extend(scoped(
            select(SettingCategoryRule.id).join(SettingCategory, SettingCategoryRule.category_id == SettingCategory.id)
            .join(SettingFieldOption, SettingCategoryRule.option_id == SettingFieldOption.id)
            .join(SettingFieldDefinition, SettingFieldOption.definition_id == SettingFieldDefinition.id),
            SettingCategory.project_id, SettingFieldDefinition.project_id))
    for model, column in (
        (Character, SettingTimeline.character_id),
        (Character, SettingTimeline.related_character_id),
        (WorldSetting, SettingTimeline.world_setting_id),
        (Organization, SettingTimeline.organization_id),
        (Career, SettingTimeline.career_id),
    ):
        issues.extend(scoped(select(SettingTimeline.id).join(model, column == model.id),
                             SettingTimeline.project_id, model.project_id))
    for model, column in (
        (Character, SettingReferenceHandle.character_id),
        (WorldSetting, SettingReferenceHandle.world_setting_id),
        (Organization, SettingReferenceHandle.organization_id),
        (Career, SettingReferenceHandle.career_id),
    ):
        issues.extend(scoped(select(SettingReferenceHandle.id).join(model, column == model.id),
                             SettingReferenceHandle.project_id, model.project_id))

    field_owner = aliased(SettingTimeline)
    event_owner = aliased(SettingTimeline)
    if not incoming_only:
        query = (
            select(SettingProgression.id).join(event_owner, SettingProgression.timeline_id == event_owner.id)
            .join(SettingTextField, SettingProgression.custom_field_id == SettingTextField.id)
            .join(field_owner, SettingTextField.timeline_id == field_owner.id)
            .where(event_owner.id != field_owner.id)
        )
        issues.extend((query.where(event_owner.project_id == project_id),
                       query.where(field_owner.project_id == project_id)))
    for query, left, right in (
        (select(SettingProgression.id).join(event_owner, SettingProgression.timeline_id == event_owner.id)
         .join(Chapter, SettingProgression.chapter_id == Chapter.id), event_owner.project_id, Chapter.project_id),
        (select(SettingProgression.id).join(event_owner, SettingProgression.timeline_id == event_owner.id)
         .join(Scene, SettingProgression.scene_id == Scene.id).join(Chapter, Scene.chapter_id == Chapter.id),
         event_owner.project_id, Chapter.project_id),
        (select(SettingProgression.id).join(event_owner, SettingProgression.timeline_id == event_owner.id)
         .join(AnalysisChange, SettingProgression.source_change_id == AnalysisChange.id),
         event_owner.project_id, AnalysisChange.project_id),
        (select(ChapterAnalysis.id).join(Chapter, ChapterAnalysis.chapter_id == Chapter.id),
         ChapterAnalysis.project_id, Chapter.project_id),
        (select(AnalysisChange.id).join(ChapterAnalysis, AnalysisChange.analysis_id == ChapterAnalysis.id),
         AnalysisChange.project_id, ChapterAnalysis.project_id),
    ):
        issues.extend(scoped(query, left, right))
    # JSON 内的句柄／选项没有数据库外键或反向索引，合法写入口会校验同书。
    # superseded_by_id 没有反向索引且沿用原级联 SET NULL；都不为库外坏数据扫描全站。
    return await db.scalar(union_all(*issues).limit(1)) is not None


async def _purge_setting_history(db: AsyncSession, project_id: int) -> None:
    """父作品已锁：先清理字段事件，避免 PG 级联定义与字段的次序冲突。"""
    # 其他作品的旧坏数据可能用普通外键指向本书的主体或章锚。仅检查一次会留下
    # 检查后并发插入的窗口；先锁本书中可被引用的父行，使 FK 的 KEY SHARE 等待。
    # SQLite 的父项目 UPDATE 已持有全库写锁，不需要再做这些无行锁能力的查询。
    if db.get_bind().dialect.name == "postgresql":
        chapter_ids = select(Chapter.id).where(Chapter.project_id == project_id)
        parents = [
            (Character, select(Character.id).where(Character.project_id == project_id)),
            (WorldSetting, select(WorldSetting.id).where(WorldSetting.project_id == project_id)),
            (Organization, select(Organization.id).where(Organization.project_id == project_id)),
            (Career, select(Career.id).where(Career.project_id == project_id)),
            (Chapter, select(Chapter.id).where(Chapter.project_id == project_id)),
            (Scene, select(Scene.id).where(Scene.chapter_id.in_(chapter_ids))),
            (ChapterAnalysis, select(ChapterAnalysis.id).where(ChapterAnalysis.project_id == project_id)),
            (AnalysisChange, select(AnalysisChange.id).where(AnalysisChange.project_id == project_id)),
        ]
        for model, statement in parents:
            await db.execute(statement.order_by(model.id).with_for_update(of=model))
    timeline_ids = select(SettingTimeline.id).where(SettingTimeline.project_id == project_id)
    definition_ids = select(SettingFieldDefinition.id).where(SettingFieldDefinition.project_id == project_id)
    relevant = union_all(
        select(SettingFieldDefinition.id).where(SettingFieldDefinition.project_id == project_id),
        select(SettingTextField.id).where(SettingTextField.timeline_id.in_(timeline_ids)),
        select(SettingProgression.id).where(SettingProgression.timeline_id.in_(timeline_ids)),
    ).limit(1)
    if await db.scalar(relevant) is None:
        # 空字段目录无需锁全图，但其他作品的异常事件仍可能指向本书主体或章锚。
        if await _has_unsafe_setting_references(db, project_id, incoming_only=True):
            raise ConflictError("设定数据存在跨作品引用，无法安全彻底删除",
                                extra={"code": "invalid_setting_purge_graph"})
        return

    # 固定顺序仅锁目标身份，阻止新增指向待删字段/事件的外键，不等待其他作品的锁。
    locks = [
        (SettingTimeline, select(SettingTimeline.id).where(SettingTimeline.project_id == project_id)),
        (SettingFieldDefinition, select(SettingFieldDefinition.id).where(
            SettingFieldDefinition.project_id == project_id)),
        (SettingFieldOption, select(SettingFieldOption.id).where(SettingFieldOption.definition_id.in_(definition_ids))),
        (SettingTextField, select(SettingTextField.id).where(SettingTextField.timeline_id.in_(timeline_ids))),
        (SettingProgression, select(SettingProgression.id).where(SettingProgression.timeline_id.in_(timeline_ids))),
    ]
    for model, statement in locks:
        await db.execute(statement.order_by(model.id).with_for_update(of=model))
    if await _has_unsafe_setting_references(db, project_id):
        raise ConflictError("设定数据存在跨作品引用，无法安全彻底删除",
                            extra={"code": "invalid_setting_purge_graph"})

    await db.execute(delete(SettingProgression).where(SettingProgression.timeline_id.in_(timeline_ids)))
    await db.execute(delete(SettingTextField).where(SettingTextField.timeline_id.in_(timeline_ids)))


async def restore(db: AsyncSession, actor_id: int, project_id: int, deleted_at: datetime) -> None:
    record = await project_lifecycle.load_record(db, project_id)
    if record is None:
        raise NotFoundError("项目不存在")
    await authz.require(db, actor_id, "recycle_bin.restore", write=True, targets=(record.user_id,))
    async with AuditOperation(
        db, actor_id, "project.restore", "project", project_id, project_id=project_id, include_own_project=True
    ) as audit:
        project = await project_lifecycle.lock_record(db, project_id)
        _check_record(project, deleted_at)
        if project_lifecycle.now_utc() >= project_lifecycle.expires_at(project.deleted_at):
            raise ConflictError("作品已超过 30 天保留期，无法恢复", extra={"code": "recycle_expired"})
        if await db.get(User, project.user_id) is None:
            raise ConflictError("所属用户不存在，无法恢复作品")
        await _actor(db, actor_id, admin=True, permission="recycle_bin.restore")
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
) -> bool:
    system = actor_id is None and cutoff is not None
    if _has_pending_session_writes(db):
        await _reject_pending_purge(db, actor_id, project_id, system=system)
    project = None
    if system:
        try:
            project = await project_lifecycle.lock_record(db, project_id)
            _check_record(project, deleted_at)
        except (NotFoundError, ConflictError):
            # 扫描与取得父行锁之间可能已由另一实例清理，或有人恢复了作品。
            # 尚未进入审计和清理阶段，因此没有失败事件需要记录。
            return False
    else:
        if actor_id is None:
            raise ForbiddenError("需要管理员权限")
        record = await project_lifecycle.load_record(db, project_id)
        if record is None:
            raise NotFoundError("项目不存在")
        await authz.require(db, actor_id, "recycle_bin.purge", write=True, targets=(record.user_id,))
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
        if project is None:
            project = await project_lifecycle.lock_record(db, project_id)
            _check_record(project, deleted_at)
        if system:
            if project_lifecycle.expires_at(project.deleted_at) > cutoff:
                raise ConflictError("作品尚未到期")
        else:
            await _actor(db, actor_id, admin=True, permission="recycle_bin.purge")
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
        await _purge_setting_history(db, project_id)
        media_ids = await media_cleanup.mark_project_deleted(db, project_id)
        await db.delete(project)
        await audit.commit()
        # commit 期间若仍出现尚未核实的收尾证据，不能通过清理内存掩盖它。
        if not engine.has_cleanup_failure(project_id):
            engine.forget_project_hubs(list(task_keys), statistics_keys=task_keys)
        retrieval_service.invalidate(project_id)
    if media_ids:
        try:
            await media_cleanup.sweep(engine.ctx.sessionmaker, object_ids=media_ids)
        except Exception:
            logger.warning("作品已彻删，剩余媒体由定时任务回收 project=%s", project_id)
    return True


async def _release_abandoned_deletion_intents(db: AsyncSession) -> None:
    """只释放已持久确认停止或围栏的发起者留下的同一删除请求。"""
    abandoned = workers.confirmed_terminal(Project.deletion_worker_key)
    rows = (await db.execute(select(Project.id, Project.deletion_token, Project.deletion_worker_key).where(
        Project.deleted_at.is_(None), Project.deletion_token.is_not(None), abandoned,
    ))).all()
    for project_id, token, worker_key in rows:
        await db.execute(update(Project).where(
            Project.id == project_id, Project.deleted_at.is_(None),
            Project.deletion_token == token, Project.deletion_worker_key == worker_key,
        ).values(deletion_started_at=None, deletion_token=None, deletion_worker_key=None,
                 updated_at=Project.updated_at))


async def recover_pending_deletions(
    sessionmaker: async_sessionmaker[AsyncSession], *, seconds: float = 30, workers_checked: bool = False,
) -> None:
    for intent in tuple(_failed_releases.get(sessionmaker, ())):
        await _release_intent(sessionmaker, intent)
    async with sessionmaker() as db:
        if await db.scalar(select(Project.id).where(Project.deletion_worker_key.is_not(None)).limit(1)) is None:
            return
        if not workers_checked:
            await workers.prepare_orphan_scan(db, seconds)
            # 围栏/worker 锁先释放，随后才做作品更新，锁顺序与删除请求兼容。
            await db.commit()
        await _release_abandoned_deletion_intents(db)
        await db.commit()


async def purge_expired(engine: "TaskEngine", *, now: datetime | None = None) -> int:
    cutoff = project_lifecycle.as_utc(now) if now is not None else project_lifecycle.now_utc()
    deleted_before = cutoff - timedelta(days=project_lifecycle.RETENTION_DAYS)
    cursor: tuple[datetime, int] | None = None
    count = 0

    async def purge_one(project_id: int, stamp: datetime) -> bool:
        async with engine.ctx.sessionmaker() as db:
            return await purge(db, engine, None, project_id, stamp, cutoff=cutoff)

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
                if await retry_contended_write(partial(purge_one, pid, stamp)):
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
            await retry_contended_write(
                partial(recover_pending_deletions, self.engine.ctx.sessionmaker, seconds=self.engine.lease_seconds),
            )
            await purge_expired(self.engine)
        except Exception:
            logger.error("回收站定时清理暂时失败，下个周期重试")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task
