"""管理员用户管理。

本模块只返回账号元数据和基础资源计数；项目内容权限由项目管理服务提供。
护栏：不能对自己禁用 / 取消管理员 / 删除；任何操作后必须至少保留一名启用中的管理员。
"""

from anyio import to_thread
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.security import hash_password
from app.models import Project, Task, User
from app.schemas.admin import (
    AdminResourceUsage,
    AdminUserCreate,
    AdminUserDetail,
    AdminUserList,
    AdminUserOut,
    AdminUserUpdate,
    AdminUserUsage,
    ResetPasswordRequest,
)
from app.services import auth_service, project_lifecycle, task_statistics_service
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation
from app.services.project_service import chapter_stats_subquery

SELF_FORBIDDEN = "不能对自己执行该操作"
LAST_ADMIN = "至少需要保留一名启用中的管理员"


def _project_count_subquery():
    return (
        select(Project.user_id.label("user_id"), func.count(Project.id).label("project_count"))
        .group_by(Project.user_id)
        .subquery()
    )


def _to_out(access: authz.AuthorizationSnapshot, project_count: int) -> AdminUserOut:
    return AdminUserOut(
        **access.target(), id=access.id, username=access.username, is_admin=access.is_super_admin,
        is_active=access.is_active, created_at=access.created_at, project_count=project_count,
    )


async def _user_out(db: AsyncSession, user: User) -> AdminUserOut:
    counts = _project_count_subquery()
    rows = (await db.execute(
        authz.snapshot_statement(include_token_version=False).outerjoin(counts, counts.c.user_id == User.id)
        .add_columns(func.coalesce(counts.c.project_count, 0).label("project_count"))
        .where(User.id == user.id)
    )).all()
    if not rows:
        raise NotFoundError("用户不存在")
    return _to_out(authz.snapshots_from_rows(rows)[user.id], int(rows[0].project_count))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_users(db: AsyncSession, *, q: str | None, page: int, page_size: int) -> AdminUserList:
    conditions = []
    keyword = (q or "").strip()
    if keyword:
        conditions.append(User.username.ilike(f"%{_escape_like(keyword)}%", escape="\\"))
    total = await db.scalar(select(func.count(User.id)).where(*conditions))
    counts = _project_count_subquery()
    page_ids = (
        select(User.id).where(*conditions).order_by(User.id)
        .offset((page - 1) * page_size).limit(page_size).subquery()
    )
    rows = (await db.execute(
        authz.snapshot_statement(include_token_version=False).join(page_ids, page_ids.c.id == User.id)
        .outerjoin(counts, counts.c.user_id == User.id)
        .add_columns(func.coalesce(counts.c.project_count, 0).label("project_count"))
        .order_by(User.id)
    )).all()
    accesses = authz.snapshots_from_rows(rows)
    usage = {row.id: int(row.project_count) for row in rows}
    return AdminUserList(items=[_to_out(access, usage[uid]) for uid, access in accesses.items()], total=int(total or 0))


async def get_user_detail(db: AsyncSession, user_id: int) -> AdminUserDetail:
    """单快照安全投影；章节先按作品聚合，避免作品数被章数放大。"""
    chapters = chapter_stats_subquery()
    groups = {
        "active": and_(*project_lifecycle.active_conditions()),
        "recycled": Project.deleted_at.is_not(None),
        "deleting": and_(Project.deleted_at.is_(None), Project.deletion_started_at.is_not(None)),
    }
    metrics = {
        "project_count": 1,
        "chapter_count": func.coalesce(chapters.c.chapter_count, 0),
        "total_words": func.coalesce(chapters.c.total_words, 0),
    }
    names = [f"{group}_{metric}" for group in groups for metric in metrics]
    counts = (
        select(
            Project.user_id,
            func.count(Project.id).label("project_count"),
            *(
                func.sum(case((condition, value), else_=0)).label(f"{group}_{metric}")
                for group, condition in groups.items() for metric, value in metrics.items()
            ),
        )
        .outerjoin(chapters, chapters.c.project_id == Project.id)
        .where(Project.user_id == user_id)
        .group_by(Project.user_id)
        .subquery()
    )
    rows = (await db.execute(
        authz.snapshot_statement(include_token_version=False).outerjoin(counts, counts.c.user_id == User.id)
        .add_columns(
            User.last_login_at, func.coalesce(counts.c.project_count, 0).label("project_count"),
            *(func.coalesce(counts.c[name], 0).label(name) for name in names),
        )
        .where(User.id == user_id)
    )).all()
    if not rows:
        raise NotFoundError("用户不存在")
    row = rows[0]._mapping
    access = authz.snapshots_from_rows(rows)[user_id]
    usage = {
        group: AdminResourceUsage(**{metric: int(row[f"{group}_{metric}"]) for metric in metrics})
        for group in groups
    }
    return AdminUserDetail(
        **_to_out(access, int(row["project_count"])).model_dump(),
        last_login_at=row["last_login_at"], usage=AdminUserUsage(**usage),
    )


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("用户不存在")
    return user


