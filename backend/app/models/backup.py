"""实例级整库备份；不引用用户/作品，历史身份与文件位置使用安全快照。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BackupSettings(Base):
    __tablename__ = "backup_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("revision >= 1", name="positive_revision"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    instance_key: Mapped[str] = mapped_column(String(36), nullable=False)
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False)
    secrets_enc: Mapped[dict] = mapped_column(JSON, nullable=False)
    next_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BackupRun(Base):
    __tablename__ = "backup_runs"
    __table_args__ = (
        CheckConstraint("source IN ('manual','scheduled')", name="source_valid"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','partial','failed','interrupted')", name="status_valid"
        ),
        CheckConstraint(
            "(status IN ('queued','running') AND active_slot IS NOT NULL AND active_slot = 1) OR "
            "(status NOT IN ('queued','running') AND active_slot IS NULL)",
            name="active_status",
        ),
        CheckConstraint("settings_revision >= 1", name="positive_settings_revision"),
        CheckConstraint(
            "notification_status IN ('not_configured','pending','sending','sent','failed','unknown')",
            name="notification_status_valid",
        ),
        CheckConstraint(
            "cleanup_status IN ('not_required','pending','succeeded','failed','unknown')",
            name="cleanup_status_valid",
        ),
        Index("ix_backup_runs_created_at_id", "created_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_key: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    instance_key: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    scheduled_date: Mapped[str | None] = mapped_column(String(10), unique=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    actor_username: Mapped[str | None] = mapped_column(String(64))
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    database_engine: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_revision: Mapped[str | None] = mapped_column(String(64))
    encrypted: Mapped[bool] = mapped_column(nullable=False)
    file_format: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    active_slot: Mapped[int | None] = mapped_column(Integer, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    notification_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_configured")
    notification_error: Mapped[str | None] = mapped_column(Text)
    cleanup_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_required")
    cleanup_error: Mapped[str | None] = mapped_column(Text)


class BackupArtifact(Base):
    __tablename__ = "backup_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "destination"),
        CheckConstraint("destination IN ('local','s3')", name="destination_valid"),
        CheckConstraint(
            "status IN ('pending','ready','failed','unknown','deleting','deleted','missing')",
            name="status_valid",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_nonnegative"),
        Index("ix_backup_artifacts_destination_status", "destination", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backup_runs.id", ondelete="RESTRICT"), nullable=False)
    destination: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(160))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    locator: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    access_snapshot_enc: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version_id: Mapped[str | None] = mapped_column(String(1024))
    multipart_upload_id: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
