"""当前整理标签与设定分类；四实体在 SQLite 上不再复用自动分配的身份。"""

from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_ENTITY_TABLES = ("characters", "world_settings", "organizations", "careers")
_RULE_SHAPE = (
    "(kind = 'tag' AND tag IS NOT NULL AND length(trim(tag)) > 0 "
    "AND definition_id IS NULL AND option_id IS NULL) OR "
    "(kind = 'dropdown' AND tag IS NULL AND definition_id IS NOT NULL AND option_id IS NOT NULL)"
)


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


@contextmanager
def _sqlite_rebuild(*, preserve_entity_sequences):
    """父表必须关闭 FK 后重建，避免旧子行被 CASCADE／SET NULL；不改变全局引擎。"""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("旧资料存在无效外键，请先修复后重试分类迁移")
    sequences = dict(bind.execute(sa.text(
        "SELECT name,seq FROM sqlite_sequence WHERE name IN ('characters','world_settings','organizations','careers')"
    )).all())
    enabled = bind.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    try:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
            raise RuntimeError("不能安全关闭迁移连接的外键校验，拒绝重建父表")
        yield
        if preserve_entity_sequences:
            for name, sequence in sequences.items():
                bind.execute(sa.text(
                    "UPDATE sqlite_sequence SET seq=CASE WHEN seq < :seq THEN :seq ELSE seq END WHERE name=:name"
                ), {"seq": sequence, "name": name})
        if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("分类迁移后外键校验失败")
    finally:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _alter_entity_tags(*, add):
    """SQLite 的标签列与身份约束一次重建；PG 继续原位 ADD／DROP COLUMN。"""
    for table in _ENTITY_TABLES:
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(
                table, recreate="always", table_kwargs={"sqlite_autoincrement": add},
                naming_convention={"pk": "pk_%(table_name)s"},
            ) as batch:
                if add:
                    batch.add_column(sa.Column("setting_tags", sa.JSON(), server_default="[]", nullable=False))
                else:
                    batch.drop_column("setting_tags")
        elif add:
            op.add_column(table, sa.Column("setting_tags", sa.JSON(), server_default="[]", nullable=False))
        else:
            op.drop_column(table, "setting_tags")


def _upgrade() -> None:
    # 标签属于当前条目，不能从 projects.tags 猜测或复制。
    _alter_entity_tags(add=True)
    op.add_column("projects", sa.Column("categories_revision", sa.Integer(), server_default="0", nullable=False))
    op.create_table(
        "setting_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("applicable_types", sa.JSON(), nullable=False),
        sa.Column("match_mode", sa.String(8), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("project_id", "name"),
        sa.CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        sa.CheckConstraint("match_mode IN ('all', 'any')", name="match_mode"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_setting_categories_project_id", "setting_categories", ["project_id"])
    op.create_table(
        "setting_category_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("setting_categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("tag", sa.String(50), nullable=True),
        # PG 的项目级联可能先到定义／选项；延迟到事务末核验，不改变单删保护。
        sa.Column("definition_id", sa.Integer(), sa.ForeignKey(
            "setting_field_definitions.id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED",
        )),
        sa.Column("option_id", sa.Integer(), sa.ForeignKey(
            "setting_field_options.id", ondelete="NO ACTION", deferrable=True, initially="DEFERRED",
        )),
        *_timestamps(),
        sa.CheckConstraint(_RULE_SHAPE, name="rule_shape"),
        sqlite_autoincrement=True,
    )
    for column in ("category_id", "definition_id", "option_id"):
        op.create_index(f"ix_setting_category_rules_{column}", "setting_category_rules", [column])


def upgrade() -> None:
    with _sqlite_rebuild(preserve_entity_sequences=True):
        _upgrade()


def _check_downgrade() -> None:
    bind = op.get_bind()
    for name in ("setting_categories", "setting_category_rules"):
        if bind.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first() is not None:
            raise RuntimeError("存在设定分类或规则，不能无损降级；请先备份并明确处理新资料")
    for name in _ENTITY_TABLES:
        table = sa.table(name, sa.column("setting_tags", sa.JSON()))
        for tags in bind.execute(sa.select(table.c.setting_tags)).scalars():
            if tags != []:
                raise RuntimeError("存在条目整理标签，不能无损降级；请先备份并明确处理新资料")


def downgrade() -> None:
    # 先扫描全部新资料，再做任何 DROP，失败时不留下部分降级结构。
    _check_downgrade()
    with _sqlite_rebuild(preserve_entity_sequences=False):
        op.drop_table("setting_category_rules")
        op.drop_table("setting_categories")
        _alter_entity_tags(add=False)
        op.drop_column("projects", "categories_revision")
