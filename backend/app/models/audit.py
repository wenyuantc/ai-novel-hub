"""实例级审计快照：故意不引用任何业务外键。"""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    __table_args__ = (
        Index("ix_admin_audit_time", "occurred_at", "id"),
        Index("ix_admin_audit_actor_time", "actor_user_id", "occurred_at", "id"),
        Index("ix_admin_audit_target_time", "target_type", "target_id", "occurred_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    actor_user_id: Mapped[int] = mapped_column(Integer)
    actor_username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(8))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[int | None] = mapped_column(Integer)
    target_label: Mapped[str | None] = mapped_column(String(200))
    owner_user_id: Mapped[int | None] = mapped_column(Integer)
    owner_username: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[int | None] = mapped_column(Integer)
    project_title: Mapped[str | None] = mapped_column(String(200))
    chapter_id: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[dict] = mapped_column(JSON)
    version_ref: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(200))
