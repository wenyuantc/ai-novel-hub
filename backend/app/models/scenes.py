"""稳定的章内写作单元；章节正文只保存这些活动场景的有序投影。"""

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, text, true
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Scene(TimestampMixin, Base):
    __tablename__ = "scenes"
    __table_args__ = (
        CheckConstraint("sort >= 0", name="sort_nonnegative"),
        Index(
            "uq_scenes_active_sort", "chapter_id", "sort", unique=True,
            sqlite_where=text("active"), postgresql_where=text("active"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    separator_before: Mapped[str] = mapped_column(Text, default="", server_default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
