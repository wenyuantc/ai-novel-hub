"""保存分类规则，以稳定字段／选项身份计算成员，不保存成员快照。"""

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

_RULE_SHAPE = (
    "(kind = 'tag' AND tag IS NOT NULL AND length(trim(tag)) > 0 "
    "AND definition_id IS NULL AND option_id IS NULL) OR "
    "(kind = 'dropdown' AND tag IS NULL AND definition_id IS NOT NULL AND option_id IS NOT NULL)"
)


class SettingCategory(TimestampMixin, Base):
    """一部作品的当前分类配置，成员由当前标签和时点下拉值按需求得。"""

    __tablename__ = "setting_categories"
    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint("match_mode IN ('all', 'any')", name="match_mode"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    applicable_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    match_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class SettingCategoryRule(TimestampMixin, Base):
    """恰一种条件载荷；dropdown 归属、字段类型及规则交集由领域服务校验。"""

    __tablename__ = "setting_category_rules"
    __table_args__ = (
        CheckConstraint(_RULE_SHAPE, name="rule_shape"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("setting_categories.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    tag: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 延迟 NO ACTION：PG 可能先删定义再级联分类；事务末校验仍阻止单删在用配置。
    definition_id: Mapped[int | None] = mapped_column(
        ForeignKey("setting_field_definitions.id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        index=True, nullable=True,
    )
    option_id: Mapped[int | None] = mapped_column(
        ForeignKey("setting_field_options.id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED"),
        index=True, nullable=True,
    )
