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


class SceneMove(Base):
    """同书内移动的持久归属证据，与两章投影及版本在同一事务提交。"""

    __tablename__ = "scene_moves"
    __table_args__ = (
        CheckConstraint("from_chapter_id <> to_chapter_id", name="distinct_chapters"),
        CheckConstraint("from_version_seq > 0", name="from_version_positive"),
        CheckConstraint("to_version_seq > 0", name="to_version_positive"),
        Index(
            "uq_scene_moves_first", "scene_id", unique=True,
            sqlite_where=text("first_move"), postgresql_where=text("first_move"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    from_chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"), index=True)
    to_chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"), index=True)
    planning_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("planning_units.id", ondelete="SET NULL"), index=True,
    )
    first_move: Mapped[bool] = mapped_column(Boolean)
    from_version_seq: Mapped[int | None] = mapped_column(Integer)
    to_version_seq: Mapped[int | None] = mapped_column(Integer)
