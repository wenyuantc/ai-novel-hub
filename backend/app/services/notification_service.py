"""个人收件。投递与来源同事务，读、计数和打开共用数据库权限条件。"""

from datetime import UTC, datetime
from typing import get_args
from uuid import uuid4

from sqlalchemy import and_, exists, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.models import (
    AiUsageBudget,
    ConsistencyRun,
    Notification,
    NotificationPreference,
    Project,
    ProjectCollaborator,
    SeriesCollaborator,
    Task,
    User,
)
from app.schemas.notifications import (
    BudgetTarget,
    ConsistencyTarget,
    InvitationTarget,
    Kind,
    NotificationItem,
    NotificationPage,
    TaskTarget,
)
from app.services import authorization_service, project_lifecycle
from app.services.collaboration_task_access import task_visible_to_actor


def _insert(db):
    return pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert


def _source_visible(db, row, source, recipient_id, kind: Kind, *, reading: bool):
    """source 是收件表或投递候选行；所有 EXISTS 显式关联同一行。"""

    def matches(model, *conditions):
        return select(model.id).where(*conditions).correlate(source).exists()

    member = ProjectCollaborator
    if kind == "invitation":
        project = matches(Project, Project.id == row.project_id, *project_lifecycle.active_conditions())
        pending = matches(
            member,
            member.project_id == row.project_id,
            member.user_id == recipient_id,
            member.status == "pending",
            member.invitation_id == row.grant_id,
        )
        series = matches(
            SeriesCollaborator,
            SeriesCollaborator.series_id == row.series_id,
            SeriesCollaborator.user_id == recipient_id,
            SeriesCollaborator.status == "pending",
            SeriesCollaborator.invitation_id == row.grant_id,
        )
        return or_(and_(project, pending), series)

    owner = matches(
        Project, Project.id == row.project_id, Project.user_id == recipient_id, *project_lifecycle.active_conditions()
    )
    if kind == "budget":
        return matches(
            AiUsageBudget,
            AiUsageBudget.id == row.budget_id,
            AiUsageBudget.instance_key == row.budget_key,
            AiUsageBudget.currency == row.currency,
            or_(
                and_(row.project_id.is_(None), AiUsageBudget.user_id == recipient_id),
                and_(AiUsageBudget.project_id == row.project_id, owner),
            ),
        )

    project = matches(Project, Project.id == row.project_id, *project_lifecycle.active_conditions())
    accepted = matches(
        member,
        member.project_id == row.project_id,
        member.user_id == recipient_id,
        member.status == "accepted",
        member.invitation_id == row.grant_id,
    )
    access = or_(and_(owner, row.grant_id.is_(None)), and_(project, accepted))
    if kind == "consistency":
        return and_(
            access,
            matches(
                ConsistencyRun,
                ConsistencyRun.id == row.run_id,
                ConsistencyRun.project_id == row.project_id,
                ConsistencyRun.status == "succeeded",
            ),
        )
    task_rules = [
        Task.id == row.task_id,
        Task.statistics_key == row.task_key,
        Task.user_id == recipient_id,
        Task.project_id.is_not_distinct_from(row.project_id),
        Task.status == row.variant,
    ]
    if reading:
        task_rules.append(task_visible_to_actor(db, recipient_id))
    return and_(or_(row.project_id.is_(None), access), matches(Task, *task_rules))


async def deliver(
    db: AsyncSession,
    *,
    recipient_id: int,
    kind: Kind,
    event_key: str,
    variant: str | None = None,
    project_id: int | None = None,
    series_id: int | None = None,
    grant_id: str | None = None,
    task_id: int | None = None,
    task_key: str | None = None,
    run_id: int | None = None,
    budget_id: int | None = None,
    budget_key: str | None = None,
    currency: str | None = None,
) -> None:
    """限当前可投递来源，用一条 INSERT SELECT 读取偏好、验证来源并按事件去重。"""
    values = dict(
        recipient_id=recipient_id,
        kind=kind,
        event_key=event_key,
        variant=variant,
        project_id=project_id,
        series_id=series_id,
        grant_id=grant_id,
        task_id=task_id,
        task_key=task_key,
        run_id=run_id,
        budget_id=budget_id,
        budget_key=budget_key,
        currency=currency,
    )
    table = Notification.__table__
    # PostgreSQL 对 CTE 内无类型的 NULL 默认推断为 text；显式使用每个目的列的类型。
    candidate = select(
        *(
            literal(value, type_=table.c[name].type).cast(table.c[name].type).label(name)
            for name, value in values.items()
        )
    ).subquery()
    visible = _source_visible(db, candidate.c, candidate, recipient_id, kind, reading=False)
    suppressed = exists().where(
        NotificationPreference.user_id == recipient_id,
        NotificationPreference.kind == kind,
        NotificationPreference.enabled.is_(False),
    )
    created_at = literal(datetime.now(UTC), type_=table.c.created_at.type)
    query = select(*candidate.c, suppressed, literal(str(uuid4())), created_at).where(
        visible,
        exists().where(User.id == recipient_id, User.is_active.is_(True)),
    )
    statement = (
        _insert(db)(table)
        .from_select([*values, "suppressed", "public_id", "created_at"], query)
        .on_conflict_do_nothing(
            index_elements=["recipient_id", "event_key"],
        )
    )
    await db.execute(statement)


