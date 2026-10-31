"""管理员用户管理。

本模块只返回账号元数据（用户名、角色、状态、项目数）；项目内容权限由项目管理服务提供。
护栏：不能对自己禁用 / 取消管理员 / 删除；任何操作后必须至少保留一名启用中的管理员。
"""

from anyio import to_thread
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.security import hash_password
from app.models import Project, Task, User
from app.schemas.admin import AdminUserCreate, AdminUserList, AdminUserOut, AdminUserUpdate, ResetPasswordRequest
from app.services import auth_service, task_statistics_service
from app.services.audit_writer import AuditOperation

SELF_FORBIDDEN = "不能对自己执行该操作"
LAST_ADMIN = "至少需要保留一名启用中的管理员"


def _project_count_subquery():
    return (
        select(Project.user_id.label("user_id"), func.count(Project.id).label("project_count"))
        .group_by(Project.user_id)
        .subquery()
    )


def _to_out(user: User, project_count: int) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        is_active=user.is_active,
        created_at=user.created_at,
        project_count=project_count,
    )


async def _user_out(db: AsyncSession, user: User) -> AdminUserOut:
    count = await db.scalar(select(func.count(Project.id)).where(Project.user_id == user.id))
    return _to_out(user, int(count or 0))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_users(db: AsyncSession, *, q: str | None, page: int, page_size: int) -> AdminUserList:
    conditions = []
    keyword = (q or "").strip()
    if keyword:
        conditions.append(User.username.ilike(f"%{_escape_like(keyword)}%", escape="\\"))
    total = await db.scalar(select(func.count(User.id)).where(*conditions))
    counts = _project_count_subquery()
    rows = await db.execute(
        select(User, func.coalesce(counts.c.project_count, 0))
        .outerjoin(counts, counts.c.user_id == User.id)
        .where(*conditions)
        .order_by(User.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return AdminUserList(items=[_to_out(u, int(c)) for u, c in rows.all()], total=int(total or 0))


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("用户不存在")
    return user


async def _ensure_other_active_admin(db: AsyncSession, target_id: int) -> None:
    """目标即将不再是启用中的管理员时调用：同一事务内锁住启用中的管理员行再计数。

    PostgreSQL 下 FOR UPDATE 让并发的互相降级串行化（后到者重读时已看不到被降级的行）；
    SQLite 不支持行锁（语句被方言忽略），测试环境单连接串行，不影响正确性。
    """
    admin_ids = (
        await db.scalars(select(User.id).where(User.is_admin.is_(True), User.is_active.is_(True)).with_for_update())
    ).all()
    if not any(uid != target_id for uid in admin_ids):
        raise ConflictError(LAST_ADMIN)


async def create_user(db: AsyncSession, actor: User, data: AdminUserCreate) -> AdminUserOut:
    async with AuditOperation(db, actor, "user.create", "user") as audit:
        audit.values["target_label"] = data.username
        user = await auth_service.create_user_uncommitted(db, data, is_admin=data.is_admin)
        audit.target(user)
        audit.summary.changed_fields = ["username", "is_admin", "is_active"]
        await audit.commit()
        await db.refresh(user)
        return _to_out(user, 0)


async def update_user(db: AsyncSession, actor: User, user_id: int, data: AdminUserUpdate) -> AdminUserOut:
    async with AuditOperation(db, actor, "user.update", "user", user_id) as audit:
        user = await _get_user(db, user_id)
        audit.target(user)
        audit.track(user, ["is_admin", "is_active"])
        new_admin = user.is_admin if data.is_admin is None else data.is_admin
        new_active = user.is_active if data.is_active is None else data.is_active
        if user.id == actor.id and (not new_admin or not new_active):
            raise BadRequestError(SELF_FORBIDDEN)
        if user.is_admin and user.is_active and not (new_admin and new_active):
            await _ensure_other_active_admin(db, user.id)
        if user.is_active and not new_active:
            # 禁用：已登录会话立即失效（SQL 端自增，避免与并发的重置密码互相覆盖）
            user.token_version = User.token_version + 1
        user.is_admin = new_admin
        user.is_active = new_active
        await audit.commit()
        await db.refresh(user)
        return await _user_out(db, user)


async def reset_password(db: AsyncSession, actor: User, user_id: int, data: ResetPasswordRequest) -> None:
    async with AuditOperation(db, actor, "user.reset_password", "user", user_id) as audit:
        user = await _get_user(db, user_id)
        audit.target(user)
        user.password_hash = await to_thread.run_sync(hash_password, data.new_password)
        # SQL 端自增，避免并发的禁用 / 改密基于旧值写回同一版本号
        user.token_version = User.token_version + 1
        await audit.commit()


async def delete_user(db: AsyncSession, actor: User, user_id: int) -> None:
    """只允许删除已无任何作品的用户；私人配置沿既有外键级联清理。"""
    async with AuditOperation(db, actor, "user.delete", "user", user_id) as audit:
        user = await _get_user(db, user_id)
        audit.target(user)
        if user.id == actor.id:
            raise BadRequestError(SELF_FORBIDDEN)
        if user.is_admin and user.is_active:
            await _ensure_other_active_admin(db, user.id)
        await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        count = int(await db.scalar(select(func.count(Project.id)).where(Project.user_id == user_id)) or 0)
        if count:
            raise ConflictError(
                "该用户名下仍有作品（含回收站），请先禁用账号或彻底清理作品后再删除用户",
                extra={"code": "user_has_projects", "project_count": count},
            )
        await task_statistics_service.mark_removed(db, Task.user_id == user_id)
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
