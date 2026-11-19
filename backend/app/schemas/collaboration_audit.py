"""协作审计查询的严格筛选参数与安全响应契约。"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.audit import AuditDateRange
from app.schemas.collaboration import Role, Scope

MemberStatus = Literal["pending", "accepted", "declined", "revoked"]
CollaborationAuditAction = Literal["invite", "accept", "decline", "modify", "revoke"]
CollaborationAuditResult = Literal["success", "failure"]
CollaborationAuditErrorCode = Literal[
    "bad_request",
    "unauthorized",
    "conflict",
    "not_found",
    "permission_denied",
    "invitee_unavailable",
    "stale_invitation",
    "cancelled",
    "already_collaborator",
    "self_invite",
    "collaboration_busy",
    "system_error",
]
DbId = Annotated[int, Field(gt=0, le=2_147_483_647)]
NonNegativeDbId = Annotated[int, Field(ge=0, le=2_147_483_647)]


def _normalize_query(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


class CollaborationAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before_role: Role | None = None
    after_role: Role | None = None
    before_status: MemberStatus | None = None
    after_status: MemberStatus | None = None
    before_allow_owner_ai: bool | None = None
    after_allow_owner_ai: bool | None = None


class CollaborationAuditEventOut(BaseModel):
    id: int
    operation_id: str
    occurred_at: datetime
    scope: Scope
    resource_id: int
    resource_label: str | None
    invitation_id: str | None
    actor_user_id: int | None
    actor_username: str | None
    target_user_id: int | None
    target_username: str | None
    action: CollaborationAuditAction
    result: CollaborationAuditResult
    error_code: CollaborationAuditErrorCode | None
    summary: CollaborationAuditSummary

    @field_validator("occurred_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CollaborationAuditList(BaseModel):
    items: list[CollaborationAuditEventOut]
    total: int


class _CollaborationAuditDateFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: CollaborationAuditAction | None = None
    result: CollaborationAuditResult | None = None
    from_at: AwareDatetime | None = None
    to_at: AwareDatetime | None = None
    page: int = Field(default=1, ge=1, le=10_000_000)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dates(self):
        if (self.from_at is None) != (self.to_at is None):
            raise ValueError("开始和结束时间必须成对提供")
        if self.from_at is not None:
            dates = AuditDateRange(from_at=self.from_at, to_at=self.to_at)
            self.from_at, self.to_at = dates.from_at, dates.to_at
        return self


class AuthorCollaborationAuditFilters(_CollaborationAuditDateFilters):
    member_user_id: DbId | None = None
    member_q: str | None = Field(default=None, max_length=64)

    @field_validator("member_q")
    @classmethod
    def normalize_member_q(cls, value: str | None) -> str | None:
        return _normalize_query(value)


class AdminCollaborationAuditFilters(_CollaborationAuditDateFilters):
    scope: Scope | None = None
    resource_id: NonNegativeDbId | None = None
    resource_q: str | None = Field(default=None, max_length=200)
    actor_user_id: DbId | None = None
    actor_q: str | None = Field(default=None, max_length=64)
    target_user_id: DbId | None = None
    target_q: str | None = Field(default=None, max_length=64)

    @field_validator("resource_q", "actor_q", "target_q")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return _normalize_query(value)
