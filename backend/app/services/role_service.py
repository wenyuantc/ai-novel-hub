"""仅超管可维护的角色目录和账号角色整体替换。"""

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.permissions import PERMISSIONS
from app.models import AuthorizationState, Role, User, UserRole
from app.schemas.audit import RbacAuditChange
from app.schemas.rbac import PermissionList, PermissionOut, RoleCreate, RoleList, RoleOut, RoleUpdate, UserRolesUpdate
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation


def permission_catalog() -> PermissionList:
    return PermissionList(items=[PermissionOut(
        code=p.code, label=p.label, group=p.group, dangerous=p.dangerous, requires=list(p.requires),
    ) for p in PERMISSIONS])


async def _get(db: AsyncSession, role_id: int) -> Role:
    role = await db.get(Role, role_id, populate_existing=True)
    if role is None:
        raise NotFoundError("角色不存在")
    return role


def _mutable(role: Role) -> None:
    if role.system_key is not None:
        raise ConflictError("内置角色定义不可修改或删除，请复制为自定义角色", extra={"code": "builtin_role_readonly"})


def _version(role: Role, revision: int) -> None:
    if role.revision != revision:
        raise ConflictError("角色已被修改，请刷新后重新确认", extra={"code": "stale_revision"})


async def member_count(db: AsyncSession, role: Role) -> int:
    if role.system_key == "super_admin":
        stmt = select(func.count()).select_from(User).where(User.is_admin.is_(True))
    elif role.system_key == "user":
        stmt = select(func.count()).select_from(User).where(
            User.is_admin.is_(False), ~select(UserRole.user_id).where(UserRole.user_id == User.id).exists(),
        )
    else:
        stmt = select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id)
    return int(await db.scalar(stmt) or 0)


def _role_query():
    assignments = select(func.count()).select_from(UserRole).where(UserRole.role_id == Role.id).correlate(Role)
    supers = select(func.count()).select_from(User).where(User.is_admin.is_(True)).scalar_subquery()
    ordinary = select(func.count()).select_from(User).where(
        User.is_admin.is_(False), ~select(UserRole.user_id).where(UserRole.user_id == User.id).exists(),
    ).scalar_subquery()
    members = case(
        (Role.system_key == "super_admin", supers), (Role.system_key == "user", ordinary),
        else_=assignments.scalar_subquery(),
    ).label("member_count")
    return select(Role, AuthorizationState.revision.label("authorization_revision"), members).join(
        AuthorizationState, AuthorizationState.id == 1,
    ).execution_options(populate_existing=True)


def _output(row) -> RoleOut:
    role = row.Role
    return RoleOut(
        id=role.id, name=role.name, system_key=role.system_key, is_builtin=role.system_key is not None,
        description=role.description, permissions=role.permissions, revision=role.revision,
        member_count=row.member_count, created_at=role.created_at, updated_at=role.updated_at,
        authorization_revision=row.authorization_revision,
    )


async def role_out(db: AsyncSession, role: Role) -> RoleOut:
    row = (await db.execute(_role_query().where(Role.id == role.id))).one_or_none()
    if row is None:
        raise NotFoundError("角色不存在")
    return _output(row)


async def list_roles(db: AsyncSession, *, q: str | None, page: int, page_size: int) -> RoleList:
    stmt = _role_query()
    count = select(func.count()).select_from(Role)
    if q:
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        predicate = Role.name.ilike(f"%{escaped}%", escape="\\")
        stmt, count = stmt.where(predicate), count.where(predicate)
    total = int(await db.scalar(count) or 0)
    rows = (await db.execute(stmt.order_by(Role.id).offset((page - 1) * page_size).limit(page_size))).all()
    # 空页的revision仅是目录游标，不与某个旧Role实体拼接。
    revision = rows[0].authorization_revision if rows else int(await db.scalar(
        select(AuthorizationState.revision).where(AuthorizationState.id == 1)
    ))
    return RoleList(items=[_output(row) for row in rows], total=total, authorization_revision=revision)


async def get_role(db: AsyncSession, role_id: int) -> RoleOut:
    row = (await db.execute(_role_query().where(Role.id == role_id))).one_or_none()
    if row is None:
        raise NotFoundError("角色不存在")
    return _output(row)


