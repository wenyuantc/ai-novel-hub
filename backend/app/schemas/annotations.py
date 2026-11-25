"""章内协作审稿的公开契约。"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.book_search import CheckedText

MAX_MESSAGE_LENGTH = 4000
MAX_EXCERPT_LENGTH = 240


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_LENGTH)]
    operation_id: UUID

    @field_validator("body")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value.encode("utf-8")
        if "\x00" in value:
            raise ValueError("留言不能包含空字符")
        return value


class ThreadCreate(MessageCreate):
    scene_id: int = Field(ge=1, le=2_147_483_647)
    expected_content_revision: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    selection_hash: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    selected_text: CheckedText | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def check_selection_evidence(self):
        if (self.selection_hash is None) == (self.selected_text is None):
            raise ValueError("请提供原句指纹或选区原文其中一项")
        return self


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    author_name: str
    body: str
    created_at: datetime


class ThreadOut(BaseModel):
    id: int
    scene_id: int
    chapter_id: int
    content_revision: int
    start: int | None
    end: int | None
    excerpt: str
    selection_hash: str
    anchor_valid: bool
    resolved: bool
    created_at: datetime
    first_message: MessageOut


class ThreadPage(BaseModel):
    items: list[ThreadOut]
    total: int


class MessagePage(BaseModel):
    items: list[MessageOut]
    total: int
