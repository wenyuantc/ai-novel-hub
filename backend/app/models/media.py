"""库外媒体的存储位置、配额、对象元数据与共享卷诊断。"""

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
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MediaStorageLocation(Base):
    __tablename__ = "media_storage_locations"
    __table_args__ = (
        CheckConstraint("kind IN ('local','s3')", name="kind_valid"),
        CheckConstraint("state IN ('active','readonly','retired')", name="state_valid"),
        Index(
            "uq_media_storage_locations_active",
            "state",
            unique=True,
            sqlite_where=text("state = 'active'"),
            postgresql_where=text("state = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    secrets_enc: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class MediaStorageSettings(Base):
    __tablename__ = "media_storage_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("revision >= 1", name="positive_revision"),
        CheckConstraint("default_quota_bytes >= 0", name="nonnegative_quota"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    default_quota_bytes: Mapped[int] = mapped_column(BigInteger, default=5 * 1024**3)
    last_operation_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MediaObject(Base):
    __tablename__ = "media_objects"
    __table_args__ = (
        CheckConstraint("kind IN ('image','video','audio')", name="kind_valid"),
        CheckConstraint("scope IN ('user','project')", name="scope_valid"),
        CheckConstraint("status IN ('pending','ready','deleting')", name="status_valid"),
        CheckConstraint("size_bytes >= 0", name="nonnegative_size"),
        UniqueConstraint("parent_id", "variant"),
        UniqueConstraint("location_id", "storage_key"),
        Index("ix_media_objects_owner_status", "owner_user_id", "status"),
        Index("ix_media_objects_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("media_storage_locations.id", ondelete="RESTRICT"))
    volume_id: Mapped[str | None] = mapped_column(String(36))
    source: Mapped[str] = mapped_column(String(64), default="upload")
    storage_key: Mapped[str] = mapped_column(String(255))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    scope: Mapped[str] = mapped_column(String(8))
    plugin_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(8))
    mime: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("media_objects.id", ondelete="RESTRICT"))
    variant: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_token: Mapped[str | None] = mapped_column(String(36))
    cleanup_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaVolumeReport(Base):
    __tablename__ = "media_volume_reports"

    worker_key: Mapped[str] = mapped_column(String(36), primary_key=True)
    volume_id: Mapped[str] = mapped_column(String(36))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@event.listens_for(MediaStorageSettings.__table__, "after_create")
def _seed_settings(table, connection, **kwargs):
    connection.execute(table.insert().values(id=1))


@event.listens_for(MediaStorageLocation.__table__, "after_create")
def _seed_local(table, connection, **kwargs):
    connection.execute(table.insert().values(kind="local", state="active"))
