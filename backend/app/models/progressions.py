"""设定时间线：稳定主体、文本字段和按章节生效的剧情变化。

关系以角色对、职业以角色与职业、成员以角色与组织为主体，不依赖可被删除的当前挂接行。
所有跨项目引用和字段值的类型由业务服务校验；数据库约束主体形状、章节锚点和事件标识。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

_SUBJECT_SHAPE = (
    "(subject_kind = 'character' AND character_id IS NOT NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'world' AND character_id IS NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NOT NULL AND career_id IS NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'relationship' AND character_id IS NOT NULL AND related_character_id IS NOT NULL "
    "AND character_id < related_character_id AND world_setting_id IS NULL "
    "AND career_id IS NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'career' AND character_id IS NOT NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NOT NULL AND organization_id IS NULL) OR "
    "(subject_kind = 'membership' AND character_id IS NOT NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NULL AND organization_id IS NOT NULL) OR "
    "(subject_kind = 'organization' AND character_id IS NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NULL AND organization_id IS NOT NULL) OR "
    "(subject_kind = 'career_entry' AND character_id IS NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NOT NULL AND organization_id IS NULL)"
)


def _subject_index(name: str, kind: str, *columns: str) -> Index:
    condition = text(f"subject_kind = '{kind}'")
    return Index(name, "project_id", *columns, unique=True, postgresql_where=condition, sqlite_where=condition)


class SettingTimeline(TimestampMixin, Base):
    """一个设定主体的基础快照；事件独立保存，旧档案明确标记待校准。"""

    __tablename__ = "setting_timelines"
    __table_args__ = (
        CheckConstraint(_SUBJECT_SHAPE, name="subject_shape"),
        CheckConstraint("baseline_status IN ('confirmed', 'legacy_unverified')", name="baseline_status"),
        CheckConstraint("revision >= 1", name="positive_revision"),
        _subject_index("uq_setting_timelines_character", "character", "character_id"),
        _subject_index("uq_setting_timelines_world", "world", "world_setting_id"),
        _subject_index("uq_setting_timelines_relationship", "relationship", "character_id", "related_character_id"),
        _subject_index("uq_setting_timelines_career", "career", "character_id", "career_id"),
        _subject_index("uq_setting_timelines_membership", "membership", "character_id", "organization_id"),
        _subject_index("uq_setting_timelines_organization", "organization", "organization_id"),
        _subject_index("uq_setting_timelines_career_entry", "career_entry", "career_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True, nullable=True
    )
    related_character_id: Mapped[int | None] = mapped_column(
        ForeignKey("characters.id", ondelete="CASCADE"), index=True, nullable=True
    )
    world_setting_id: Mapped[int | None] = mapped_column(
        ForeignKey("world_settings.id", ondelete="CASCADE"), index=True, nullable=True
    )
    career_id: Mapped[int | None] = mapped_column(
        ForeignKey("careers.id", ondelete="CASCADE"), index=True, nullable=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=True
    )
    baseline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)
    baseline_status: Mapped[str] = mapped_column(
        String(24), default="confirmed", server_default="confirmed", nullable=False
    )
    legacy_compat_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


class SettingTextField(TimestampMixin, Base):
    """条目字段绑定；保留旧物理身份，让旧文本事件继续关联原行。"""

    __tablename__ = "setting_text_fields"
    __table_args__ = (
        UniqueConstraint("timeline_id", "label", name="uq_setting_text_fields_timeline_id_label"),
        UniqueConstraint("timeline_id", "definition_id", name="uq_setting_text_fields_timeline_id_definition_id"),
        CheckConstraint("length(trim(label)) > 0", name="label_nonempty"),
        CheckConstraint(
            "(definition_id IS NULL AND label IS NOT NULL) OR (definition_id IS NOT NULL AND label IS NULL)",
            name="binding_shape",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timeline_id: Mapped[int] = mapped_column(
        ForeignKey("setting_timelines.id", ondelete="CASCADE"), index=True, nullable=False
    )
    definition_id: Mapped[int | None] = mapped_column(
        ForeignKey("setting_field_definitions.id", ondelete="NO ACTION"), index=True, nullable=True
    )
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    base_value: Mapped[Any] = mapped_column(JSON(none_as_null=False), default="", server_default='""', nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class SettingProgression(TimestampMixin, Base):
    """一个字段在故事位置发生的变化；章锚删除后事实保留为孤儿记录。"""

    __tablename__ = "setting_progressions"
    __table_args__ = (
        CheckConstraint(
            "(field_key IS NOT NULL AND field_key <> '' AND custom_field_id IS NULL) OR "
            "(field_key IS NULL AND custom_field_id IS NOT NULL)",
            name="field_reference",
        ),
        CheckConstraint(
            "(anchor_kind = 'chapter' AND chapter_no IS NULL) OR "
            "(anchor_kind = 'planned' AND chapter_id IS NULL AND chapter_no IS NOT NULL AND chapter_no >= 1)",
            name="anchor_shape",
        ),
        CheckConstraint("boundary IN ('start', 'end')", name="boundary"),
        CheckConstraint("operation IN ('append', 'replace')", name="operation"),
        CheckConstraint("source IN ('manual', 'analysis', 'legacy_import')", name="source"),
        CheckConstraint("protection IN ('automatic', 'confirmed', 'manual')", name="protection"),
        CheckConstraint("sort_order >= 0", name="nonnegative_order"),
        CheckConstraint("revision >= 1", name="positive_revision"),
        Index(
            "uq_setting_progressions_analysis_field",
            "source_change_id",
            "field_key",
            unique=True,
            postgresql_where=text("source_change_id IS NOT NULL AND field_key IS NOT NULL"),
            sqlite_where=text("source_change_id IS NOT NULL AND field_key IS NOT NULL"),
        ),
        Index(
            "uq_setting_progressions_analysis_custom",
            "source_change_id",
            "custom_field_id",
            unique=True,
            postgresql_where=text("source_change_id IS NOT NULL AND custom_field_id IS NOT NULL"),
            sqlite_where=text("source_change_id IS NOT NULL AND custom_field_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    timeline_id: Mapped[int] = mapped_column(
        ForeignKey("setting_timelines.id", ondelete="CASCADE"), index=True, nullable=False
    )
    field_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 不用 RESTRICT：删除主体时字段与事件会在同一语句中级联清理；单独删除被引用字段仍由 NO ACTION 阻止。
    custom_field_id: Mapped[int | None] = mapped_column(ForeignKey("setting_text_fields.id"), index=True, nullable=True)
    operation: Mapped[str] = mapped_column(String(8), nullable=False)
    value: Mapped[Any] = mapped_column(JSON(none_as_null=False), nullable=False)
    anchor_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    chapter_id: Mapped[int | None] = mapped_column(
        ForeignKey("chapters.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 只有 planned 锚点保存数字；chapter_id 被删除后不得靠旧数字自动重绑。
    chapter_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anchor_label: Mapped[str] = mapped_column(String(250), default="", server_default="", nullable=False)
    boundary: Mapped[str] = mapped_column(String(8), default="start", server_default="start", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual", nullable=False)
    protection: Mapped[str] = mapped_column(String(12), default="manual", server_default="manual", nullable=False)
    # 删章会同时解除章锚并经分析级联删来源；延迟校验避免 PG 在两条 SET NULL 路径之间误报外键错误。
    source_change_id: Mapped[int | None] = mapped_column(
        ForeignKey("analysis_changes.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"),
        index=True,
        nullable=True,
    )
    source_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("setting_progressions.id", ondelete="SET NULL"), nullable=True
    )
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(String(200), default="", server_default="", nullable=False)
