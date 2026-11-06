"""类型化字段目录、稳定引用和独立组织／职业条目时间线。

原字段行与事件外键不变；旧基础文本逐字编码为 JSON 字符串。
"""

from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_OLD_SUBJECT_SHAPE = (
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
    "AND world_setting_id IS NULL AND career_id IS NULL AND organization_id IS NOT NULL)"
)
_NEW_SUBJECT_SHAPE = (
    _OLD_SUBJECT_SHAPE + " OR "
    "(subject_kind = 'organization' AND character_id IS NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NULL AND organization_id IS NOT NULL) OR "
    "(subject_kind = 'career_entry' AND character_id IS NULL AND related_character_id IS NULL "
    "AND world_setting_id IS NULL AND career_id IS NOT NULL AND organization_id IS NULL)"
)
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


@contextmanager
def _sqlite_rebuild():
    """两个被引用父表必须关闭 FK 后重建，随后校验全图并恢复原开关。"""
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    enabled = sqlite and bind.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    if sqlite and bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("旧资料存在无效外键，请先修复后重试字段迁移")
    if enabled:
        with op.get_context().autocommit_block():
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
        if sqlite and bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("类型化字段迁移后外键校验失败")
    finally:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _create_catalog():
    op.create_table(
        "setting_field_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("field_type", sa.String(16), nullable=False),
        sa.Column("applicable_types", sa.JSON(), nullable=False),
        sa.Column("reference_types", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("project_id", "label"),
        sa.CheckConstraint("length(trim(label)) > 0", name="label_nonempty"),
        sa.CheckConstraint("field_type IN ('text', 'line', 'dropdown', 'reference')", name="field_type"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_setting_field_definitions_project_id", "setting_field_definitions", ["project_id"])
    op.create_table(
        "setting_field_options",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("definition_id", sa.Integer(),
                  sa.ForeignKey("setting_field_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("definition_id", "label"),
        sa.CheckConstraint("length(trim(label)) > 0", name="label_nonempty"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_setting_field_options_definition_id", "setting_field_options", ["definition_id"])
    targets = (
        ("character", "character_id", "characters"),
        ("world", "world_setting_id", "world_settings"),
        ("organization", "organization_id", "organizations"),
        ("career", "career_id", "careers"),
    )
    op.create_table(
        "setting_reference_handles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        *(sa.Column(column, sa.Integer(), sa.ForeignKey(f"{table}.id", ondelete="NO ACTION"), nullable=True)
          for _, column, table in targets),
        *_timestamps(),
        sa.CheckConstraint(_REFERENCE_SHAPE, name="target_shape"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_setting_reference_handles_project_id", "setting_reference_handles", ["project_id"])
    for kind, column, _ in targets:
        op.create_index(f"ix_setting_reference_handles_{column}", "setting_reference_handles", [column])
        condition = sa.text(f"kind = '{kind}'")
        op.create_index(
            f"uq_setting_reference_handles_{kind}", "setting_reference_handles", ["project_id", column],
            unique=True, postgresql_where=condition, sqlite_where=condition,
        )


def _upgrade_timelines():
    with op.batch_alter_table("setting_timelines") as batch:
        batch.drop_constraint(op.f("ck_setting_timelines_subject_shape"), type_="check")
        batch.create_check_constraint(op.f("ck_setting_timelines_subject_shape"), _NEW_SUBJECT_SHAPE)
        for kind, column in (("organization", "organization_id"), ("career_entry", "career_id")):
            condition = sa.text(f"subject_kind = '{kind}'")
            batch.create_index(
                f"uq_setting_timelines_{kind}", ["project_id", column], unique=True,
                postgresql_where=condition, sqlite_where=condition,
            )
    for kind, column, table in (
        ("organization", "organization_id", "organizations"), ("career_entry", "career_id", "careers"),
    ):
        op.get_bind().execute(sa.text(
            f"INSERT INTO setting_timelines(project_id,subject_kind,{column},baseline) "
            f"SELECT project_id,'{kind}',id,'{{}}' FROM {table} ORDER BY id"
        ))


def _upgrade_fields():
    sqlite = op.get_bind().dialect.name == "sqlite"
    if sqlite:
        # json_quote 而非 json()：旧文本即使看起来像对象／null 也必须保持字符串。
        op.get_bind().execute(sa.text("UPDATE setting_text_fields SET base_value=json_quote(base_value)"))
    else:
        op.alter_column("setting_text_fields", "base_value", server_default=None)
    # 绑定 ID 也是事件／效果持久身份；删除最高空绑定后不能分配给另一个定义。
    with op.batch_alter_table("setting_text_fields", table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.add_column(sa.Column("definition_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_setting_text_fields_definition_id_setting_field_definitions",
            "setting_field_definitions", ["definition_id"], ["id"], ondelete="NO ACTION",
        )
        batch.alter_column("label", existing_type=sa.String(100), nullable=True)
        batch.alter_column(
            "base_value", existing_type=sa.Text(), type_=sa.JSON(none_as_null=False),
            nullable=False, server_default=sa.text("'\"\"'"), postgresql_using="to_json(base_value)",
        )
        batch.create_index("ix_setting_text_fields_definition_id", ["definition_id"])
        batch.create_unique_constraint("uq_setting_text_fields_timeline_id_definition_id", ["timeline_id", "definition_id"])
        batch.create_check_constraint(
            op.f("ck_setting_text_fields_binding_shape"),
            "(definition_id IS NULL AND label IS NOT NULL) OR (definition_id IS NOT NULL AND label IS NULL)",
        )


def upgrade() -> None:
    with _sqlite_rebuild():
        op.add_column("projects", sa.Column("fields_revision", sa.Integer(), server_default="0", nullable=False))
        _create_catalog()
        _upgrade_timelines()
        _upgrade_fields()


def _check_downgrade():
    bind = op.get_bind()
    for table in ("setting_field_definitions", "setting_field_options", "setting_reference_handles"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("存在类型化字段配置或引用身份，不能无损降级；请先备份并处理新资料")
    timelines = sa.table(
        "setting_timelines", sa.column("id", sa.Integer()), sa.column("subject_kind", sa.String()),
        sa.column("baseline", sa.JSON()), sa.column("revision", sa.Integer()),
        sa.column("baseline_status", sa.String()), sa.column("legacy_compat_allowed", sa.Boolean()),
    )
    for row in bind.execute(sa.select(timelines).where(
        timelines.c.subject_kind.in_(("organization", "career_entry"))
    )).mappings():
        if row["baseline"] != {} or row["revision"] != 1 or row["baseline_status"] != "confirmed" or row["legacy_compat_allowed"]:
            raise RuntimeError("存在独立组织或职业条目资料，不能无损降级")
        for table in ("setting_text_fields", "setting_progressions"):
            if bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE timeline_id=:id LIMIT 1"), {"id": row["id"]}).first():
                raise RuntimeError("存在独立组织或职业条目字段／历史，不能无损降级")
    fields = sa.table("setting_text_fields", sa.column("base_value", sa.JSON()), sa.column("definition_id", sa.Integer()))
    for value, definition_id in bind.execute(sa.select(fields)):
        if definition_id is not None or not isinstance(value, str):
            raise RuntimeError("存在旧版不能表示的字段值，不能无损降级")
    events = sa.table(
        "setting_progressions", sa.column("value", sa.JSON()), sa.column("custom_field_id", sa.Integer()),
    )
    for value in bind.execute(sa.select(events.c.value).where(events.c.custom_field_id.is_not(None))).scalars():
        if not isinstance(value, str):
            raise RuntimeError("存在旧版不能表示的自定义字段历史，不能无损降级")


def downgrade() -> None:
    # 所有检查必须先于 DDL 和 DELETE，不能失败后只留下部分旧版本结构。
    _check_downgrade()
    with _sqlite_rebuild():
        bind = op.get_bind()
        sqlite = bind.dialect.name == "sqlite"
        if sqlite:
            bind.execute(sa.text("UPDATE setting_text_fields SET base_value=json_extract(base_value,'$')"))
        else:
            op.alter_column("setting_text_fields", "base_value", server_default=None)
        with op.batch_alter_table("setting_text_fields", table_kwargs={"sqlite_autoincrement": False}) as batch:
            batch.drop_constraint(op.f("ck_setting_text_fields_binding_shape"), type_="check")
            batch.drop_constraint("uq_setting_text_fields_timeline_id_definition_id", type_="unique")
            batch.drop_index("ix_setting_text_fields_definition_id")
            batch.drop_constraint("fk_setting_text_fields_definition_id_setting_field_definitions", type_="foreignkey")
            batch.drop_column("definition_id")
            batch.alter_column("label", existing_type=sa.String(100), nullable=False)
            batch.alter_column(
                "base_value", existing_type=sa.JSON(), type_=sa.Text(), nullable=False,
                server_default="", postgresql_using="base_value #>> '{}'",
            )
        bind.execute(sa.text("DELETE FROM setting_timelines WHERE subject_kind IN ('organization','career_entry')"))
        with op.batch_alter_table("setting_timelines") as batch:
            batch.drop_index("uq_setting_timelines_organization")
            batch.drop_index("uq_setting_timelines_career_entry")
            batch.drop_constraint(op.f("ck_setting_timelines_subject_shape"), type_="check")
            batch.create_check_constraint(op.f("ck_setting_timelines_subject_shape"), _OLD_SUBJECT_SHAPE)
        for table in ("setting_reference_handles", "setting_field_options", "setting_field_definitions"):
            op.drop_table(table)
        op.drop_column("projects", "fields_revision")
