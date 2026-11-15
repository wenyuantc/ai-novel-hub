"""项目访问来源和动作能力；旧严格作者及管理员 helper 语义不变。"""

from hashlib import sha256
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import Project, ProjectCollaborator
from app.schemas.project import ProjectAccessOut
from app.services import ai_dispatch, project_lifecycle
from app.services import authorization_service as authz

Capability = Literal[
    "content.read", "content.edit", "export", "ai.use", "collaborators.manage", "project.recycle",
    "project.backup", "reading_progress.write",
]
_FIELD = {
    "content.read": "can_read",
    "content.edit": "can_edit_content",
    "export": "can_export",
    "ai.use": "can_use_ai",
    "collaborators.manage": "can_manage_collaborators",
    "project.recycle": "can_recycle",
    "project.backup": "can_backup",
    "reading_progress.write": "can_write_reading_progress",
}
_OWNER_ONLY = frozenset({"collaborators.manage", "project.backup", "reading_progress.write"})


def no_access() -> ProjectAccessOut:
    return ProjectAccessOut(
        role=None, is_collaborator=False, allow_owner_ai=False, can_read=False, can_edit_content=False,
        can_export=False,
        can_use_ai=False, can_manage_collaborators=False, can_recycle=False, can_backup=False,
        can_write_reading_progress=False,
    )


def access_for(
    project: Project, actor_id: int | None, member: ProjectCollaborator | None,
    snapshot: authz.AuthorizationSnapshot | None,
) -> ProjectAccessOut:
    if actor_id is None or snapshot is None or not snapshot.is_active:
        return no_access()
    owner = project.user_id == actor_id
    accepted = bool(member is not None and member.user_id == actor_id and member.status == "accepted")
    admin_read = snapshot.allows("projects.read")
    admin_edit = admin_read and snapshot.allows("projects.edit")
    admin_recycle = admin_read and snapshot.allows("projects.recycle")
    editor = bool(accepted and member is not None and member.role == "editor")
    role = "owner" if owner else member.role if accepted and member is not None else "admin" if admin_read else None
    authorization_version = None
    if accepted and not owner and member is not None:
        identity = f"{project.id}:{actor_id}:{member.invitation_id}:{member.revision}".encode()
        authorization_version = sha256(identity).hexdigest()
    return ProjectAccessOut(
        role=role, is_collaborator=accepted and not owner,
        authorization_version=authorization_version,
        allow_owner_ai=bool(accepted and member is not None and member.allow_owner_ai),
        can_read=owner or accepted or admin_read,
        can_edit_content=owner or editor or admin_edit, can_export=owner or accepted,
        can_use_ai=owner or editor, can_manage_collaborators=owner, can_recycle=owner or admin_recycle,
        can_backup=owner, can_write_reading_progress=owner,
    )


async def resolve_access(
    db: AsyncSession, actor_id: int, project_id: int, *, for_update: bool = False,
    visibility_write: bool = False,
) -> tuple[Project, ProjectAccessOut, ProjectCollaborator | None]:
    """写路径先取认证/User 屏障，再按需取发送门禁和项目锁，锁后重读授权。"""
    if visibility_write and not for_update:
        raise ValueError("可见性写门禁必须同时锁定项目")
    sqlite = db.get_bind().dialect.name == "sqlite"
    timed_visibility = sqlite and for_update and visibility_write
    try:
        # 包含认证异常分支和锁后成员重读；授权完成前不得自动 flush 调用方暂存的 ORM 数据。
        with db.no_autoflush:
            timeout = int(await db.scalar(text("PRAGMA busy_timeout")) or 0) if timed_visibility else None
            if timed_visibility:
                # SQLite 认证屏障也取得全库写锁；发送门禁的限时须在认证之前生效。
                await db.execute(text(f"PRAGMA busy_timeout = {int(ai_dispatch._LOCK_WAIT_SECONDS * 1000)}"))
            try:
                project = await project_lifecycle.load_record(db, project_id)
                if project is None:
                    raise NotFoundError("项目不存在")
                snapshot = await authz.require_authenticated(db, actor_id, write=for_update, targets=(project.user_id,))
                if for_update:
                    if visibility_write:
                        await ai_dispatch.lock_visibility_write(db, project_id)
                    project = await project_lifecycle.lock_record(db, project_id)
                else:
                    project = await project_lifecycle.load_record(db, project_id)
                    if project is None:
                        raise NotFoundError("项目不存在")
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == actor_id,
                ).execution_options(populate_existing=True))
                access = access_for(project, actor_id, member, snapshot)
                if not access.can_read:
                    raise NotFoundError("项目不存在")
                if project.deleted_at is not None:
                    raise NotFoundError("项目不存在")
                project_lifecycle.ensure_active(project)
                return project, access, member
            finally:
                if timeout is not None:
                    # 回滚会把连接归还池，必须先恢复这条连接自身的 PRAGMA。
                    await db.execute(text(f"PRAGMA busy_timeout = {timeout}"))
    except OperationalError as exc:
        if sqlite and ai_dispatch.is_sqlite_busy(exc):
            await db.rollback()
            if timed_visibility:
                raise ai_dispatch.VisibilityBusyError() from None
            raise ConflictError("协作状态正在变化，请刷新后重试", extra={"code": "collaboration_busy"}) from None
        raise


async def require_project(
    db: AsyncSession, actor_id: int, project_id: int, capability: Capability, *, for_update: bool = False,
    visibility_write: bool = False, grant_identity: tuple[str, int] | None = None,
) -> Project:
    if capability not in _FIELD:
        raise ValueError("未知作品能力")
    # 能力判定中可能再次查询认证状态；任何失败分支都不能提前 flush 调用方待写内容。
    with db.no_autoflush:
        project, access, member = await resolve_access(
            db, actor_id, project_id, for_update=for_update, visibility_write=visibility_write,
        )
        if grant_identity is not None and (
            member is None or member.status != "accepted"
            or (member.invitation_id, member.revision) != grant_identity
        ):
            raise ForbiddenError("协作授权已变化", extra={"code": "permission_denied"})
        if not getattr(access, _FIELD[capability]):
            if capability in _OWNER_ONLY or (capability in {"export", "ai.use"} and not access.is_collaborator):
                raise NotFoundError("项目不存在")
            snapshot = await authz.require_authenticated(db, actor_id)
            if capability in {"content.edit", "project.recycle"} and snapshot.allows("projects.read"):
                permission = "projects.edit" if capability == "content.edit" else "projects.recycle"
                raise ForbiddenError("没有执行该操作的权限", extra={
                    "code": "permission_denied", "required_permission": permission,
                })
            if capability == "project.recycle":
                raise NotFoundError("项目不存在")
            raise ForbiddenError("当前作品没有执行该操作的权限", extra={"code": "project_readonly"})
        return project
