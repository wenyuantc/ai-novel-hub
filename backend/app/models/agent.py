"""项目智能体会话、原生工具消息与可确认的写入记录。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AgentConversation(TimestampMixin, Base):
    __tablename__ = "agent_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    auto_execute: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    summarized_until: Mapped[int | None] = mapped_column(Integer)
    ai_history_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 本会话启用的 MCP 插件 id（NULL = 不启用）；运行时再按归属与启用状态过滤
    mcp_plugin_ids: Mapped[list[int] | None] = mapped_column(JSON)


class AgentMessage(TimestampMixin, Base):
    __tablename__ = "agent_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "turn_task_id", "tool_call_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True)
    turn_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    tool_call_id: Mapped[str | None] = mapped_column(String(200))
    tool_name: Mapped[str | None] = mapped_column(String(100))
    page_context: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 用户消息强制启用的 Skill 与其快照 {id, name, body}；本轮续跑 / 重试沿用快照
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id", ondelete="SET NULL"), index=True)
    skill_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AgentChange(TimestampMixin, Base):
    __tablename__ = "agent_changes"
    __table_args__ = (UniqueConstraint("conversation_id", "turn_task_id", "tool_call_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    turn_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(200))
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    kind: Mapped[str] = mapped_column(String(16))
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="proposed", server_default="proposed")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    effects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default="[]")
    error: Mapped[str] = mapped_column(Text, default="", server_default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