async def _flush_unique(db: AsyncSession) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ConflictError("角色名称已存在") from exc


async def create_role(db: AsyncSession, actor: User, data: RoleCreate) -> RoleOut:
    await authz.require(db, actor, write=True)
    async with AuditOperation(db, actor, "role.create", "role") as audit:
        source = await _get(db, data.source_role_id) if data.source_role_id is not None else None
        if source is not None:
            _version(source, data.source_revision)
        permissions = data.permissions if data.permissions is not None else source.permissions if source else []
        role = Role(name=data.name, description=data.description, permissions=list(permissions))
        db.add(role)
        await _flush_unique(db)
        audit.target(role)
        audit.summary.rbac_change = RbacAuditChange(
            source_role_id=source.id if source else None, permissions_after=list(permissions),
        )
        await authz.advance(db)
        await audit.commit(flush_fn=_flush_unique)
        await db.refresh(role)
        return await role_out(db, role)


async def update_role(db: AsyncSession, actor: User, role_id: int, data: RoleUpdate) -> RoleOut:
    await authz.require(db, actor, write=True)
    async with AuditOperation(db, actor, "role.update", "role", role_id) as audit:
        await authz.check_revision(db, data.authorization_revision)
        role = await _get(db, role_id)
        audit.target(role)
        _mutable(role)
        _version(role, data.revision)
        before = list(role.permissions)
        for field in data.model_fields_set - {"revision", "authorization_revision"}:
            setattr(role, field, getattr(data, field))
        role.revision += 1
        audit.summary.rbac_change = RbacAuditChange(permissions_before=before, permissions_after=role.permissions)
        await authz.advance(db)
        await audit.commit(flush_fn=_flush_unique)
        await db.refresh(role)
        return await role_out(db, role)


async def delete_role(
    db: AsyncSession, actor: User, role_id: int, *, revision: int, authorization_revision: int,
) -> None:
    await authz.require(db, actor, write=True)
    async with AuditOperation(db, actor, "role.delete", "role", role_id) as audit:
        await authz.check_revision(db, authorization_revision)
        role = await _get(db, role_id)
        audit.target(role)
        _mutable(role)
        _version(role, revision)
        if await member_count(db, role):
            raise ConflictError("该角色仍分配给账号，请先撤回全部分配", extra={"code": "role_in_use"})
        audit.summary.rbac_change = RbacAuditChange(permissions_before=role.permissions)
        await db.delete(role)
        await authz.advance(db)
        await audit.commit()


async def replace_user_roles(db: AsyncSession, actor: User, user_id: int, data: UserRolesUpdate):
    from app.services.admin_service import _ensure_other_active_admin, _user_out

    await authz.require(db, actor, write=True, targets=(user_id,))
    async with AuditOperation(db, actor, "user.roles.update", "user", user_id) as audit:
        await authz.check_revision(db, data.authorization_revision)
        target = await db.get(User, user_id, populate_existing=True)
        if target is None:
            raise NotFoundError("用户不存在")
        audit.target(target)
        before = await authz.snapshot(db, user_id)
        roles = []
        for role_id in data.role_ids:
            roles.append(await _get(db, role_id))
        super_admin = any(role.system_key == "super_admin" for role in roles)
        if target.is_admin and not super_admin:
            if target.id == actor.id:
                raise BadRequestError("不能移除自己的超级管理员身份")
            if target.is_active:
                await _ensure_other_active_admin(db, target.id)
        target.is_admin = super_admin
        await db.execute(delete(UserRole).where(UserRole.user_id == user_id))
        for role in roles:
            if role.system_key not in ("super_admin", "user"):
                db.add(UserRole(user_id=user_id, role_id=role.id))
        audit.summary.rbac_change = RbacAuditChange(
            role_ids_before=[r.id for r in before.roles][:100], role_ids_after=data.role_ids[:100],
            ids_truncated=len(data.role_ids) > 100 or len(before.roles) > 100,
        )
        await authz.advance(db)
        await audit.commit()
        await db.refresh(target)
        return await _user_out(db, target)
