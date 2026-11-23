"""项目智能体会话、原生工具消息与可确认的写入记录。"""

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


def _uuid_check(column: str, *, version4: bool = False) -> str:
    """严格小写规范 UUID（SQLite 不依赖方言特有的正则函数）。"""
    hexadecimal = f"replace({column}, '-', '')"
    remainder = hexadecimal
    for char in "0123456789abcdef":
        remainder = f"replace({remainder}, '{char}', '')"
    version_check = (
        f"AND substr({column}, 15, 1) = '4' AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b') "
        if version4 else ""
    )
    return (
        f"CAST({column} AS TEXT) = {column} AND length({column}) = 36 "
        f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
        f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
        f"{version_check}"
        f"AND length({hexadecimal}) = 32 AND {remainder} = ''"
    )


class AgentConversation(TimestampMixin, Base):
    __tablename__ = "agent_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    auto_execute: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    summarized_until: Mapped[int | None] = mapped_column(Integer)
    # 摘要来源（0053）：NULL = 作者 / 升级前；协作者在受控路径生成的摘要记 "collaborator:<邀请 UUID>:<轮次>"，
    # 只在同一授权轮次的执行体内被承认（E4i-b 裁决 13）。服务端写入，不进入 API 输出。
    summary_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ai_history_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 创建会话时的协作邀请轮次（0054）：NULL = 作者会话 / 升级前；协作者经 HTTP 创建时记当前 accepted 成员的
    # invitation_id。撤权后重邀（新 invitation_id）时旧轮次会话只读（E4i-d 裁决 7）。服务端写入，不进入 API 输出。
    collaboration_invitation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 本会话启用的 MCP 插件 id（NULL = 不启用）；运行时再按归属与启用状态过滤
    mcp_plugin_ids: Mapped[list[int] | None] = mapped_column(JSON)


class AgentMessage(TimestampMixin, Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_task_id", "tool_call_id"),
        CheckConstraint(f"turn_task_key IS NULL OR ({_uuid_check('turn_task_key')})", name="turn_task_key_uuid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True)
    turn_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    # 历史为 NULL；保存创建时的任务统计 UUID，即使任务被删除也保留。
    turn_task_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
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
    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_task_id", "tool_call_id"),
        CheckConstraint(_uuid_check("change_key", version4=True), name="change_key_uuid4"),
        CheckConstraint(f"turn_task_key IS NULL OR ({_uuid_check('turn_task_key')})", name="turn_task_key_uuid"),
        Index("ux_agent_changes_change_key", "change_key", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Python 端逐次生成；空串默认仅服务于 SQLite 原位迁移，约束不允许它实际落库。
    change_key: Mapped[str] = mapped_column(String(36), nullable=False, default=lambda: str(uuid4()), server_default="")
    conversation_id: Mapped[int] = mapped_column(ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    turn_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    turn_task_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tool_call_id: Mapped[str] = mapped_column(String(200))
    tool_name: Mapped[str] = mapped_column(String(100))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    kind: Mapped[str] = mapped_column(String(16))
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="proposed", server_default="proposed")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    effects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, server_default="[]")
    error: Mapped[str] = mapped_column(Text, default="", server_default="")
    external_worker_key: Mapped[str | None] = mapped_column(String(36))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
