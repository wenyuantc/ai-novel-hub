"""可跨执行进程续等的厂商任务台账。"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AiExternalJob(Base):
    __tablename__ = "ai_external_jobs"
    __table_args__ = (
        CheckConstraint("kind = 'video'", name="kind"),
        CheckConstraint(
            "state IN ('created','submitting','submitted','succeeded','failed','cancelled','expired')",
            name="state",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress"),
        Index("ix_ai_external_jobs_task_id", "task_id"),
        Index("ix_ai_external_jobs_project_id", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    payer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    config_id: Mapped[int] = mapped_column()
    config_instance_key: Mapped[str] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), server_default="video")
    request: Mapped[dict] = mapped_column(JSON)
    external_id: Mapped[str | None] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(16), server_default="created")
    progress: Mapped[int | None] = mapped_column()
    result_object_id: Mapped[str | None] = mapped_column(ForeignKey("media_objects.id", ondelete="SET NULL"))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    usage_call_id: Mapped[str | None] = mapped_column(ForeignKey("ai_usage_calls.id", ondelete="SET NULL"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_ref: Mapped[str | None] = mapped_column(String(255))
