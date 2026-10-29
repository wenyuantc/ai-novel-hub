"""实例内共享内容与审核快照；与项目无关，不参与项目备份。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, false, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

WORKSHOP_CATEGORIES = {
    "general": "通用",
    "xuanhuan": "玄幻仙侠",
    "wuxia": "武侠",
    "romance": "言情",
    "scifi": "科幻",
    "suspense": "悬疑",
    "history": "历史",
    "urban": "都市",
    "other": "其他",
}


class WorkshopContent:
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    prompt_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    content: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class WorkshopItem(WorkshopContent, TimestampMixin, Base):
    __tablename__ = "workshop_items"
    __table_args__ = (Index("ix_workshop_items_prompt_key", "prompt_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    status: Mapped[str] = mapped_column(
        String(8), default="active", server_default="active", nullable=False, index=True
    )
    hidden_by: Mapped[str | None] = mapped_column(String(8))
    like_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    download_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkshopSubmission(WorkshopContent, TimestampMixin, Base):
    __tablename__ = "workshop_submissions"
    __table_args__ = (
        Index(
            "uq_workshop_pending_item",
            "item_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("workshop_items.id", ondelete="CASCADE"), index=True)
    submitter_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(
        String(10), default="pending", server_default="pending", nullable=False, index=True
    )
    review_note: Mapped[str] = mapped_column(String(500), default="", server_default="", nullable=False)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkshopLike(Base):
    __tablename__ = "workshop_likes"

    item_id: Mapped[int] = mapped_column(ForeignKey("workshop_items.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class WorkshopImport(Base):
    __tablename__ = "workshop_imports"

    item_id: Mapped[int] = mapped_column(ForeignKey("workshop_items.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
