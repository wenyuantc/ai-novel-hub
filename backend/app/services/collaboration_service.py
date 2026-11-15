"""作品与系列邀请：状态、轮次及审计使用同一父行事务屏障。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import anyio
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import Project, ProjectCollaborator, Series, SeriesCollaborator, User
from app.schemas.collaboration import InvitationOut, MemberOut, ProjectMemberUpdate, Scope
from app.services import ai_dispatch, project_lifecycle
from app.services import authorization_service as authz
from app.services.collaboration_audit import AuditSnapshot, add_success, get_result, record_failure

logger = logging.getLogger(__name__)
ProjectChangeNotifier = Callable[[int, int], None]


class CollaborationOperationError(AppError):
    status_code = 500


def _stale() -> ConflictError:
    return ConflictError("邀请状态已变化，请刷新后重试", extra={"code": "stale_invitation"})


def _unavailable() -> BadRequestError:
    return BadRequestError("该用户无法接受邀请", extra={"code": "invitee_unavailable"})


def _model(scope: Scope):
    return ProjectCollaborator if scope == "project" else SeriesCollaborator


def _parent_field(scope: Scope):
    return ProjectCollaborator.project_id if scope == "project" else SeriesCollaborator.series_id


def _membership_conflict(exc: IntegrityError, scope: Scope) -> bool:
    """只把同一作品/系列与用户的唯一竞争翻译成重复邀请。"""
    table = f"{scope}_collaborators"
    original = exc.orig
    if str(original) == f"UNIQUE constraint failed: {table}.{scope}_id, {table}.user_id":
        return True
    return any(
        getattr(error, "sqlstate", None) == "23505"
        and getattr(error, "constraint_name", None) == f"uq_{table}_{scope}_id"
        for error in (original, getattr(original, "__cause__", None))
    )


def _out(member: ProjectCollaborator | SeriesCollaborator, username: str) -> MemberOut:
    return MemberOut(
        user_id=member.user_id, username=username, role=member.role, status=member.status,
        allow_owner_ai=member.allow_owner_ai if isinstance(member, ProjectCollaborator) else None,
        invitation_id=member.invitation_id, revision=member.revision, responded_at=member.responded_at,
        created_at=member.created_at, updated_at=member.updated_at,
    )


async def _reconcile_failure(
    db: AsyncSession, factory: async_sessionmaker[AsyncSession], state: dict[str, Any], code: str,
    released: list[bool],
) -> str | None:
    rollback_safe = True
    try:
        await db.rollback()
    except Exception:
        # 连接异常时先失效并释放原连接；不能在未确认事务结束时补造失败记录。
        try:
            await db.invalidate()
        except Exception:
            rollback_safe = False
    # 失败审计查询也可能抛错；将释放事实独立记录，不能把未知连接状态当作释放。
    released[0] = rollback_safe
    if rollback_safe:
        return await record_failure(factory, AuditSnapshot(**state), code)
    # 原连接是否提交不确定，只读取独立审计结果；查不到时不得写入失败。
    return await get_result(factory, state["operation_id"])


async def _reconcile_guarded(
    db: AsyncSession, factory: async_sessionmaker[AsyncSession], state: dict[str, Any], code: str,
    released: list[bool],
) -> str | None:
    with anyio.CancelScope(shield=True):
        return await _reconcile_failure(db, factory, state, code, released)


async def _reconcile_protected(
    db: AsyncSession, factory: async_sessionmaker[AsyncSession], state: dict[str, Any], code: str,
    released: list[bool],
) -> tuple[str | None, asyncio.CancelledError | None, bool]:
    # 原调用方无论被取消多少次，独立任务仍需完成事务回滚和审计核验。
    cleanup = asyncio.create_task(_reconcile_guarded(db, factory, state, code, released))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(cleanup), cancelled, False
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
            if cleanup.cancelled():
                return None, cancelled, True
        except Exception:
            return None, cancelled, True


def _notify_project_change(state: dict[str, Any], notify: ProjectChangeNotifier | None) -> None:
    """仅发无内容的本进程唤醒；通知失败不得改变已提交业务的事实。"""
    project_id, target_id = state.get("resource_id"), state.get("target_user_id")
    if (notify is None or state.get("scope") != "project" or type(project_id) is not int or project_id <= 0
            or type(target_id) is not int or target_id <= 0):
        return
    try:
        notify(project_id, target_id)
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001
        # 回调仅触发本进程复验；不能输出异常原文，也不能把已提交操作记作失败。
        logger.warning("协作变更通知失败 error_type=%s", type(exc).__name__)


async def _write[T](
    db: AsyncSession, factory: async_sessionmaker[AsyncSession], actor: User, scope: Scope, resource_id: int,
    action: str, operation: Callable[[dict[str, Any]], Awaitable[T]],
    *, notify: ProjectChangeNotifier | None = None,
) -> T:
    if scope == "project" and not callable(notify):
        # 所有项目写调用方都必须显式接入本进程唤醒，不能新增一个静默漏通知的入口。
        raise ValueError("作品协作写入需要授权变更通知器")
    state: dict[str, Any] = {
        "operation_id": str(uuid4()), "scope": scope, "resource_id": resource_id, "action": action,
        "actor_user_id": actor.id, "actor_username": actor.username,
    }
    phase = "business"
    try:
        result = await operation(state)
        phase = "audit"
        await add_success(db, AuditSnapshot(**state))
        phase = "commit"
        await db.commit()
        _notify_project_change(state, notify)
        return result
    except asyncio.CancelledError:
        released = [False]
        recorded, _, audit_failed = await _reconcile_protected(db, factory, state, "cancelled", released)
        if recorded == "success" or (phase == "commit" and recorded is None and released[0]):
            # 连接已释放而结果未知时也可唤醒：通知不表示成功，订阅必须重新查库。
            _notify_project_change(state, notify)
        if audit_failed or recorded is None:
            logger.error("协作取消后审计核验失败或结果未知 operation=%s", state["operation_id"])
        raise
    except Exception as exc:
        if (phase == "business" and action == "invite" and isinstance(exc, IntegrityError)
                and _membership_conflict(exc, scope)):
            failure: AppError = ConflictError("协作状态已变化，请刷新后重试", extra={"code": "already_collaborator"})
        elif (phase == "business" and isinstance(exc, OperationalError) and db.get_bind().dialect.name == "sqlite"
              and ai_dispatch.is_sqlite_busy(exc)):
            failure = ConflictError("协作状态正在变化，请刷新后重试", extra={"code": "collaboration_busy"})
        elif phase == "business" and isinstance(exc, AppError):
            failure = exc
        else:
            failure = CollaborationOperationError("服务器内部错误")
        code = (failure.extra or {}).get("code") or {
            400: "bad_request", 401: "unauthorized", 403: "permission_denied", 404: "not_found", 409: "conflict",
        }.get(failure.status_code, "system_error")
        released = [False]
        recorded, cancelled, audit_failed = await _reconcile_protected(db, factory, state, str(code), released)
        if recorded == "success" or (phase == "commit" and recorded is None and released[0]):
            _notify_project_change(state, notify)
        if cancelled is not None:
            if audit_failed or recorded is None:
                logger.error("协作取消后审计核验失败或结果未知 operation=%s", state["operation_id"])
            raise cancelled from None
        if audit_failed:
            raise CollaborationOperationError("协作审计暂不可用，请稍后重试") from None
        if recorded == "success":
            raise ConflictError("操作结果已提交，请刷新后确认", extra={"code": "operation_confirmed"}) from None
        if recorded is None:
            raise CollaborationOperationError("协作审计暂不可用，请稍后重试") from None
        raise failure from None


async def _parent(db: AsyncSession, scope: Scope, resource_id: int) -> Project | Series | None:
    model = Project if scope == "project" else Series
    return await db.scalar(select(model).where(model.id == resource_id).execution_options(populate_existing=True))


async def _lock_parent(
    db: AsyncSession, actor_id: int, scope: Scope, resource_id: int, *, target_id: int | None = None,
    owner_only: bool = False,
) -> Project | Series:
    parent = await _parent(db, scope, resource_id)
    if parent is None:
        raise NotFoundError("作品或系列不存在")
    owner_id = parent.user_id if isinstance(parent, Project) else parent.owner_id
    targets = (owner_id,) if target_id is None else (owner_id, target_id)
    await authz.require_authenticated(db, actor_id, write=True, targets=targets)
    if scope == "project":
        await ai_dispatch.lock_visibility_write(db, resource_id)
        parent = await project_lifecycle.lock_active(db, resource_id)
        owner_id = parent.user_id
    else:
        result = await db.execute(update(Series).where(Series.id == resource_id)
                                  .values(id=Series.id, updated_at=Series.updated_at)
                                  .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise NotFoundError("系列不存在")
        parent = await _parent(db, scope, resource_id)
        if parent is None:
            raise NotFoundError("系列不存在")
        owner_id = parent.owner_id
    if owner_only and owner_id != actor_id:
        raise NotFoundError("作品或系列不存在")
    return parent


async def list_members(db: AsyncSession, actor_id: int, scope: Scope, resource_id: int) -> list[MemberOut]:
    await authz.require_authenticated(db, actor_id)
    parent = await _parent(db, scope, resource_id)
    if parent is None or (parent.user_id if isinstance(parent, Project) else parent.owner_id) != actor_id:
        raise NotFoundError("作品或系列不存在")
    if isinstance(parent, Project):
        project_lifecycle.ensure_active(parent)
    model = _model(scope)
    rows = (await db.execute(select(model, User.username).join(User, User.id == model.user_id)
                             .where(_parent_field(scope) == resource_id).order_by(model.id))).all()
    return [_out(member, username) for member, username in rows]


async def invite(
    db: AsyncSession, factory: async_sessionmaker[AsyncSession], actor: User, scope: Scope, resource_id: int,
    username: str, role: str = "editor", allow_owner_ai: bool = False,
    *, notify: ProjectChangeNotifier | None = None,
) -> MemberOut:
    async def operation(state: dict[str, Any]) -> MemberOut:
        target = await db.scalar(
            select(User).where(User.username == username).execution_options(populate_existing=True)
        )
        target_id = target.id if target is not None else None
        if target is not None:
            state.update(target_user_id=target.id, target_username=target.username)
        parent = await _lock_parent(db, actor.id, scope, resource_id, target_id=target_id, owner_only=True)
        state["resource_label"] = parent.title if isinstance(parent, Project) else parent.name
        if target_id is None:
            raise _unavailable()
        target = await db.scalar(select(User).where(User.id == target_id).execution_options(populate_existing=True))
        if target is None or not target.is_active or target.username != username:
            raise _unavailable()
        if target.id == actor.id:
            raise BadRequestError("不能邀请自己", extra={"code": "self_invite"})
        model = _model(scope)
        member = await db.scalar(select(model).where(
            _parent_field(scope) == resource_id, model.user_id == target.id,
        ).execution_options(populate_existing=True))
        if member is not None and member.status in ("pending", "accepted"):
            raise ConflictError("该用户已受邀或已是协作者", extra={"code": "already_collaborator"})
        invitation_id = str(uuid4())
        if member is None:
            values = {f"{scope}_id": resource_id, "user_id": target.id, "invitation_id": invitation_id,
                      "role": role if scope == "project" else "editor", "status": "pending",
                      "invited_by_user_id": actor.id}
            if scope == "project":
                values["allow_owner_ai"] = allow_owner_ai
            member = model(**values)
            db.add(member)
        else:
            member.invitation_id, member.status, member.revision = invitation_id, "pending", member.revision + 1
            member.responded_at, member.invited_by_user_id = None, actor.id
            member.role = role if scope == "project" else "editor"
            if isinstance(member, ProjectCollaborator):
                member.allow_owner_ai = allow_owner_ai
        state.update(invitation_id=invitation_id, summary={"after_role": member.role, "after_status": "pending"})
        if isinstance(member, ProjectCollaborator):
            state["summary"]["after_allow_owner_ai"] = member.allow_owner_ai
        await db.flush()
        await db.refresh(member)
        return _out(member, target.username)

    return await _write(db, factory, actor, scope, resource_id, "invite", operation, notify=notify)


async def change_member(
    db: AsyncSession, factory: async_sessionmaker[AsyncSession], actor: User, scope: Scope, resource_id: int,
    target_id: int, expected_revision: int, *, body: ProjectMemberUpdate | None = None,
    notify: ProjectChangeNotifier | None = None,
) -> MemberOut | None:
    action = "modify" if body is not None else "revoke"

    async def operation(state: dict[str, Any]) -> MemberOut | None:
        parent = await _lock_parent(db, actor.id, scope, resource_id, target_id=target_id, owner_only=True)
        state["resource_label"] = parent.title if isinstance(parent, Project) else parent.name
        target = await db.scalar(select(User).where(User.id == target_id).execution_options(populate_existing=True))
        model = _model(scope)
        member = await db.scalar(select(model).where(
            _parent_field(scope) == resource_id, model.user_id == target_id,
        ).execution_options(populate_existing=True))
        if member is None or target is None:
            raise NotFoundError("协作者不存在")
        state.update(target_user_id=target.id, target_username=target.username, invitation_id=member.invitation_id)
        if member.revision != expected_revision or member.status not in ("pending", "accepted"):
            raise _stale()
        summary: dict[str, Any] = {"before_role": member.role, "before_status": member.status}
        if isinstance(member, ProjectCollaborator):
            summary["before_allow_owner_ai"] = member.allow_owner_ai
        state["summary"] = summary
        if body is None:
            member.status = "revoked"
        else:
            assert isinstance(member, ProjectCollaborator)
            if body.role is not None:
                member.role = body.role
            if body.allow_owner_ai is not None:
                member.allow_owner_ai = body.allow_owner_ai
        member.revision += 1
        summary.update(after_role=member.role, after_status=member.status)
        if isinstance(member, ProjectCollaborator):
            summary["after_allow_owner_ai"] = member.allow_owner_ai
        await db.flush()
        await db.refresh(member)
        return _out(member, target.username) if body is not None else None

    return await _write(db, factory, actor, scope, resource_id, action, operation, notify=notify)


async def list_invitations(db: AsyncSession, actor_id: int) -> list[InvitationOut]:
    await authz.require_authenticated(db, actor_id)
    projects = (await db.execute(select(ProjectCollaborator, Project, User.username)
        .join(Project, Project.id == ProjectCollaborator.project_id).join(User, User.id == Project.user_id)
        .where(ProjectCollaborator.user_id == actor_id, ProjectCollaborator.status == "pending",
               *project_lifecycle.active_conditions()).order_by(ProjectCollaborator.id))).all()
    series = (await db.execute(select(SeriesCollaborator, Series, User.username)
        .join(Series, Series.id == SeriesCollaborator.series_id).join(User, User.id == Series.owner_id)
        .where(SeriesCollaborator.user_id == actor_id, SeriesCollaborator.status == "pending")
        .order_by(SeriesCollaborator.id))).all()
    return [InvitationOut(scope="project", resource_id=p.id, title=p.title, owner=name, role=m.role,
                          allow_owner_ai=m.allow_owner_ai, invitation_id=m.invitation_id, revision=m.revision)
            for m, p, name in projects] + [
        InvitationOut(scope="series", resource_id=s.id, title=s.name, owner=name, role=m.role,
                      invitation_id=m.invitation_id, revision=m.revision) for m, s, name in series
    ]


async def respond(
    db: AsyncSession, factory: async_sessionmaker[AsyncSession], actor: User, scope: Scope, invitation_id: str,
    expected_revision: int, *, accept: bool, notify: ProjectChangeNotifier | None = None,
) -> MemberOut:
    model = _model(scope)

    async def operation(state: dict[str, Any]) -> MemberOut:
        state.update(invitation_id=invitation_id, target_user_id=actor.id, target_username=actor.username)
        await authz.require_authenticated(db, actor.id)
        # 不暴露他人的邀请；初次查询也置于审计边界内，未知资源使用 0。
        initial = await db.scalar(select(model).where(model.invitation_id == invitation_id, model.user_id == actor.id))
        if initial is None:
            raise NotFoundError("邀请不存在")
        resource_id = getattr(initial, f"{scope}_id")
        state["resource_id"] = resource_id
        parent = await _lock_parent(db, actor.id, scope, resource_id)
        state["resource_label"] = parent.title if isinstance(parent, Project) else parent.name
        member = await db.scalar(select(model).where(
            _parent_field(scope) == resource_id, model.user_id == actor.id,
        ).execution_options(populate_existing=True))
        if member is None:
            raise NotFoundError("邀请不存在")
        if member.invitation_id != invitation_id:
            raise NotFoundError("邀请不存在")
        if member.revision != expected_revision or member.status != "pending":
            raise _stale()
        state["summary"] = {"before_role": member.role, "before_status": member.status}
        if isinstance(member, ProjectCollaborator):
            state["summary"]["before_allow_owner_ai"] = member.allow_owner_ai
        member.status = "accepted" if accept else "declined"
        member.responded_at = datetime.now(UTC)
        member.revision += 1
        state["summary"].update(after_role=member.role, after_status=member.status)
        if isinstance(member, ProjectCollaborator):
            state["summary"]["after_allow_owner_ai"] = member.allow_owner_ai
        await db.flush()
        await db.refresh(member)
        return _out(member, actor.username)

    return await _write(db, factory, actor, scope, 0, "accept" if accept else "decline", operation, notify=notify)
