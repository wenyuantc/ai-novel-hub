"""短剧作品、剧集、可复用素材与分镜。"""

from typing import Annotated

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

Id = Annotated[int, mapped_column(primary_key=True)]
Description = Annotated[str, mapped_column(Text, default="", server_default="")]
Name = Annotated[str, mapped_column(String(200))]
Sort = Annotated[int, mapped_column(default=0, server_default="0")]


class RevisionMixin:
    revision: Mapped[int] = mapped_column(default=0, server_default="0")


class DramaSeries(RevisionMixin, TimestampMixin, Base):
    __tablename__ = "drama_series"
    __table_args__ = (
        CheckConstraint("aspect_ratio IN ('16:9','9:16','1:1')", name="aspect_ratio"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[Id]
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[Name]
    style: Mapped[str] = mapped_column(String(48), default="3d", server_default="3d")
    aspect_ratio: Mapped[str] = mapped_column(String(8), default="16:9", server_default="16:9")
    description: Mapped[Description]
    sort: Mapped[Sort]


class DramaEpisode(RevisionMixin, TimestampMixin, Base):
    __tablename__ = "drama_episodes"
    __table_args__ = (
        UniqueConstraint("series_id", "number"),
        CheckConstraint("number > 0", name="number"),
        CheckConstraint("resolution IN ('480p','720p','1080p')", name="resolution"),
        CheckConstraint("status IN ('draft','active','completed')", name="status"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[Id]
    series_id: Mapped[int] = mapped_column(ForeignKey("drama_series.id", ondelete="CASCADE"))
    number: Mapped[int]
    title: Mapped[Name]
    source_text: Mapped[Description]
    script: Mapped[Description]
    script_skipped: Mapped[bool] = mapped_column(default=False, server_default=false())
    resolution: Mapped[str] = mapped_column(String(8), default="720p", server_default="720p")
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")


class DramaEpisodeSource(Base):
    __tablename__ = "drama_episode_sources"
    __table_args__ = (
        UniqueConstraint("episode_id", "sort"),
        UniqueConstraint("episode_id", "chapter_id", name="uq_drama_episode_sources_chapter"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[Id]
    episode_id: Mapped[int] = mapped_column(ForeignKey("drama_episodes.id", ondelete="CASCADE"))
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"), index=True)
    chapter_title: Mapped[Name]
    sort: Mapped[Sort]


class DramaCharacter(RevisionMixin, Base):
    __tablename__ = "drama_characters"
    __table_args__ = (UniqueConstraint("series_id", "name"), {"sqlite_autoincrement": True})
    id: Mapped[Id]
    series_id: Mapped[int] = mapped_column(ForeignKey("drama_series.id", ondelete="CASCADE"))
    name: Mapped[Name]
    role: Mapped[Description]
    description: Mapped[Description]
    appearance: Mapped[Description]
    sort: Mapped[Sort]
    novel_character_id: Mapped[int | None] = mapped_column(ForeignKey("characters.id", ondelete="SET NULL"), index=True)


class DramaLook(RevisionMixin, Base):
    __tablename__ = "drama_looks"
    __table_args__ = (
        UniqueConstraint("character_id", "name"),
        Index(
            "uq_drama_looks_default",
            "character_id",
            unique=True,
            sqlite_where=text("is_default"),
            postgresql_where=text("is_default"),
        ),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[Id]
    character_id: Mapped[int] = mapped_column(ForeignKey("drama_characters.id", ondelete="CASCADE"))
    name: Mapped[Name]
    styling: Mapped[Description]
    image_prompt: Mapped[Description]
    is_default: Mapped[bool] = mapped_column(default=False, server_default=false())
    sort: Mapped[Sort]


class DramaScene(RevisionMixin, Base):
    __tablename__ = "drama_scenes"
    __table_args__ = (UniqueConstraint("series_id", "location", "time_of_day"), {"sqlite_autoincrement": True})
    id: Mapped[Id]
    series_id: Mapped[int] = mapped_column(ForeignKey("drama_series.id", ondelete="CASCADE"))
    location: Mapped[Name]
    time_of_day: Mapped[str] = mapped_column(String(200), default="", server_default="")
    description: Mapped[Description]
    lighting: Mapped[Description]
    image_prompt: Mapped[Description]
    novel_world_setting_id: Mapped[int | None] = mapped_column(
        ForeignKey("world_settings.id", ondelete="SET NULL"), index=True
    )


class DramaProp(RevisionMixin, Base):
    __tablename__ = "drama_props"
    __table_args__ = (UniqueConstraint("series_id", "name"), {"sqlite_autoincrement": True})
    id: Mapped[Id]
    series_id: Mapped[int] = mapped_column(ForeignKey("drama_series.id", ondelete="CASCADE"))
    name: Mapped[Name]
    category: Mapped[Description]
    description: Mapped[Description]
    image_prompt: Mapped[Description]


class DramaShot(RevisionMixin, TimestampMixin, Base):
    __tablename__ = "drama_shots"
    __table_args__ = (
        UniqueConstraint("episode_id", "position"),
        CheckConstraint("duration_seconds BETWEEN 2 AND 30", name="duration"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[Id]
    episode_id: Mapped[int] = mapped_column(ForeignKey("drama_episodes.id", ondelete="CASCADE"))
    position: Mapped[int]
    title: Mapped[Name]
    description: Mapped[Description]
    shot_size: Mapped[Description]
    atmosphere: Mapped[Description]
    duration_seconds: Mapped[int] = mapped_column(default=5, server_default="5")
    scene_id: Mapped[int | None] = mapped_column(ForeignKey("drama_scenes.id", ondelete="SET NULL"), index=True)
    video_prompt: Mapped[Description]


class DramaEpisodeLook(Base):
    __tablename__ = "drama_episode_looks"
    episode_id: Mapped[int] = mapped_column(ForeignKey("drama_episodes.id", ondelete="CASCADE"), primary_key=True)
    look_id: Mapped[int] = mapped_column(ForeignKey("drama_looks.id", ondelete="CASCADE"), primary_key=True, index=True)


class DramaEpisodeScene(Base):
    __tablename__ = "drama_episode_scenes"
    episode_id: Mapped[int] = mapped_column(ForeignKey("drama_episodes.id", ondelete="CASCADE"), primary_key=True)
    scene_id: Mapped[int] = mapped_column(
        ForeignKey("drama_scenes.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class DramaEpisodeProp(Base):
    __tablename__ = "drama_episode_props"
    episode_id: Mapped[int] = mapped_column(ForeignKey("drama_episodes.id", ondelete="CASCADE"), primary_key=True)
    prop_id: Mapped[int] = mapped_column(ForeignKey("drama_props.id", ondelete="CASCADE"), primary_key=True, index=True)


class DramaShotLook(Base):
    __tablename__ = "drama_shot_looks"
    shot_id: Mapped[int] = mapped_column(ForeignKey("drama_shots.id", ondelete="CASCADE"), primary_key=True)
    look_id: Mapped[int] = mapped_column(ForeignKey("drama_looks.id", ondelete="CASCADE"), primary_key=True, index=True)


class DramaShotProp(Base):
    __tablename__ = "drama_shot_props"
    shot_id: Mapped[int] = mapped_column(ForeignKey("drama_shots.id", ondelete="CASCADE"), primary_key=True)
    prop_id: Mapped[int] = mapped_column(ForeignKey("drama_props.id", ondelete="CASCADE"), primary_key=True, index=True)
