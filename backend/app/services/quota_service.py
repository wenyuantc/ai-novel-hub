"""用户配额的唯一解析/计数/写入边界，作品准入持owner父行直到原事务结束。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError
from app.models import Project, User, UserQuota
from app.schemas.audit import QuotaAuditChange, RbacAuditChange
from app.schemas.rbac import InheritedQuota, LimitedQuota, QuotaItem, UnlimitedQuota, UserQuotaOut, UserQuotaUpdate
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation


def config(row: UserQuota | None, metric: str):
    prefix = "project" if metric == "project_count" else "task"
    mode = getattr(row, f"{prefix}_mode") if row is not None else "inherit"
    value = getattr(row, f"{prefix}_value") if row is not None else None
    if mode == "inherit" and value is None:
        return InheritedQuota(mode="inherit")
    if mode == "unlimited" and value is None:
        return UnlimitedQuota(mode="unlimited")
    if mode == "limited" and type(value) is int and 0 <= value <= 2_147_483_647:
        return LimitedQuota(mode="limited", value=value)
    raise authz.AuthorizationStorageError("用户配额状态无效，请联系管理员")


def resolve(row: UserQuota | None, metric: str, settings: Settings) -> tuple[int | None, str]:
    selected = config(row, metric)
    if selected.mode == "limited":
        return selected.value, "user"
    if selected.mode == "unlimited":
        return None, "user"
    return (None, "default") if metric == "project_count" else (settings.task_max_per_user, "system")


async def project_count(db: AsyncSession, user_id: int) -> int:
    return int(await db.scalar(select(func.count(Project.id)).where(Project.user_id == user_id)) or 0)


async def check_project_creation(db: AsyncSession, user_id: int) -> None:
    # 无可编辑的全局作品默认值，User屏障已与所有显式额度更新串行，不需反向获取全站gate。
    await authz.lock_users(db, user_id)
    row = await db.get(UserQuota, user_id, populate_existing=True)
    selected = config(row, "project_count")
    if selected.mode == "limited":
        used = await project_count(db, user_id)
        if used >= selected.value:
            raise ConflictError("作品数量已达到该账号上限", extra={
                "code": "quota_exceeded", "metric": "project_count", "used": used, "limit": selected.value,
            })


async def task_limits(db: AsyncSession, user_ids, settings: Settings) -> dict[int, int | None]:
    ids = set(user_ids)
    rows = {row.user_id: row for row in await db.scalars(select(UserQuota).where(UserQuota.user_id.in_(ids)))}
    return {user_id: resolve(rows.get(user_id), "task_concurrency", settings)[0] for user_id in ids}


async def get_quotas(db: AsyncSession, user_id: int, settings: Settings, engine) -> UserQuotaOut:
    counts = select(func.count(Project.id)).where(Project.user_id == User.id).correlate(User).scalar_subquery()
    rows = (await db.execute(
        authz.snapshot_statement(include_token_version=False).outerjoin(UserQuota, UserQuota.user_id == User.id)
        .add_columns(UserQuota, counts.label("project_used")).where(User.id == user_id)
    )).all()
    if not rows:
        from app.core.errors import NotFoundError
        raise NotFoundError("用户不存在")
    target = authz.snapshots_from_rows(rows)[user_id]
    row = rows[0]._mapping[UserQuota]
    project_limit, project_source = resolve(row, "project_count", settings)
    task_limit, task_source = resolve(row, "task_concurrency", settings)
    return UserQuotaOut(
        user_id=user_id, username=target.username, is_management_account=target.is_management_account,
        revision=row.revision if row is not None else 0, authorization_revision=target.revision,
        global_task_limit=settings.task_max_concurrency,
        project_count=QuotaItem(
            config=config(row, "project_count"), effective_limit=project_limit,
            source=project_source, used=int(rows[0].project_used),
        ),
        task_concurrency=QuotaItem(
            config=config(row, "task_concurrency"), effective_limit=task_limit,
            source=task_source, used=engine.admitted_count(user_id),
        ),
    )


async def update_quotas(
    db: AsyncSession, actor: User, user_id: int, request: UserQuotaUpdate, settings: Settings, engine,
) -> UserQuotaOut:
    current = await authz.require(db, actor, "quotas.update", write=True, targets=(user_id,))
    async with AuditOperation(db, actor, "user.quotas.update", "user", user_id) as audit:
        await authz.check_revision(db, request.authorization_revision)
        target = await authz.protect_target(db, current, user_id)
        audit.values["target_label"] = target.username
        row = await db.get(UserQuota, user_id, populate_existing=True)
        if request.revision != (row.revision if row is not None else 0):
            raise ConflictError("配额已被修改，请刷新后重新确认", extra={"code": "stale_revision"})
        before = {metric: config(row, metric) for metric in request.changes.model_fields_set}
        if row is None:
            row = UserQuota(user_id=user_id, project_mode="inherit", task_mode="inherit", revision=1)
            db.add(row)
        else:
            row.revision += 1
        changes = []
        for metric in request.changes.model_fields_set:
            value = getattr(request.changes, metric)
            prefix = "project" if metric == "project_count" else "task"
            setattr(row, f"{prefix}_mode", value.mode)
            setattr(row, f"{prefix}_value", value.value if value.mode == "limited" else None)
            changes.append(QuotaAuditChange(metric=metric, before=before[metric], after=value))
        audit.summary.rbac_change = RbacAuditChange(quotas=changes)
        await audit.commit()
        engine.notify()
        return await get_quotas(db, user_id, settings, engine)
