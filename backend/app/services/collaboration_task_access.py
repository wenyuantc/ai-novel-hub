"""本人任务的作品授权：活动协作任务绑定不可复用的邀请轮次。

这里不创建任务，也不读取模型配置。受理方只能把锁内重读得到的授权写入保留键；
请求参数不得充当此键。读取/订阅使用 SQL 过滤，确保隐藏记录不会占据分页 limit。
"""

from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, cast, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, ProjectCollaborator, Task, User
from app.models.entities import FINISHED_TASK_STATUSES, OPEN_TASK_STATUSES
from app.services import project_lifecycle
from app.services.authorization_service import AuthenticatedActor

GRANT_KEY = "_collaboration_grant"
GrantIdentity = tuple[str, int]


def task_grant_identity(task_input: Any) -> GrantIdentity | None:
    """只接受规范 UUID 与正整数；bool、字符串数字及破损历史值均不能授权。"""
    grant = task_input.get(GRANT_KEY) if isinstance(task_input, dict) else None
    if not isinstance(grant, dict):
        return None
    invitation, revision = grant.get("invitation_id"), grant.get("revision")
    if not isinstance(invitation, str) or type(revision) is not int or revision <= 0:
        return None
    try:
        if str(UUID(invitation)) != invitation:
            return None
    except ValueError:
        return None
    return invitation, revision


def task_requires_owner_ai(task_input: Any) -> bool:
    """任务内服务端绑定是否依赖作者模型授权；破损值保守地视为依赖。

    只用于协作任务的收紧方向检查：作者来源的文本/图片模型或作者向量模型都要求当前
    `allow_owner_ai` 仍为真。显式 null 不代表任何绑定，由实际发送前的绑定重验拒绝。
    """
    if not isinstance(task_input, dict):
        return True
    model = task_input.get("_project_model")
    if model is not None and (not isinstance(model, dict) or model.get("source") != "actor"):
        return True
    embedding = task_input.get("_shared_embedding")
    return embedding is not None and (not isinstance(embedding, dict) or embedding.get("mode") != "keyword")


async def owner_ai_allowed(db: AsyncSession, project_id: int, actor_id: int) -> bool:
    """调用方已持作品父行锁时读取当前成员的作者模型授权；不存在或非 accepted 一律为否。"""
    with db.no_autoflush:
        value = await db.scalar(select(ProjectCollaborator.allow_owner_ai).where(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == actor_id,
            ProjectCollaborator.status == "accepted",
        ).execution_options(populate_existing=True))
    return value is True


def bind_task_grant(task_input: dict[str, Any], grant_identity: GrantIdentity | None) -> dict[str, Any]:
    """覆盖保留键；grant_identity 必须来自服务端锁后重读，不能取自请求。"""
    result = dict(task_input)
    result.pop(GRANT_KEY, None)
    if grant_identity is not None:
        candidate = {"invitation_id": grant_identity[0], "revision": grant_identity[1]}
        if task_grant_identity({GRANT_KEY: candidate}) != grant_identity:
            raise ValueError("无效的协作授权轮次")
        result[GRANT_KEY] = candidate
    return result


def _matching_grant(db: AsyncSession):
    grant = Task.input[GRANT_KEY]
    revision = grant["revision"]
    invitation = grant["invitation_id"]
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        revision_is_integer = func.json_type(Task.input, '$."_collaboration_grant"."revision"') == "integer"
        invitation_is_string = func.json_type(Task.input, '$."_collaboration_grant"."invitation_id"') == "text"
        member_id = ProjectCollaborator.invitation_id
        canonical_invitation = and_(
            func.length(member_id) == 36, func.length(func.replace(member_id, "-", "")) == 32,
            *(func.substr(member_id, position, 1) == "-" for position in (9, 14, 19, 24)),
            ~member_id.bool_op("GLOB")("*[^0-9a-f-]*"),
        )
    elif dialect == "postgresql":
        revision_is_integer = func.json_typeof(revision) == "number"
        invitation_is_string = func.json_typeof(invitation) == "string"
        canonical_invitation = ProjectCollaborator.invitation_id.bool_op("~")(
            "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    else:
        # 未验证的数据库不能意外放行任务轮次。
        return false()
    return and_(
        revision_is_integer,
        invitation_is_string,
        canonical_invitation,
        invitation.as_string() == ProjectCollaborator.invitation_id,
        cast(revision.as_string(), String) == cast(ProjectCollaborator.revision, String),
    )


def task_visible_to_actor(
    db: AsyncSession, actor_id: int, *, active_grant_required: bool = False, token_version: int | None = None,
):
    """SQL 谓词，同时限定真实任务归属、项目生命周期和协作状态。

    `active_grant_required` 用于从活动状态开始的流：即使任务中途结束，原邀请轮次失效也不能续流。
    token_version 用于跨请求会话的流复验；普通 HTTP 已由 CurrentUser 验证。
    """
    original = db.info.get("authenticated_actor")
    if isinstance(original, AuthenticatedActor):
        if original.id != actor_id or (token_version is not None and token_version != original.token_version):
            return false()
        token_version = original.token_version
    member_is_current = and_(
        ProjectCollaborator.project_id == Project.id,
        ProjectCollaborator.user_id == actor_id,
        ProjectCollaborator.status == "accepted",
    )
    current_editor = and_(ProjectCollaborator.role == "editor", _matching_grant(db))
    member_rule = current_editor if active_grant_required else or_(
        Task.status.in_(FINISHED_TASK_STATUSES),
        and_(Task.status.in_(OPEN_TASK_STATUSES), current_editor),
    )
    membership = exists(select(ProjectCollaborator.id).where(member_is_current, member_rule).correlate(Project, Task))
    project_rule = exists(select(Project.id).where(
        Project.id == Task.project_id, *project_lifecycle.active_conditions(),
        or_(Project.user_id == actor_id, membership),
    ).correlate(Task))
    user_rule = [User.id == actor_id, User.is_active.is_(True)]
    if token_version is not None:
        user_rule.append(User.token_version == token_version)
    return and_(Task.user_id == actor_id, Task.statistics_key.is_not(None),
                or_(Task.project_id.is_(None), project_rule),
                exists(select(User.id).where(*user_rule)))
