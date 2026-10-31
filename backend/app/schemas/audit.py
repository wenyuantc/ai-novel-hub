"""管理员审计的唯一传输契约；摘要拒绝未知键及自由文本值。"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

AuditAction = Literal[
    "user.create",
    "user.update",
    "user.reset_password",
    "user.delete",
    "project.update",
    "project.delete",
    "project.restore",
    "project.purge",
    "project.expire",
    "chapter.create",
    "chapter.create_from_outlines",
    "chapter.update",
    "chapter.delete",
    "chapter.cast.update",
    "chapter_version.update",
    "chapter_version.delete",
    "chapter_version.restore",
    "workshop.publish",
    "workshop.approve",
    "workshop.reject",
    "workshop.update",
    "workshop.hide",
    "workshop.unhide",
    "workshop.delete",
    "skill.create",
    "skill.import",
    "skill.update",
    "skill.references.replace",
    "skill.delete",
    "skill.restore_builtin",
    "audit.cleanup",
]
AuditTargetType = Literal[
    "user", "project", "chapter", "chapter_version", "workshop_item", "workshop_submission", "skill", "audit"
]
AuditResult = Literal["success", "failure"]
AuditErrorCode = Literal[
    "bad_request",
    "forbidden",
    "not_found",
    "conflict",
    "system_error",
    "invalid_preview",
    "preview_actor_mismatch",
    "preview_replayed",
]
AuditField = Literal[
    "username",
    "is_admin",
    "is_active",
    "title",
    "genre",
    "premise",
    "synopsis",
    "theme",
    "tags",
    "selling_points",
    "audience",
    "target_words",
    "status",
    "style",
    "default_style_id",
    "auto_analyze",
    "chapter_no",
    "outline_id",
    "plan_id",
    "style_id",
    "content",
    "starred",
    "note",
    "cast",
    "slug",
    "name",
    "description",
    "category",
    "triggers",
    "body",
    "references",
    "builtin_key",
    "hidden_by",
]
SafeState = Literal[
    "draft",
    "planning",
    "writing",
    "completed",
    "archived",
    "generating",
    "done",
    "active",
    "hidden",
    "admin",
    "author",
    "pending",
    "approved",
    "rejected",
    "cancelled",
]


class AuditValueChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    field: Literal[
        "is_admin",
        "is_active",
        "target_words",
        "status",
        "chapter_no",
        "outline_id",
        "plan_id",
        "style_id",
        "starred",
        "hidden_by",
        "auto_analyze",
        "default_style_id",
    ]
    before: bool | int | SafeState | None
    after: bool | int | SafeState | None


class AuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lifecycle_action: Literal["soft_delete", "restore", "purge", "expire"] | None = None
    retention_days: Literal[30] | None = None
    cancelled_task_count: int | None = Field(default=None, ge=0)
    changed_fields: list[AuditField] = Field(default_factory=list, max_length=50)
    changes: list[AuditValueChange] = Field(default_factory=list, max_length=20)
    word_count_before: int | None = Field(default=None, ge=0)
    word_count_after: int | None = Field(default=None, ge=0)
    body_chars_before: int | None = Field(default=None, ge=0)
    body_chars_after: int | None = Field(default=None, ge=0)
    reference_count_before: int | None = Field(default=None, ge=0)
    reference_count_after: int | None = Field(default=None, ge=0)
    affected_count: int | None = Field(default=None, ge=0)
    affected_ids: list[int] = Field(default_factory=list, max_length=100)
    ids_truncated: bool = False
    chapter_count: int | None = Field(default=None, ge=0)
    version_count: int | None = Field(default=None, ge=0)
    from_at: AwareDatetime | None = None
    to_at: AwareDatetime | None = None
    expected_count: int | None = Field(default=None, ge=0)
    deleted_count: int | None = Field(default=None, ge=0)
    high_water_id: int | None = Field(default=None, ge=0)


class AuditVersionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(gt=0)
    chapter_id: int = Field(gt=0)
    version_id: int = Field(gt=0)
    seq: int = Field(gt=0)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AuditLogBrief(BaseModel):
    id: int
    operation_id: str
    occurred_at: datetime
    actor_user_id: int | None
    actor_username: str
    action: AuditAction
    result: AuditResult
    target_type: AuditTargetType
    target_id: int | None
    target_label: str | None
    owner_user_id: int | None
    owner_username: str | None
    project_id: int | None
    project_title: str | None
    chapter_id: int | None
    error_code: AuditErrorCode | None
    error_message: str | None

    @field_validator("occurred_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AuditLogDetail(AuditLogBrief):
    summary: AuditSummary
    version_ref: AuditVersionRef | None
    version_status: Literal["available", "changed", "missing"] | None


class AuditLogList(BaseModel):
    items: list[AuditLogBrief]
    total: int


class AuditDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_at: AwareDatetime
    to_at: AwareDatetime

    @model_validator(mode="after")
    def validate_range(self):
        if self.from_at >= self.to_at:
            raise ValueError("开始时间必须早于结束时间")
        self.from_at = self.from_at.astimezone(UTC)
        self.to_at = self.to_at.astimezone(UTC)
        return self


class AuditCleanupPreview(AuditDateRange):
    count: int
    protected_count: int
    expires_at: datetime
    preview_token: str


class AuditCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_token: str = Field(min_length=1, max_length=4096)
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("必须明确确认清理")
        return value


class AuditCleanupOut(BaseModel):
    deleted_count: int
    audit_log_id: int


class AuditLogFilters(BaseModel):
    actor_user_id: int | None = Field(default=None, gt=0)
    actor_q: str | None = Field(default=None, max_length=64)
    target_type: AuditTargetType | None = None
    target_id: int | None = Field(default=None, gt=0)
    action: AuditAction | None = None
    result: AuditResult | None = None
    from_at: AwareDatetime | None = None
    to_at: AwareDatetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dates(self):
        if (self.from_at is None) != (self.to_at is None):
            raise ValueError("开始和结束时间必须成对提供")
        if self.from_at is not None:
            dates = AuditDateRange(from_at=self.from_at, to_at=self.to_at)
            self.from_at, self.to_at = dates.from_at, dates.to_at
        return self