def _visible(db, user_id):
    n = Notification
    return and_(
        n.recipient_id == user_id,
        n.suppressed.is_(False),
        or_(*(and_(n.kind == kind, _source_visible(db, n, n, user_id, kind, reading=True)) for kind in get_args(Kind))),
    )


def consistency_run_visible(db: AsyncSession, recipient_id: int, notification_id: str, project_id: int, run_id: int):
    """通知深链读取概要时，按原收件 UUID 与该运行在同一查询中复验。"""
    return select(Notification.id).where(
        _visible(db, recipient_id), Notification.public_id == str(notification_id),
        Notification.kind == "consistency", Notification.project_id == project_id, Notification.run_id == run_id,
    ).correlate(None).exists()


def _item(row):
    if row.kind == "invitation":
        title = "收到作品协作邀请" if row.project_id is not None else "收到系列协作邀请"
    elif row.kind == "task":
        title = "后台任务已完成" if row.variant == "succeeded" else "后台任务失败"
    elif row.kind == "budget":
        scope = "账号" if row.project_id is None else "作品"
        level = "已达额度" if row.variant == "exceeded" else "已达提醒比例"
        title = f"{scope} {row.currency} 预算{level}"
    else:
        title = "一致性检查已完成"
    return NotificationItem(
        id=row.public_id, kind=row.kind, title=title, created_at=row.created_at, read_at=row.read_at
    )


def _cursor(row):
    stamp = project_lifecycle.as_utc(row.created_at).isoformat(timespec="microseconds")
    return f"{stamp}~{row.id}"


def _parse_cursor(value: str):
    try:
        stamp, item_id = value.split("~")
        moment = datetime.fromisoformat(stamp)
        key = int(item_id)
        if moment.tzinfo != UTC or not 0 < key <= 2_147_483_647:
            raise ValueError
        return moment, key
    except ValueError:
        raise BadRequestError("通知分页位置无效") from None


async def list_items(db: AsyncSession, user_id: int, limit: int, before: str | None):
    n = Notification
    stmt = select(n).where(_visible(db, user_id))
    if before is not None:
        stamp, key = _parse_cursor(before)
        stmt = stmt.where(tuple_(n.created_at, n.id) < tuple_(stamp, key))
    rows = (await db.scalars(stmt.order_by(n.created_at.desc(), n.id.desc()).limit(limit + 1))).all()
    return NotificationPage(
        items=[_item(row) for row in rows[:limit]], next_before=_cursor(rows[limit - 1]) if len(rows) > limit else None
    )


async def unread_count(db: AsyncSession, user_id: int):
    return await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            _visible(db, user_id),
            Notification.read_at.is_(None),
        )
    )


async def mark_read(db: AsyncSession, user_id: int, notification_id: str, *, open_target: bool):
    await authorization_service.require_authenticated(db, user_id, write=True)
    row = (
        await db.scalars(
            update(Notification)
            .where(
                Notification.public_id == notification_id,
                _visible(db, user_id),
            )
            .values(read_at=func.coalesce(Notification.read_at, func.now()))
            .returning(Notification)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("通知已失效或不存在")
    target = None
    if open_target:
        if row.kind == "invitation":
            target = InvitationTarget(
                scope="project" if row.project_id is not None else "series", invitation_id=row.grant_id
            )
        elif row.kind == "task":
            target = TaskTarget(task_id=row.task_id, task_key=row.task_key)
        elif row.kind == "budget":
            target = BudgetTarget(project_id=row.project_id, currency=row.currency, budget_key=row.budget_key)
        else:
            target = ConsistencyTarget(project_id=row.project_id, run_id=row.run_id)
    await db.commit()
    return target


async def preferences(db: AsyncSession, user_id: int):
    result = dict.fromkeys(get_args(Kind), True)
    rows = await db.execute(
        select(NotificationPreference.kind, NotificationPreference.enabled).where(
            NotificationPreference.user_id == user_id
        )
    )
    result.update(rows.all())
    return result


async def set_preference(db: AsyncSession, user_id: int, kind: Kind, enabled: bool):
    await authorization_service.require_authenticated(db, user_id, write=True)
    stmt = _insert(db)(NotificationPreference).values(user_id=user_id, kind=kind, enabled=enabled)
    await db.execute(stmt.on_conflict_do_update(index_elements=["user_id", "kind"], set_={"enabled": enabled}))
    result = await preferences(db, user_id)
    await db.commit()
    return result
