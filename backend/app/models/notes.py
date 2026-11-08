"""作品内独立笔记；不参与 AI 上下文或自动剧情记忆。"""

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ProjectNote(TimestampMixin, Base):
    __tablename__ = "project_notes"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("ix_project_notes_project_pinned", "project_id", "is_pinned", "updated_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    # 收藏／固定是作品级标记，写入不递增 revision、不改 updated_at（见 note_service.set_note_flags）。
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
