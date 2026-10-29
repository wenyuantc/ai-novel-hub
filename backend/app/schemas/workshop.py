"""实例内工坊的提交、浏览与审核契约。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

WorkshopKind = Literal["style", "prompt"]
WorkshopCategory = Literal["general", "xuanhuan", "wuxia", "romance", "scifi", "suspense", "history", "urban", "other"]
SubmissionStatus = Literal["pending", "approved", "rejected", "cancelled"]


def clean_tags(tags: list[str]) -> list[str]:
    result = []
    for tag in tags:
        tag = tag.strip()
        if not tag or len(tag) > 12 or any(c in tag for c in ('"', "\\", "\n", "\r")):
            raise ValueError("标签须为 1–12 字，不能包含引号、反斜线或换行")
        if tag not in result:
            result.append(tag)
    return result


class WorkshopMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: WorkshopCategory = "general"
    tags: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str]) -> list[str]:
        return clean_tags(tags)


class WorkshopSubmit(WorkshopMetadata):
    kind: WorkshopKind
    item_id: int | None = Field(default=None, gt=0)
    prompt_key: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    content: dict[str, str] = Field(max_length=12)
    is_anonymous: bool = False

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("名称不能为空")
        return name


class WorkshopApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: WorkshopCategory | None = None
    tags: list[str] | None = Field(default=None, max_length=5)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, tags: list[str] | None) -> list[str] | None:
        return clean_tags(tags) if tags is not None else None


class WorkshopReject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def nonblank_note(cls, note: str) -> str:
        note = note.strip()
        if not note:
            raise ValueError("请填写驳回原因")
        return note


class WorkshopItemOut(BaseModel):
    id: int
    kind: WorkshopKind
    prompt_key: str | None
    name: str
    description: str
    category: WorkshopCategory
    tags: list[str]
    content: dict[str, str]
    author_name: str | None
    author_username: str | None = None
    is_mine: bool
    is_anonymous: bool
    is_official: bool
    status: Literal["active", "hidden"]
    hidden_by: Literal["author", "admin"] | None
    like_count: int
    download_count: int
    liked: bool
    imported: bool
    published_at: datetime
    created_at: datetime
    updated_at: datetime


class WorkshopItemList(BaseModel):
    items: list[WorkshopItemOut]
    total: int


class WorkshopSubmissionOut(BaseModel):
    id: int
    item_id: int | None
    kind: WorkshopKind
    prompt_key: str | None
    name: str
    description: str
    category: WorkshopCategory
    tags: list[str]
    content: dict[str, str]
    is_anonymous: bool
    author_name: str | None
    author_username: str | None = None
    status: SubmissionStatus
    review_note: str
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkshopSubmissionList(BaseModel):
    items: list[WorkshopSubmissionOut]
    total: int


class WorkshopImportOut(BaseModel):
    style_id: int | None = None
    prompt_key: str | None = None
    download_count: int
    imported: bool = True
