"""管理员作品回收站的唯一传输契约。"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.schemas.project import ProjectStatus


class RecycleProjectOut(BaseModel):
    id: int
    title: str
    genre: str
    status: ProjectStatus
    owner_user_id: int
    owner_username: str
    deleted_by_user_id: int | None
    deleted_by_username: str | None
    deleted_at: datetime
    expires_at: datetime
    restorable: bool
    chapter_count: int
    total_words: int

    @field_validator("deleted_at", "expires_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RecycleProjectList(BaseModel):
    items: list[RecycleProjectOut]
    total: int


class RecycleRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted_at: AwareDatetime
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("必须明确确认操作")
        return value


class RecyclePurgeRequest(RecycleRestoreRequest):
    title: str = Field(min_length=1, max_length=200)
