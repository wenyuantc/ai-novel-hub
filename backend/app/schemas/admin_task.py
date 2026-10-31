"""管理员任务监控的安全白名单；绝不复用所有者 TaskOut。"""

import unicodedata
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.task import TaskKind, TaskStatus

AdminTaskSort = Literal["created_desc", "queue_longest", "running_longest"]
AdminTaskError = Literal["execution_failed", "cancelled", "interrupted", "cleanup_pending"]


def validate_cancel_reason(value: str) -> str:
    value = value.strip()
    if not 1 <= len(value) <= 200 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("取消原因须为1–200字纯文本，不能包含控制字符")
    return value


class AdminTaskFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str | None = Field(default=None, max_length=200)
    owner_user_id: int | None = Field(default=None, gt=0)
    project_id: int | None = Field(default=None, gt=0)
    task_id: int | None = Field(default=None, gt=0)
    kind: TaskKind | None = None
    status: list[TaskStatus] = Field(default_factory=list)
    from_at: AwareDatetime | None = None
    to_at: AwareDatetime | None = None
    sort: AdminTaskSort = "created_desc"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dates(self):
        if (self.from_at is None) != (self.to_at is None):
            raise ValueError("开始和结束时间必须成对提供")
        if self.from_at is not None:
            if self.from_at >= self.to_at:
                raise ValueError("开始时间必须早于结束时间")
            self.from_at = self.from_at.astimezone(UTC)
            self.to_at = self.to_at.astimezone(UTC)
        return self


class AdminTaskBrief(BaseModel):
    id: int
    task_key: str | None
    kind: TaskKind | Literal["unknown"]
    title: str
    status: TaskStatus | Literal["unknown"]
    progress: int
    owner_user_id: int
    owner_username: str
    project_id: int | None
    project_title: str | None
    project_lifecycle: Literal["active", "deleting", "recycled", "none"]
    chapter_id: int | None
    stage: str
    error_code: AdminTaskError | None
    error_message: str | None
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    queued_seconds: int | None
    running_seconds: int | None
    cancellation_pending: bool
    cancel_requested_at: datetime | None
    can_cancel: bool


class AdminTaskDetail(AdminTaskBrief):
    as_of: datetime


class AdminTaskCounts(BaseModel):
    queued: int = 0
    running: int = 0
    paused: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    interrupted: int = 0
    unknown: int = 0


class AdminTaskList(BaseModel):
    items: list[AdminTaskBrief]
    total: int
    counts: AdminTaskCounts
    as_of: datetime


class AdminTaskCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]
    reason: str

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("必须明确确认取消")
        return value

    @field_validator("reason")
    @classmethod
    def plain_reason(cls, value: str) -> str:
        return validate_cancel_reason(value)


class AdminTaskCancelOut(BaseModel):
    operation_id: str
    audit_log_id: int | None
