"""个人通知只保存受限类别与来源身份，打开时重新读取权限。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

KIND_CHECK = "kind IN ('invitation','task','budget','consistency')"


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (CheckConstraint(KIND_CHECK, name="kind"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(KIND_CHECK, name="kind"),
        UniqueConstraint("recipient_id", "event_key"),
        Index("ix_notifications_recipient_time_id", "recipient_id", "created_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, default=lambda: str(uuid4()))
    recipient_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    event_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    variant: Mapped[str | None] = mapped_column(String(16))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    grant_id: Mapped[str | None] = mapped_column(String(36))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_key: Mapped[str | None] = mapped_column(String(36))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("consistency_runs.id", ondelete="CASCADE"))
    budget_id: Mapped[int | None] = mapped_column(ForeignKey("ai_usage_budgets.id", ondelete="CASCADE"))
    budget_key: Mapped[str | None] = mapped_column(String(36))
    currency: Mapped[str | None] = mapped_column(String(3))
    # 关闭期间保留去重事实，防止重新开启后重复处理事件时补发。
    suppressed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
