"""本人小说模板、独立图片和持久建书去重收据。"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class NovelTemplate(TimestampMixin, Base):
    __tablename__ = "novel_templates"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint("format_version = 1", name="format_version"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    format_version: Mapped[int] = mapped_column(default=1, server_default="1")
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    counts: Mapped[dict[str, int]] = mapped_column(JSON)
    source_title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(default=1, server_default="1")


class NovelTemplateImage(Base):
    __tablename__ = "novel_template_images"
    __table_args__ = (
        UniqueConstraint("template_id", "local_ref"),
        CheckConstraint("width > 0 AND height > 0 AND byte_size > 0", name="positive_size"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("novel_templates.id", ondelete="CASCADE"), index=True)
    local_ref: Mapped[str] = mapped_column(String(100))
    mime_type: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))


class NovelTemplateCreationReceipt(Base):
    __tablename__ = "novel_template_creation_receipts"
    __table_args__ = (
        UniqueConstraint("owner_id", "intent_key"),
        CheckConstraint("request_version = 1", name="request_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    intent_key: Mapped[str] = mapped_column(String(36))
    request_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    request_digest: Mapped[str] = mapped_column(String(64))
    result_project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