async def _ensure_other_active_admin(db: AsyncSession, target_id: int) -> None:
    """调用者已持共同授权gate；只读确认，不在升序User锁之后再无序加锁。

    所有超管身份/启用状态变更及bootstrap争用同一gate，故另一个启用超管
    不能在这次确认与提交之间被并发撤回；SQLite也依赖真实UPDATE而非FOR UPDATE。
    """
    if "authorization_gate_lease" not in db.info:
        raise RuntimeError("最后启用超管检查必须持有授权gate")
    other = await db.scalar(select(User.id).where(
        User.id != target_id, User.is_admin.is_(True), User.is_active.is_(True),
    ).limit(1))
    if other is None:
        raise ConflictError(LAST_ADMIN)


async def create_user(db: AsyncSession, actor: User, data: AdminUserCreate) -> AdminUserOut:
    await authz.require(db, actor, "users.create", write=True)
    async with AuditOperation(db, actor, "user.create", "user") as audit:
        audit.values["target_label"] = data.username
        user = await auth_service.create_user_uncommitted(db, data)
        audit.target(user)
        audit.summary.changed_fields = ["username", "is_admin", "is_active"]
        await audit.commit()
        await db.refresh(user)
        return await _user_out(db, user)


async def update_user(db: AsyncSession, actor: User, user_id: int, data: AdminUserUpdate) -> AdminUserOut:
    current = await authz.require(db, actor, "users.status", write=True, targets=(user_id,))
    async with AuditOperation(db, actor, "user.update", "user", user_id) as audit:
        await authz.protect_target(db, current, user_id)
        user = await db.get(User, user_id, populate_existing=True)
        audit.target(user)
        audit.track(user, ["is_active"])
        new_active = user.is_active if data.is_active is None else data.is_active
        if user.id == actor.id and not new_active:
            raise BadRequestError(SELF_FORBIDDEN)
        if user.is_admin and user.is_active and not new_active:
            await _ensure_other_active_admin(db, user.id)
        if user.is_active and not new_active:
            user.token_version = User.token_version + 1
        if user.is_active != new_active:
            await authz.advance(db)
        user.is_active = new_active
        await audit.commit()
        await db.refresh(user)
        return await _user_out(db, user)


async def reset_password(db: AsyncSession, actor: User, user_id: int, data: ResetPasswordRequest) -> None:
    current = await authz.require(db, actor, "users.reset_password", write=True, targets=(user_id,))
    async with AuditOperation(db, actor, "user.reset_password", "user", user_id) as audit:
        await authz.protect_target(db, current, user_id)
        user = await _get_user(db, user_id)
        audit.target(user)
        user.password_hash = await to_thread.run_sync(hash_password, data.new_password)
        # SQL 端自增，避免并发的禁用 / 改密基于旧值写回同一版本号
        user.token_version = User.token_version + 1
        await audit.commit()


async def delete_user(db: AsyncSession, actor: User, user_id: int) -> None:
    """只允许删除已无任何作品的用户；私人配置沿既有外键级联清理。"""
    current = await authz.require(db, actor, "users.delete", write=True, targets=(user_id,))
    async with AuditOperation(db, actor, "user.delete", "user", user_id) as audit:
        await authz.protect_target(db, current, user_id)
        user = await _get_user(db, user_id)
        audit.target(user)
        if user.id == actor.id:
            raise BadRequestError(SELF_FORBIDDEN)
        if user.is_admin and user.is_active:
            await _ensure_other_active_admin(db, user.id)
        # 与任务取消受理采用同一父用户真实写屏障，SQLite同样生效。
        await db.execute(update(User).where(User.id == user_id).values(id=User.id))
        count = int(await db.scalar(select(func.count(Project.id)).where(Project.user_id == user_id)) or 0)
        if count:
            raise ConflictError(
                "该用户名下仍有作品（含回收站），请先禁用账号或彻底清理作品后再删除用户",
                extra={"code": "user_has_projects", "project_count": count},
            )
        if await db.scalar(select(Task.id).where(
            Task.user_id == user_id, Task.admin_cancel_operation_id.is_not(None)
        ).limit(1)) is not None:
            raise ConflictError(
                "该用户仍有取消请求尚未安全收尾，暂不能删除账号", extra={"code": "user_has_pending_tasks"}
            )
        await task_statistics_service.mark_removed(db, Task.user_id == user_id)
        await authz.advance(db)
        await db.delete(user)
        await audit.commit(flush_fn=_flush_user_delete)


async def _flush_user_delete(db: AsyncSession) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        original = exc.orig
        cause = getattr(original, "__cause__", None)
        name = getattr(getattr(cause, "diag", None), "constraint_name", None) or getattr(cause, "constraint_name", None)
        # SQLite 的 RESTRICT 由内部触发器实现，可能使用 1811 而不是普通外键错误 787。
        # 不把其他用户自定义 trigger / IntegrityError 笼统翻译成有作品冲突。
        sqlite_code = getattr(original, "sqlite_errorcode", None)
        sqlite_fk = sqlite_code == 787 or (
            sqlite_code == 1811 and str(original) == "FOREIGN KEY constraint failed"
        )
        if name == "fk_projects_user_id_users" or sqlite_fk:
            raise ConflictError(
                "该用户名下仍有作品（含回收站），请先彻底清理作品后再删除用户",
                extra={"code": "user_has_projects"},
            ) from exc
        raise
