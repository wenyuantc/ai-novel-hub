"""协作审计：成功与业务同事务，失败独立提交并核实未知提交结果。"""

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CollaborationAuditEvent

_ACTIONS = frozenset({"invite", "accept", "decline", "modify", "revoke"})
_ROLES = frozenset({"viewer", "editor"})
_STATUSES = frozenset({"pending", "accepted", "declined", "revoked"})
_ERRORS = frozenset({
    "bad_request", "unauthorized", "conflict", "not_found", "permission_denied", "invitee_unavailable",
    "stale_invitation", "cancelled", "already_collaborator", "self_invite", "collaboration_busy", "system_error",
})
_SUMMARY_KEYS = frozenset({
    "before_role", "after_role", "before_status", "after_status", "before_allow_owner_ai", "after_allow_owner_ai",
})


@dataclass(frozen=True)
class AuditSnapshot:
    """调用方在业务事务内保存不包含正文、提示词或密钥的不可变身份快照。"""

    operation_id: str
    scope: Literal["project", "series"]
    resource_id: int
    action: str
    resource_label: str | None = None
    invitation_id: str | None = None
    actor_user_id: int | None = None
    actor_username: str | None = None
    target_user_id: int | None = None
    target_username: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if str(UUID(self.operation_id)) != self.operation_id:
            raise ValueError("操作身份必须为规范 UUID")
        if self.invitation_id is not None and str(UUID(self.invitation_id)) != self.invitation_id:
            raise ValueError("邀请身份必须为规范 UUID")
        if self.scope not in ("project", "series") or self.action not in _ACTIONS:
            raise ValueError("未知协作审计作用域或动作")
        # 仅失败事件可使用 0，表示对不存在/非本人的邀请无法确定资源身份。
        if type(self.resource_id) is not int or self.resource_id < 0:
            raise ValueError("审计资源身份不合法")
        for value in (self.actor_user_id, self.target_user_id):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("审计身份必须为正整数")
        for value, limit in ((self.resource_label, 200), (self.actor_username, 64), (self.target_username, 64)):
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                raise ValueError("审计快照名称不合法")
        if not isinstance(self.summary, dict) or self.summary.keys() - _SUMMARY_KEYS:
            raise ValueError("审计摘要包含不允许的字段")
        for key, value in self.summary.items():
            if key.endswith("_role") and value is not None and value not in _ROLES:
                raise ValueError("审计角色不合法")
            if key.endswith("_status") and value is not None and value not in _STATUSES:
                raise ValueError("审计状态不合法")
            if key.endswith("_allow_owner_ai") and value is not None and type(value) is not bool:
                raise ValueError("审计模型授权不合法")
        # 不让外部持有的 dict 在校验后被修改，绕过白名单。
        object.__setattr__(self, "summary", dict(self.summary))


def _event(snapshot: AuditSnapshot, result: Literal["success", "failure"], code: str | None) -> CollaborationAuditEvent:
    # 再次验证，阻止调用方原地修改经过校验的摘要。
    snapshot = AuditSnapshot(**vars(snapshot))
    summary = dict(snapshot.summary)
    if result == "failure":
        summary = {key: value for key, value in summary.items() if key.startswith("before_")}
    return CollaborationAuditEvent(
        operation_id=snapshot.operation_id,
        scope=snapshot.scope,
        resource_id=snapshot.resource_id,
        resource_label=snapshot.resource_label,
        invitation_id=snapshot.invitation_id,
        actor_user_id=snapshot.actor_user_id,
        actor_username=snapshot.actor_username,
        target_user_id=snapshot.target_user_id,
        target_username=snapshot.target_username,
        action=snapshot.action,
        result=result,
        error_code=code,
        summary=summary,
    )


async def add_success(db: AsyncSession, snapshot: AuditSnapshot) -> CollaborationAuditEvent:
    """调用方负责同一事务提交业务和审计；审计失败即业务整体回滚。"""
    if snapshot.resource_id == 0:
        raise ValueError("成功审计必须有真实资源身份")
    event = _event(snapshot, "success", None)
    db.add(event)
    await db.flush()
    return event


async def get_result(sessionmaker: async_sessionmaker[AsyncSession], operation_id: str) -> str | None:
    async with sessionmaker() as db:
        return await db.scalar(select(CollaborationAuditEvent.result).where(
            CollaborationAuditEvent.operation_id == operation_id
        ))


async def record_failure(
    sessionmaker: async_sessionmaker[AsyncSession], snapshot: AuditSnapshot, error_code: str,
) -> str:
    """在业务回滚后独立写失败；返回已存在或本次写入的真实结果。

    必须先检查 operation_id；若唯一约束竞争或提交确认丢失，再用新会话查询。
    查询不可用或找不到已提交事件时传播异常，不能伪造成功或失败。
    """
    existing = await get_result(sessionmaker, snapshot.operation_id)
    if existing is not None:
        return existing
    code = error_code if error_code in _ERRORS else "system_error"
    try:
        async with sessionmaker() as db:
            db.add(_event(snapshot, "failure", code))
            await db.commit()
        return "failure"
    except Exception:
        existing = await get_result(sessionmaker, snapshot.operation_id)
        if existing is not None:
            return existing
        raise
