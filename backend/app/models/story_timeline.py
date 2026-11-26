"""作品固定历法，以及按稳定章景身份登记的显式日期。"""

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StoryCalendar(Base):
    __tablename__ = "story_calendars"
    __table_args__ = (CheckConstraint("revision > 0", name="revision_positive"),)

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    era_name: Mapped[str] = mapped_column(String(64))
    months: Mapped[list[dict]] = mapped_column(JSON)
    revision: Mapped[int]


class _DateFields:
    year: Mapped[int]
    month_id: Mapped[str] = mapped_column(String(64))
    day: Mapped[int]


class ChapterStoryDate(_DateFields, Base):
    __tablename__ = "chapter_story_dates"
    __table_args__ = (CheckConstraint("year > 0 AND day > 0", name="positive_date"),)

    chapter_id: Mapped[int] = mapped_column(ForeignKey("chapters.id", ondelete="CASCADE"), primary_key=True)


class SceneStoryDate(_DateFields, Base):
    __tablename__ = "scene_story_dates"
    __table_args__ = (CheckConstraint("year > 0 AND day > 0", name="positive_date"),)

    scene_id: Mapped[int] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), primary_key=True)
