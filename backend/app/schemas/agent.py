from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.schemas.task import TaskOut


class PageContext(BaseModel):
    route: str = Field(default="", max_length=500)
    chapter_id: int | None = Field(default=None, gt=0)
    outline_id: int | None = Field(default=None, gt=0)
    character_id: int | None = Field(default=None, gt=0)
    selection: str | None = Field(default=None, max_length=2000)


class ConversationCreate(BaseModel):
    title: str = Field(default="", max_length=200)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    auto_execute: bool | None = None


class SendMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    page_context: PageContext | None = None
    ai_config_id: int | None = Field(default=None, gt=0)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    project_id: int
    title: str
    auto_execute: bool
    summary: str
    summarized_until: int | None
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversation_id: int
    turn_task_id: int | None
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None
    tool_name: str | None
    page_context: dict[str, Any] | None
    usage: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("tool_calls")
    def public_calls(self, calls: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        # Provider 的签名 / thinking blocks 只用于协议重放，不返回面板。
        return [{k: c[k] for k in ("id", "name", "arguments") if k in c} for c in calls] if calls else calls


class ChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    conversation_id: int
    project_id: int
    turn_task_id: int | None
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    kind: Literal["small", "delete", "task", "text"]
    preview: dict[str, Any]
    status: Literal["proposed", "executing", "executed", "rejected", "failed", "undone"]
    result: dict[str, Any] | None
    effects: list[dict[str, Any]]
    error: str
    executed_at: datetime | None
    undone_at: datetime | None
    seq: int
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
    changes: list[ChangeOut]
    active_task: TaskOut | None


class MessageAccepted(BaseModel):
    message: MessageOut
    task: TaskOut


class TurnChangesOut(BaseModel):
    changes: list[ChangeOut]
    task: TaskOut | None = None
    error: str | None = None
