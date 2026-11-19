"""协作审计只读查询：作者侧防越权/防枚举脱敏与管理员全站查询。"""

from datetime import datetime

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import CollaborationAuditEvent, Project, Series
from app.schemas.collaboration import Scope
from app.schemas.collaboration_audit import (
    AdminCollaborationAuditFilters,
    AuthorCollaborationAuditFilters,
    CollaborationAuditEventOut,
    CollaborationAuditList,
    CollaborationAuditSummary,
)
from app.services import authorization_service as authz
from app.services import project_lifecycle

_RESPONSE_ACTIONS = ("accept", "decline")
_SAFE_INVITE_FAILURE_CODES = ("self_invite", "already_collaborator")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _author_target_visible_expr():
    """避免作者借邀请失败记录（尤其 invitee_unavailable）区分已禁用账号与不存在账号。"""
    return or_(
        CollaborationAuditEvent.action != "invite",
        CollaborationAuditEvent.result != "failure",
        CollaborationAuditEvent.error_code.in_(_SAFE_INVITE_FAILURE_CODES),
    )


def _is_author_target_visible(row: CollaborationAuditEvent) -> bool:
    return (
        row.action != "invite"
        or row.result != "failure"
        or row.error_code in _SAFE_INVITE_FAILURE_CODES
    )


def _time_range_conditions(db: AsyncSession, from_at: datetime, to_at: datetime):
    """UTC 半开区间 [from_at, to_at)；兼容 SQLite server_default 秒精度与微秒格式。"""
    col = CollaborationAuditEvent.occurred_at
    if db.get_bind().dialect.name == "sqlite":
        from_cond = (
            or_(col >= from_at, cast(col, String) == from_at.strftime("%Y-%m-%d %H:%M:%S"))
            if from_at.microsecond == 0
            else col >= from_at
        )
        to_cond = (
            and_(col < to_at, cast(col, String) != to_at.strftime("%Y-%m-%d %H:%M:%S"))
            if to_at.microsecond == 0
            else col < to_at
        )
        return [from_cond, to_cond]
    return [col >= from_at, col < to_at]


def _order_by(db: AsyncSession):
    if db.get_bind().dialect.name == "sqlite":
        normalized = func.substr(cast(CollaborationAuditEvent.occurred_at, String).concat(".000000"), 1, 26)
        return (normalized.desc(), CollaborationAuditEvent.id.desc())
    return (CollaborationAuditEvent.occurred_at.desc(), CollaborationAuditEvent.id.desc())


def _project_event(
    row: CollaborationAuditEvent, *, redact_unconfirmed_invite_target: bool,
) -> CollaborationAuditEventOut:
    hide_target = redact_unconfirmed_invite_target and not _is_author_target_visible(row)
    return CollaborationAuditEventOut(
        id=row.id,
        operation_id=row.operation_id,
        occurred_at=row.occurred_at,
        scope=row.scope,
        resource_id=row.resource_id,
        resource_label=row.resource_label,
        invitation_id=row.invitation_id,
        actor_user_id=row.actor_user_id,
        actor_username=row.actor_username,
        target_user_id=None if hide_target else row.target_user_id,
        target_username=None if hide_target else row.target_username,
        action=row.action,
        result=row.result,
        error_code=row.error_code,
        summary=CollaborationAuditSummary.model_validate(row.summary or {}),
    )


async def _paginate(
    db: AsyncSession, conditions: list, *, page: int, page_size: int, redact_unconfirmed_invite_target: bool,
) -> CollaborationAuditList:
    total = int(await db.scalar(select(func.count(CollaborationAuditEvent.id)).where(*conditions)) or 0)
    rows = await db.scalars(
        select(CollaborationAuditEvent)
        .where(*conditions)
        .order_by(*_order_by(db))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return CollaborationAuditList(
        items=[_project_event(row, redact_unconfirmed_invite_target=redact_unconfirmed_invite_target) for row in rows],
        total=total,
    )


async def list_for_owner(
    db: AsyncSession, actor_id: int, scope: Scope, resource_id: int, filters: AuthorCollaborationAuditFilters,
) -> CollaborationAuditList:
    with db.no_autoflush:
        await authz.require_authenticated(db, actor_id)
        model = Project if scope == "project" else Series
        parent = await db.scalar(select(model).where(model.id == resource_id).execution_options(populate_existing=True))
    if parent is None or (parent.user_id if isinstance(parent, Project) else parent.owner_id) != actor_id:
        raise NotFoundError("作品或系列不存在")
    if isinstance(parent, Project):
        project_lifecycle.ensure_active(parent)

    conditions = [
        CollaborationAuditEvent.scope == scope,
        CollaborationAuditEvent.resource_id == resource_id,
        # 仅保留真实受邀人的响应与作者本人的管理操作；排除非作者越权探测尝试。
        or_(
            CollaborationAuditEvent.action.in_(_RESPONSE_ACTIONS),
            CollaborationAuditEvent.actor_user_id == actor_id,
        ),
    ]
    if filters.member_user_id is not None:
        conditions.append(
            or_(
                CollaborationAuditEvent.actor_user_id == filters.member_user_id,
                and_(_author_target_visible_expr(), CollaborationAuditEvent.target_user_id == filters.member_user_id),
            )
        )
    if filters.member_q:
        pattern = f"%{_escape_like(filters.member_q)}%"
        conditions.append(
            or_(
                CollaborationAuditEvent.actor_username.ilike(pattern, escape="\\"),
                and_(
                    _author_target_visible_expr(),
                    CollaborationAuditEvent.target_username.ilike(pattern, escape="\\"),
                ),
            )
        )
    if filters.action is not None:
        conditions.append(CollaborationAuditEvent.action == filters.action)
    if filters.result is not None:
        conditions.append(CollaborationAuditEvent.result == filters.result)
    if filters.from_at is not None and filters.to_at is not None:
        conditions.extend(_time_range_conditions(db, filters.from_at, filters.to_at))

    return await _paginate(
        db, conditions, page=filters.page, page_size=filters.page_size, redact_unconfirmed_invite_target=True,
    )


async def list_for_admin(db: AsyncSession, filters: AdminCollaborationAuditFilters) -> CollaborationAuditList:
    conditions = []
    for field in ("scope", "resource_id", "actor_user_id", "target_user_id", "action", "result"):
        value = getattr(filters, field)
        if value is not None:
            conditions.append(getattr(CollaborationAuditEvent, field) == value)
    if filters.resource_q:
        pattern = f"%{_escape_like(filters.resource_q)}%"
        conditions.append(CollaborationAuditEvent.resource_label.ilike(pattern, escape="\\"))
    if filters.actor_q:
        pattern = f"%{_escape_like(filters.actor_q)}%"
        conditions.append(CollaborationAuditEvent.actor_username.ilike(pattern, escape="\\"))
    if filters.target_q:
        pattern = f"%{_escape_like(filters.target_q)}%"
        conditions.append(CollaborationAuditEvent.target_username.ilike(pattern, escape="\\"))
    if filters.from_at is not None and filters.to_at is not None:
        conditions.extend(_time_range_conditions(db, filters.from_at, filters.to_at))

    return await _paginate(
        db, conditions, page=filters.page, page_size=filters.page_size, redact_unconfirmed_invite_target=False,
    )
