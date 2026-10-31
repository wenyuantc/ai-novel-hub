"""实例级统计：只保留计数、任务类型与时间，不持有业务级联外键。"""

from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DashboardTracking(Base):
    __tablename__ = "admin_dashboard_tracking"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai", nullable=False)


class DashboardDailySnapshot(Base):
    __tablename__ = "admin_dashboard_daily"
    __table_args__ = tuple(
        CheckConstraint(f"{column} >= 0", name=f"{column}_nonnegative")
        for column in ("users", "projects", "chapters", "total_words", "pending_submissions")
    )

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    users: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projects: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chapters: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_words: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pending_submissions: Mapped[int] = mapped_column(BigInteger, nullable=False)


class TaskStatistic(Base):
    __tablename__ = "admin_task_statistics"
    __table_args__ = (
        CheckConstraint("origin IN ('baseline', 'new')", name="origin"),
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled', 'interrupted')",
            name="status",
        ),
        Index("ix_admin_task_statistics_created_event_at", "created_event_at"),
        Index("ix_admin_task_statistics_status_finished_event_at", "status", "finished_event_at"),
    )

    key: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now(), nullable=False
    )
    created_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    finished_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
