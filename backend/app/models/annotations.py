"""协作审稿记录独立于正文、写作历史和作品授权。"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AnnotationThread(Base):
    __tablename__ = "annotation_threads"
    __table_args__ = (
        CheckConstraint("original_start >= 0 AND original_end > original_start", name="original_range"),
        CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="current_range"),
        Index("ix_annotation_threads_chapter", "project_id", "chapter_id", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"))
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    original_start: Mapped[int]
    original_end: Mapped[int]
    start_offset: Mapped[int]
    end_offset: Mapped[int]
    excerpt: Mapped[str] = mapped_column(Text)
    selection_hash: Mapped[str] = mapped_column(String(64))
    scene_hash: Mapped[str] = mapped_column(String(64))
    anchor_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnnotationMessage(Base):
    __tablename__ = "annotation_messages"
    __table_args__ = (
        UniqueConstraint("author_id", "operation_id", name="uq_annotation_messages_author_operation"),
        Index("ix_annotation_messages_thread", "thread_id", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("annotation_threads.id", ondelete="CASCADE"))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    author_name: Mapped[str] = mapped_column(String(64))
    body: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
