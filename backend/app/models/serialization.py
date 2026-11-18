"""连载日历、作者确认的稿件与各平台实际发布记录。"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.models.base import Base


class SerializationGoal(Base):
    __tablename__ = "serialization_goals"
    __table_args__ = (
        CheckConstraint("daily_goal > 0", name="positive_daily_goal"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
    )

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    daily_goal: Mapped[int | None]
    revision: Mapped[int] = mapped_column(default=0, server_default="0")


class SerializationEntry(Base):
    __tablename__ = "serialization_entries"
    __table_args__ = (
        UniqueConstraint("chapter_id"),
        CheckConstraint("stage IN ('revision','proofreading','final')", name="stage"),
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        Index("ix_serialization_entries_project_planned", "project_id", "planned_date"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"))
    stage: Mapped[str] = mapped_column(String(16), default="revision", server_default="revision")
    planned_date: Mapped[date | None] = mapped_column(Date)
    revision: Mapped[int] = mapped_column(default=0, server_default="0")


class SerializationEdition(Base):
    __tablename__ = "serialization_editions"
    __table_args__ = (
        UniqueConstraint("entry_id", "seq"),
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint("chapter_no_at_confirmation > 0", name="chapter_no_positive"),
        CheckConstraint("length(fingerprint) = 64", name="fingerprint_length"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("serialization_entries.id", ondelete="CASCADE"))
    seq: Mapped[int]
    # 快照章号不随当前章节重排。物理列名称与参与平移的 chapter_no 区别开。
    chapter_no_at_confirmation: Mapped[int]
    chapter_no = synonym("chapter_no_at_confirmation")
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON(none_as_null=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SerializationPublication(Base):
    __tablename__ = "serialization_publications"
    __table_args__ = (
        UniqueConstraint("project_id", "actor_id", "request_id", name="uq_serialization_publications_request"),
        CheckConstraint("length(trim(channel)) > 0", name="channel_nonempty"),
        Index("ix_serialization_publications_edition_time", "edition_id", "published_at"),
        Index("ix_serialization_publications_project_time", "project_id", "published_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    edition_id: Mapped[int] = mapped_column(ForeignKey("serialization_editions.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(100))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    request_id: Mapped[str | None] = mapped_column(String(36))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
