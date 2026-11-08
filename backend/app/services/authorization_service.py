"""操作权限事实、同快照投影和持到事务结束的管理写屏障。"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, event, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.core.authorization_runtime import AuthorizationRuntime
from app.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.permissions import BUILTIN_ROLES, PERMISSION_CODES, validate_permissions
from app.models import AuthorizationState, Role, User, UserRole
from app.schemas.rbac import RoleBrief


class AuthorizationStorageError(AppError):
    status_code = 500


@dataclass(frozen=True)
class AuthenticatedActor:
    id: int
    token_version: int


@dataclass(frozen=True)
class AuthorizationSnapshot:
    id: int
    username: str
    is_super_admin: bool
    is_active: bool
    token_version: int
    created_at: datetime
    roles: tuple[RoleBrief, ...]
    permissions: frozenset[str]
    revision: int

    @property
    def is_management_account(self) -> bool:
        return self.is_super_admin or bool(self.permissions)

    def allows(self, permission: str) -> bool:
        return permission in PERMISSION_CODES and (self.is_super_admin or permission in self.permissions)

    def public(self) -> dict:
        return {
            "is_super_admin": self.is_super_admin, "roles": list(self.roles),
            "effective_permissions": [code for code in PERMISSION_CODES if code in self.permissions],
            "authorization_revision": self.revision,
        }

    def target(self) -> dict:
        return {
            "is_super_admin": self.is_super_admin, "is_management_account": self.is_management_account,
            "roles": list(self.roles), "authorization_revision": self.revision,
        }


def runtime_for(db: AsyncSession) -> AuthorizationRuntime:
    runtime = db.info.get("authorization_runtime")
    if not isinstance(runtime, AuthorizationRuntime):
        raise RuntimeError("数据库会话未配置共享授权协调器")
    return runtime


def runtime_for_factory(factory: async_sessionmaker[AsyncSession]) -> AuthorizationRuntime:
    return factory.kw["info"]["authorization_runtime"]


@event.listens_for(Session, "after_transaction_end")
def _release_gate(session: Session, transaction) -> None:
    if transaction.parent is None:
        lease = session.info.pop("authorization_gate_lease", None)
        if lease is not None:
            lease.release()


async def initialize(factory: async_sessionmaker[AsyncSession]) -> None:
    """迁移与create_all测试使用相同目录；只在受控初始化调用。"""
    async with runtime_for_factory(factory).hold(), factory() as db:
        if await db.get(AuthorizationState, 1) is None:
            db.add(AuthorizationState(id=1, revision=0))
        found = {r.system_key: r for r in await db.scalars(select(Role).where(Role.system_key.is_not(None)))}
        for key, (name, description, permissions) in BUILTIN_ROLES.items():
            if key not in found:
                db.add(Role(system_key=key, name=name, description=description, permissions=list(permissions)))
        await db.commit()


async def gate(db: AsyncSession) -> None:
    if "authorization_gate_lease" in db.info:
        return
    lease = await runtime_for(db).acquire()
    db.info["authorization_gate_lease"] = lease
    try:
        with db.no_autoflush:
            result = await db.execute(
                update(AuthorizationState).where(AuthorizationState.id == 1)
                .values(id=AuthorizationState.id).execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            raise AuthorizationStorageError("授权状态不可用，请联系管理员")
    except BaseException:
        db.info.pop("authorization_gate_lease", None)
        lease.release()
        raise


async def lock_users(db: AsyncSession, *ids: int, missing_ok: bool = False) -> None:
    with db.no_autoflush:
        for user_id in sorted(set(ids)):
            result = await db.execute(
                update(User).where(User.id == user_id).values(id=User.id, updated_at=User.updated_at)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1 and not missing_ok:
                raise NotFoundError("用户不存在")


def snapshot_statement(*, include_token_version: bool = True):
    """可组合的安全身份/角色/全局代次单SELECT；调用方可加入同快照统计。"""
    return (
        select(
            User.id, User.username, User.is_admin, User.is_active,
            (User.token_version if include_token_version else literal(0)).label("verified_version"), User.created_at,
            AuthorizationState.revision.label("authorization_revision"), Role,
        )
        .select_from(User)
        .join(AuthorizationState, AuthorizationState.id == 1)
        .outerjoin(UserRole, UserRole.user_id == User.id)
        .outerjoin(Role, or_(
            Role.id == UserRole.role_id,
            and_(User.is_admin.is_(True), Role.system_key == "super_admin"),
            and_(User.is_admin.is_(False), UserRole.role_id.is_(None), Role.system_key == "user"),
        ))
        .execution_options(populate_existing=True)
    )


def snapshots_from_rows(rows) -> dict[int, AuthorizationSnapshot]:
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row.id, []).append(row)
    result = {}
    for user_id, group in grouped.items():
        first = group[0]
        roles: dict[int, RoleBrief] = {}
        permissions: set[str] = set()
        try:
            for row in group:
                role = row._mapping[Role]
                if role is None:
                    raise ValueError("角色元数据缺失")
                values = validate_permissions(role.permissions)
                if role.system_key is not None:
                    if role.system_key not in BUILTIN_ROLES or set(values) != set(BUILTIN_ROLES[role.system_key][2]):
                        raise ValueError("内置角色定义无效")
                    if role.system_key == "super_admin" and not first.is_admin:
                        raise ValueError("超级角色不允许有独立成员关系")
                    if role.system_key == "user" and first.is_admin:
                        continue
                roles[role.id] = RoleBrief(
                    id=role.id, name=role.name, system_key=role.system_key, is_builtin=role.system_key is not None,
                )
                if role.system_key not in ("super_admin", "user"):
                    permissions.update(values)
        except (TypeError, ValueError):
            raise AuthorizationStorageError("授权状态无效，请联系管理员") from None
        if first.is_admin:
            permissions = set(PERMISSION_CODES)
        result[user_id] = AuthorizationSnapshot(
            id=first.id, username=first.username, is_super_admin=first.is_admin, is_active=first.is_active,
            token_version=first.verified_version, created_at=first.created_at,
            roles=tuple(roles[key] for key in sorted(roles)), permissions=frozenset(permissions),
            revision=first.authorization_revision,
        )
    return result


async def snapshot(db: AsyncSession, user_id: int) -> AuthorizationSnapshot:
    with db.no_autoflush:
        rows = (await db.execute(snapshot_statement().where(User.id == user_id))).all()
    if not rows:
        if await db.scalar(select(User.id).where(User.id == user_id)) is None:
            raise NotFoundError("用户不存在")
        raise AuthorizationStorageError("授权状态不可用，请联系管理员")
    return snapshots_from_rows(rows)[user_id]


def actor_snapshot(db: AsyncSession, actor: User | int | AuthenticatedActor) -> AuthenticatedActor | int:
    if isinstance(actor, AuthenticatedActor):
        return actor
    actor_id = actor if isinstance(actor, int) else actor.id
    authenticated = db.info.get("authenticated_actor")
    if isinstance(authenticated, AuthenticatedActor) and authenticated.id == actor_id:
        return authenticated
    return actor_id if isinstance(actor, int) else AuthenticatedActor(actor.id, actor.token_version)


async def require_authenticated(
    db: AsyncSession, actor: User | int | AuthenticatedActor, *, write: bool = False, targets: tuple[int, ...] = (),
) -> AuthorizationSnapshot:
    """重验登录身份而不额外要求管理权限；写入时保持认证门禁与 User 锁。"""
    original = actor_snapshot(db, actor)
    actor_id = original if isinstance(original, int) else original.id
    if write:
        await gate(db)
        # 不存在的业务目标留给动作审计上下文报告404；所有存在的User仍严格升序锁定。
        await lock_users(db, actor_id, *targets, missing_ok=True)
    try:
        current = await snapshot(db, actor_id)
    except NotFoundError:
        raise UnauthorizedError("未登录或登录已过期") from None
    if not current.is_active or (
        isinstance(original, AuthenticatedActor) and original.token_version != current.token_version
    ):
        raise UnauthorizedError("未登录或登录已过期")
    return current


async def require_authenticated_for_send(db: AsyncSession, actor: User | int | AuthenticatedActor) -> None:
    """发送 lease 持有单用户读锁，防停用/撤销令牌穿透；不把全局授权门禁持到网络上。"""
    original = actor_snapshot(db, actor)
    actor_id = original if isinstance(original, int) else original.id
    if db.get_bind().dialect.name == "sqlite":
        await lock_users(db, actor_id, missing_ok=True)
    statement = select(User.is_active, User.token_version).where(User.id == actor_id)
    if db.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(read=True)
    row = (await db.execute(statement)).one_or_none()
    if row is None or not row.is_active or (
        isinstance(original, AuthenticatedActor) and original.token_version != row.token_version
    ):
        raise UnauthorizedError("未登录或登录已过期")


async def require(
    db: AsyncSession, actor: User | int | AuthenticatedActor, permission: str | None = None, *,
    write: bool = False, targets: tuple[int, ...] = (),
) -> AuthorizationSnapshot:
    current = await require_authenticated(db, actor, write=write, targets=targets)
    if not (current.is_super_admin if permission is None else current.allows(permission)):
        extra = {"code": "permission_denied"}
        if permission is not None:
            extra["required_permission"] = permission
        raise ForbiddenError("没有执行该操作的权限" if current.is_management_account else "需要管理员权限", extra=extra)
    return current


async def allows(db: AsyncSession, actor: User | int, permission: str) -> bool:
    current = await snapshot(db, actor if isinstance(actor, int) else actor.id)
    return current.is_active and current.allows(permission)


async def protect_target(db: AsyncSession, actor: AuthorizationSnapshot, user_id: int) -> AuthorizationSnapshot:
    target = await snapshot(db, user_id)
    if target.is_management_account and not actor.is_super_admin:
        raise ForbiddenError("管理账号仅可由超级管理员维护", extra={"code": "management_account_protected"})
    return target


async def check_revision(db: AsyncSession, expected: int) -> None:
    current = await db.scalar(select(AuthorizationState.revision).where(AuthorizationState.id == 1))
    if current != expected:
        raise ConflictError("权限状态已变化，请刷新后重新确认", extra={"code": "stale_revision"})


async def advance(db: AsyncSession) -> None:
    await db.execute(
        update(AuthorizationState).where(AuthorizationState.id == 1)
        .values(revision=AuthorizationState.revision + 1).execution_options(synchronize_session=False)
    )
