"""协作授权与不受业务删除级联影响的审计快照。"""

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
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ProjectCollaborator(TimestampMixin, Base):
    __tablename__ = "project_collaborators"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id"),
        CheckConstraint("role IN ('viewer', 'editor')", name="role"),
        CheckConstraint("status IN ('pending', 'accepted', 'declined', 'revoked')", name="status"),
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint("invited_by_user_id IS NULL OR invited_by_user_id <> user_id", name="inviter_not_target"),
        Index("ix_project_collaborators_user_status", "user_id", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    allow_owner_ai: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    invitation_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid4()), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    invited_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SeriesCollaborator(TimestampMixin, Base):
    __tablename__ = "series_collaborators"
    __table_args__ = (
        UniqueConstraint("series_id", "user_id"),
        CheckConstraint("role = 'editor'", name="role"),
        CheckConstraint("status IN ('pending', 'accepted', 'declined', 'revoked')", name="status"),
        CheckConstraint("revision > 0", name="revision"),
        CheckConstraint("invited_by_user_id IS NULL OR invited_by_user_id <> user_id", name="inviter_not_target"),
        Index("ix_series_collaborators_user_status", "user_id", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="editor", server_default="editor", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    invitation_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid4()), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    invited_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CollaborationAuditEvent(Base):
    """使用快照而非业务外键，删除用户、作品或系列后审计仍可核对。"""

    __tablename__ = "collaboration_audit_events"
    __table_args__ = (
        CheckConstraint("scope IN ('project', 'series')", name="scope"),
        CheckConstraint("result IN ('success', 'failure')", name="result"),
        Index("ix_collaboration_audit_scope_resource", "scope", "resource_id", "occurred_at", "id"),
        Index("ix_collaboration_audit_actor_time", "actor_user_id", "occurred_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    invitation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(8), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
