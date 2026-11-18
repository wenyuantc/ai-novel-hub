"""作品正文的修订记录。场景或章节删除时保留历史标签和失效明细。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RevisionBatch(Base):
    __tablename__ = "revision_batches"
    __table_args__ = (
        UniqueConstraint("project_id", "actor_id", "request_id", name="uq_revision_batches_request"),
        CheckConstraint("status IN ('applied','undone')", name="status"),
        CheckConstraint("chapter_count > 0 AND hit_count >= chapter_count", name="counts"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    request_id: Mapped[str | None] = mapped_column(String(36))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    query: Mapped[str] = mapped_column(Text)
    replacement: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    chapter_count: Mapped[int]
    hit_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RevisionBatchScene(Base):
    __tablename__ = "revision_batch_scenes"
    __table_args__ = (
        UniqueConstraint("batch_id", "scene_id", name="uq_revision_batch_scenes_target"),
        CheckConstraint("applied_revision >= 0", name="applied_revision_nonnegative"),
        CheckConstraint("hit_count > 0", name="hit_count_positive"),
        CheckConstraint("chapter_slot > 0", name="chapter_slot_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("revision_batches.id", ondelete="CASCADE"))
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"), index=True)
    scene_id: Mapped[int | None] = mapped_column(ForeignKey("scenes.id", ondelete="SET NULL"), index=True)
    # 同号、同名章节也可能同时删除。批内组号始终保留，显示标签不参加章节重排。
    chapter_slot: Mapped[int]
    chapter_no_at_apply: Mapped[int]
    chapter_title: Mapped[str] = mapped_column(String(200))
    # 导入前已冲突的目标没有可在新库使用的修订号。
    applied_revision: Mapped[int | None]
    before_document: Mapped[dict[str, Any]] = mapped_column(JSON)
    start_offsets: Mapped[list[int]] = mapped_column(JSON)
    hit_count: Mapped[int]
