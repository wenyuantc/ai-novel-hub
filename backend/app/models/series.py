from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Series(TimestampMixin, Base):
    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        CheckConstraint("revision >= 0", name="revision"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(default=0, server_default="0")


class SeriesBook(TimestampMixin, Base):
    __tablename__ = "series_books"
    __table_args__ = (
        UniqueConstraint("project_id", "series_id"),
        ForeignKeyConstraint(
            ["project_id", "owner_id"], ["projects.id", "projects.user_id"],
            name="fk_series_books_project_owner", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["series_id", "owner_id"], ["series.id", "series.owner_id"],
            name="fk_series_books_series_owner", ondelete="RESTRICT",
        ),
    )

    project_id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(index=True)
    owner_id: Mapped[int]


class SeriesEntry(TimestampMixin, Base):
    __tablename__ = "series_entries"
    __table_args__ = (
        UniqueConstraint("series_id", "id", "kind"),
        CheckConstraint("kind IN ('character', 'world')", name="kind"),
        Index("ix_series_entries_series_id_kind_name", "series_id", "kind", "display_name"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    ai_settings: Mapped[dict[str, Any]] = mapped_column(JSON)


class SeriesEntryUse(TimestampMixin, Base):
    __tablename__ = "series_entry_uses"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'character' AND character_id IS NOT NULL AND world_setting_id IS NULL) OR "
            "(kind = 'world' AND character_id IS NULL AND world_setting_id IS NOT NULL)",
            name="local_entity_kind",
        ),
        UniqueConstraint("project_id", "character_id", name="uq_series_entry_uses_local_character"),
        UniqueConstraint("project_id", "world_setting_id", name="uq_series_entry_uses_local_world"),
        ForeignKeyConstraint(
            ["project_id", "series_id"], ["series_books.project_id", "series_books.series_id"],
            name="fk_series_entry_uses_book", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["series_id", "series_entry_id", "kind"],
            ["series_entries.series_id", "series_entries.id", "series_entries.kind"],
            name="fk_series_entry_uses_entry", ondelete="RESTRICT",
        ),
        # 整书删除会同时级联成员和实体；禁止旧单书路径单独删除绑定实体。
        ForeignKeyConstraint(
            ["project_id", "character_id"], ["characters.project_id", "characters.id"],
            name="fk_series_entry_uses_character", deferrable=True, initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["project_id", "world_setting_id"], ["world_settings.project_id", "world_settings.id"],
            name="fk_series_entry_uses_world", deferrable=True, initially="DEFERRED",
        ),
    )

    project_id: Mapped[int] = mapped_column(primary_key=True)
    series_entry_id: Mapped[int] = mapped_column(primary_key=True, index=True)
    series_id: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    character_id: Mapped[int | None]
    world_setting_id: Mapped[int | None]
