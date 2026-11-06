"""作品字段目录和稳定引用身份；条目值仍保存在原有时间线字段表。"""

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

_REFERENCE_SHAPE = (
    "(kind = 'character' AND character_id IS NOT NULL AND world_setting_id IS NULL "
    "AND organization_id IS NULL AND career_id IS NULL) OR "
    "(kind = 'world' AND character_id IS NULL AND world_setting_id IS NOT NULL "
    "AND organization_id IS NULL AND career_id IS NULL) OR "
    "(kind = 'organization' AND character_id IS NULL AND world_setting_id IS NULL "
    "AND organization_id IS NOT NULL AND career_id IS NULL) OR "
    "(kind = 'career' AND character_id IS NULL AND world_setting_id IS NULL "
    "AND organization_id IS NULL AND career_id IS NOT NULL)"
)


class SettingFieldDefinition(TimestampMixin, Base):
    """一部作品内可复用的字段配置，适用范围和可选状态由领域服务校验。"""

    __tablename__ = "setting_field_definitions"
    __table_args__ = (
        UniqueConstraint("project_id", "label"),
        CheckConstraint("length(trim(label)) > 0", name="label_nonempty"),
        CheckConstraint("field_type IN ('text', 'line', 'dropdown', 'reference')", name="field_type"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    field_type: Mapped[str] = mapped_column(String(16), nullable=False)
    applicable_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reference_types: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class SettingFieldOption(TimestampMixin, Base):
    """稳定单选项；改名和停用均不改变已保存历史的身份。"""

    __tablename__ = "setting_field_options"
    __table_args__ = (
        UniqueConstraint("definition_id", "label"),
        CheckConstraint("length(trim(label)) > 0", name="label_nonempty"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    definition_id: Mapped[int] = mapped_column(
        ForeignKey("setting_field_definitions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


def _target_index(kind: str, column: str) -> Index:
    condition = text(f"kind = '{kind}'")
    return Index(
        f"uq_setting_reference_handles_{kind}", "project_id", column,
        unique=True, postgresql_where=condition, sqlite_where=condition,
    )


class SettingReferenceHandle(TimestampMixin, Base):
    """不可复用的目标身份。JSON 的依赖检查仍由服务负责，不能只靠目标外键。"""

    __tablename__ = "setting_reference_handles"
    __table_args__ = (
        CheckConstraint(_REFERENCE_SHAPE, name="target_shape"),
        _target_index("character", "character_id"),
        _target_index("world", "world_setting_id"),
        _target_index("organization", "organization_id"),
        _target_index("career", "career_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # NO ACTION 在语句结束时校验，允许同一项目删除同时级联删除目标和句柄。
    character_id: Mapped[int | None] = mapped_column(
        ForeignKey("characters.id", ondelete="NO ACTION"), index=True, nullable=True
    )
    world_setting_id: Mapped[int | None] = mapped_column(
        ForeignKey("world_settings.id", ondelete="NO ACTION"), index=True, nullable=True
    )
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="NO ACTION"), index=True, nullable=True
    )
    career_id: Mapped[int | None] = mapped_column(
        ForeignKey("careers.id", ondelete="NO ACTION"), index=True, nullable=True
    )
