"""笔记 API：列表不返回长正文，写入必须声明所基于的修订号。"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NoteTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NoteContent = Annotated[str, StringConstraints(max_length=200_000)]
Revision = Annotated[int, Field(ge=1, strict=True)]


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NoteTitle
    content: NoteContent = ""


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Revision
    title: NoteTitle | None = None
    content: NoteContent | None = None

    @field_validator("title", "content", mode="before")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("笔记标题和正文不能为 null；清空正文请传空字符串")
        return value


class NoteFlags(BaseModel):
    """收藏／固定是幂等设置：不带修订号，不改变 revision 与 updated_at。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    is_favorite: bool | None = None
    is_pinned: bool | None = None

    @field_validator("is_favorite", "is_pinned", mode="before")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("收藏与固定标记必须为布尔值，不能为 null")
        return value

    @model_validator(mode="after")
    def require_one(self):
        if not self.model_fields_set:
            raise ValueError("至少提供 is_favorite 或 is_pinned 之一")
        return self


class NoteSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    excerpt: str
    revision: int
    is_favorite: bool
    is_pinned: bool
    created_at: datetime
    updated_at: datetime


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    content: str
    revision: int
    is_favorite: bool
    is_pinned: bool
    created_at: datetime
    updated_at: datetime


class NotePage(BaseModel):
    items: list[NoteSummary]
    total: int
    page: int
    page_size: int
