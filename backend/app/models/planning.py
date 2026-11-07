"""稳定的章级规划：来源脱离不删除规划，设定关联保持真实外键。"""

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class PlanningUnit(TimestampMixin, Base):
    __tablename__ = "planning_units"
    __table_args__ = (
        CheckConstraint("binding_epoch >= 0", name="binding_epoch_nonnegative"),
        CheckConstraint(
            "NOT retired OR (chapter_id IS NULL AND plan_id IS NULL AND scene_id IS NULL)", name="retired_detached"
        ),
        CheckConstraint("source_level IN ('chapter', 'scene')", name="source_level"),
        CheckConstraint(
            "(source_level = 'chapter' AND scene_id IS NULL) OR "
            "(source_level = 'scene' AND chapter_id IS NULL AND plan_id IS NULL)", name="source_shape"
        ),
        *(CheckConstraint(f"{field}_mode IN ('inherit', 'override')", name=f"{field}_mode")
          for field in ("pov", "tags", "character", "world", "organization", "career", "relationship")),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    chapter_id: Mapped[int | None] = mapped_column(ForeignKey("chapters.id", ondelete="SET NULL"), unique=True)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("chapter_plans.id", ondelete="SET NULL"), unique=True)
    scene_id: Mapped[int | None] = mapped_column(ForeignKey("scenes.id", ondelete="SET NULL"), unique=True)
    source_level: Mapped[str] = mapped_column(String(8), default="chapter", server_default="chapter")
    pov_mode: Mapped[str] = mapped_column(String(8), default="inherit", server_default="inherit")
    tags_mode: Mapped[str] = mapped_column(String(8), default="inherit", server_default="inherit")
    character_mode: Mapped[str] = mapped_column(String(8), default="inherit", server_default="inherit")
    world_mode: Mapped[str] = mapped_column(String(8), default="inherit", server_default="inherit")
    organization_mode: Mapped[str] = mapped_column(String(8), default="inherit", server_default="inherit")
    career_mode: Mapped[str] = mapped_column(String(8), default="inherit", server_default="inherit")
    relationship_mode: Mapped[str] = mapped_column(String(8), default="inherit", server_default="inherit")
    pov_character_id: Mapped[int | None] = mapped_column(ForeignKey("characters.id", ondelete="SET NULL"), index=True)
    last_source_label: Mapped[str] = mapped_column(String(300), default="", server_default="")
    last_volume_label: Mapped[str] = mapped_column(String(300), default="", server_default="")
    detached_character_ids: Mapped[list[int]] = mapped_column(JSON, default=list, server_default="[]")
    binding_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retired: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class PlanningTag(TimestampMixin, Base):
    __tablename__ = "planning_tags"
    __table_args__ = (UniqueConstraint("project_id", "name"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    tone: Mapped[str] = mapped_column(String(16), default="default", server_default="default")


class PlanningUnitTag(TimestampMixin, Base):
    __tablename__ = "planning_unit_tags"

    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("planning_tags.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class PlanningUnitCharacter(TimestampMixin, Base):
    """仅用于场景本地角色；章级手动角色仍由 ChapterCharacter 持有。"""

    __tablename__ = "planning_unit_characters"

    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), primary_key=True)
    character_id: Mapped[int] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class PlanningUnitWorld(TimestampMixin, Base):
    __tablename__ = "planning_unit_world"

    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), primary_key=True)
    world_id: Mapped[int] = mapped_column(
        ForeignKey("world_settings.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class PlanningUnitOrganization(TimestampMixin, Base):
    __tablename__ = "planning_unit_organizations"

    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class PlanningUnitCareer(TimestampMixin, Base):
    __tablename__ = "planning_unit_careers"

    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), primary_key=True)
    career_id: Mapped[int] = mapped_column(ForeignKey("careers.id", ondelete="CASCADE"), primary_key=True, index=True)


class PlanningUnitRelationship(TimestampMixin, Base):
    __tablename__ = "planning_unit_relationships"

    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), primary_key=True)
    timeline_id: Mapped[int] = mapped_column(
        ForeignKey("setting_timelines.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Subplot(TimestampMixin, Base):
    __tablename__ = "subplots"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    tone: Mapped[str] = mapped_column(String(16), default="default", server_default="default")
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class SubplotNode(TimestampMixin, Base):
    __tablename__ = "subplot_nodes"
    __table_args__ = (UniqueConstraint("subplot_id", "unit_id"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(primary_key=True)
    subplot_id: Mapped[int] = mapped_column(ForeignKey("subplots.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)


class SubplotCrossing(TimestampMixin, Base):
    __tablename__ = "subplot_crossings"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("planning_units.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class SubplotCrossingParticipant(TimestampMixin, Base):
    __tablename__ = "subplot_crossing_participants"

    crossing_id: Mapped[int] = mapped_column(ForeignKey("subplot_crossings.id", ondelete="CASCADE"), primary_key=True)
    # 多级项目级联的删除顺序不固定；延迟到提交时校验，人工删支线仍由服务显式拒绝引用。
    subplot_id: Mapped[int] = mapped_column(
        ForeignKey("subplots.id", deferrable=True, initially="DEFERRED"), primary_key=True, index=True
    )
