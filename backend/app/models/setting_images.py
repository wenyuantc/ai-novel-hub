"""设定标准图与当前绑定；资源按作品／系列生命周期保留。"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SettingImageResource(Base):
    __tablename__ = "setting_image_resources"
    __table_args__ = (
        CheckConstraint("(project_id IS NULL) <> (series_id IS NULL)", name="one_scope"),
        CheckConstraint("width > 0 AND height > 0 AND byte_size > 0", name="positive_size"),
        UniqueConstraint("project_id", "id"),
        UniqueConstraint("series_id", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    mime_type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SettingThumbnailBinding(Base):
    __tablename__ = "setting_thumbnail_bindings"
    __table_args__ = (
        CheckConstraint(
            "(project_id IS NOT NULL AND series_id IS NULL AND series_entry_id IS NULL AND "
            "((CASE WHEN character_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN world_setting_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN organization_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN career_id IS NOT NULL THEN 1 ELSE 0 END)) = 1) OR "
            "(project_id IS NULL AND series_id IS NOT NULL AND series_entry_id IS NOT NULL AND "
            "character_id IS NULL AND world_setting_id IS NULL AND organization_id IS NULL AND career_id IS NULL)",
            name="one_subject",
        ),
        ForeignKeyConstraint(
            ["project_id", "character_id"], ["characters.project_id", "characters.id"],
            name="fk_thumbnail_character", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "world_setting_id"], ["world_settings.project_id", "world_settings.id"],
            name="fk_thumbnail_world", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "organization_id"], ["organizations.project_id", "organizations.id"],
            name="fk_thumbnail_organization", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "career_id"], ["careers.project_id", "careers.id"],
            name="fk_thumbnail_career", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["series_id", "series_entry_id"], ["series_entries.series_id", "series_entries.id"],
            name="fk_thumbnail_series_entry", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "image_resource_id"], ["setting_image_resources.project_id", "setting_image_resources.id"],
            name="fk_thumbnail_project_resource", deferrable=True, initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["series_id", "image_resource_id"], ["setting_image_resources.series_id", "setting_image_resources.id"],
            name="fk_thumbnail_series_resource", deferrable=True, initially="DEFERRED",
        ),
        UniqueConstraint("character_id"),
        UniqueConstraint("world_setting_id"),
        UniqueConstraint("organization_id"),
        UniqueConstraint("career_id"),
        UniqueConstraint("series_entry_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    image_resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    character_id: Mapped[int | None]
    world_setting_id: Mapped[int | None]
    organization_id: Mapped[int | None]
    career_id: Mapped[int | None]
    series_entry_id: Mapped[int | None]
