"""设定 AI 可见性、提及配置、有向关联和持久发送代次。"""

from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_ENTRIES = ("characters", "world_settings", "organizations", "careers")
_REBUILT = (*_ENTRIES, "setting_text_fields")
_MODE_CHECK = "ai_mode IN ('always', 'detected', 'manual_only', 'never')"
_LEGACY_CHECK = "definition_id IS NULL OR NOT ai_hidden"
_HANDLE_INDEX = "uq_setting_reference_handles_project_id_id"


@contextmanager
def _sqlite_rebuild():
    """SQLite 父表重建不能触发级联，且须保留包括空表在内的 ID 高水位。"""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return
    if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise RuntimeError("旧资料存在无效外键，请先修复后重试 AI 可见性迁移")
    sequence = sa.table("sqlite_sequence", sa.column("name"), sa.column("seq"))
    high_water = bind.execute(sa.select(sequence).where(sequence.c.name.in_(_REBUILT))).all()
    enabled = bool(bind.exec_driver_sql("PRAGMA foreign_keys").scalar())
    try:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if bind.exec_driver_sql("PRAGMA foreign_keys").scalar():
            raise RuntimeError("不能安全关闭迁移连接的外键校验")
        yield
        for name, previous in high_water:
            condition = sequence.c.name == name
            current = bind.scalar(sa.select(sequence.c.seq).where(condition))
            if current is None:
                bind.execute(sequence.insert().values(name=name, seq=previous))
            elif current < previous:
                bind.execute(sequence.update().where(condition).values(seq=previous))
        if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("AI 可见性迁移后外键校验失败")
    finally:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _batch(table):
    options = (
        {"recreate": "always", "table_kwargs": {"sqlite_autoincrement": True}}
        if op.get_bind().dialect.name == "sqlite"
        else {}
    )
    return op.batch_alter_table(table, **options)


def _upgrade():
    for table in _ENTRIES:
        # 先保存旧资料的兼容 NULL，再给未来的插入设默认。
        op.add_column(table, sa.Column("ai_mode", sa.String(16), nullable=True))
        with _batch(table) as batch:
            batch.alter_column("ai_mode", existing_type=sa.String(16), server_default="detected")
            batch.create_check_constraint(op.f(f"ck_{table}_ai_mode"), _MODE_CHECK)
            batch.add_column(sa.Column("mention_enabled", sa.Boolean(), nullable=True))
            batch.add_column(sa.Column(
                "mention_case_sensitive", sa.Boolean(), server_default=sa.true(), nullable=False,
            ))
            batch.add_column(sa.Column("mention_exclusions", sa.JSON(), server_default="[]", nullable=False))
            batch.add_column(sa.Column("ai_hidden_builtin_fields", sa.JSON(), server_default="[]", nullable=False))

    for table in ("setting_field_definitions", "setting_text_fields"):
        op.add_column(table, sa.Column("ai_hidden", sa.Boolean(), server_default=sa.false(), nullable=False))
    with _batch("setting_text_fields") as batch:
        batch.create_check_constraint(op.f("ck_setting_text_fields_legacy_ai_hidden"), _LEGACY_CHECK)

    for table, column in (
        ("projects", "ai_visibility_revision"),
        ("projects", "ai_history_epoch"),
        ("agent_conversations", "ai_history_epoch"),
    ):
        op.add_column(table, sa.Column(column, sa.Integer(), server_default="0", nullable=False))

    op.create_index(_HANDLE_INDEX, "setting_reference_handles", ["project_id", "id"], unique=True)
    op.create_table(
        "setting_ai_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_handle_id", sa.Integer(), nullable=False),
        sa.Column("target_handle_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "source_handle_id", "target_handle_id"),
        sa.CheckConstraint("source_handle_id <> target_handle_id", name="distinct_handles"),
        sa.ForeignKeyConstraint(
            ["project_id", "source_handle_id"],
            ["setting_reference_handles.project_id", "setting_reference_handles.id"],
            name="fk_setting_ai_links_source_handle", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "target_handle_id"],
            ["setting_reference_handles.project_id", "setting_reference_handles.id"],
            name="fk_setting_ai_links_target_handle", ondelete="CASCADE",
        ),
        sqlite_autoincrement=True,
    )
    for column in ("project_id", "source_handle_id", "target_handle_id"):
        op.create_index(f"ix_setting_ai_links_{column}", "setting_ai_links", [column])


def upgrade() -> None:
    with _sqlite_rebuild():
        _upgrade()


def _check_downgrade() -> None:
    bind = op.get_bind()
    checks = (
        "SELECT 1 FROM setting_ai_links LIMIT 1",
        "SELECT 1 FROM setting_field_definitions WHERE ai_hidden LIMIT 1",
        "SELECT 1 FROM setting_text_fields WHERE ai_hidden LIMIT 1",
        "SELECT 1 FROM projects WHERE ai_visibility_revision <> 0 OR ai_history_epoch <> 0 LIMIT 1",
        "SELECT 1 FROM agent_conversations WHERE ai_history_epoch <> 0 LIMIT 1",
    )
    message = "存在 AI 可见性配置或重发代次，不能无损降级；请保留资料并停止旧 AI 执行器"
    if any(bind.exec_driver_sql(stmt).first() is not None for stmt in checks):
        raise RuntimeError(message)
    for name in _ENTRIES:
        table = sa.table(
            name, sa.column("ai_mode"), sa.column("mention_enabled"), sa.column("mention_case_sensitive"),
            sa.column("mention_exclusions", sa.JSON()), sa.column("ai_hidden_builtin_fields", sa.JSON()),
        )
        for mode, enabled, sensitive, exclusions, hidden in bind.execute(sa.select(table)):
            if mode is not None or enabled is not None or not sensitive or exclusions != [] or hidden != []:
                raise RuntimeError(message)


def downgrade() -> None:
    _check_downgrade()
    with _sqlite_rebuild():
        op.drop_table("setting_ai_links")
        op.drop_index(_HANDLE_INDEX, table_name="setting_reference_handles")
        op.drop_column("agent_conversations", "ai_history_epoch")
        op.drop_column("projects", "ai_history_epoch")
        op.drop_column("projects", "ai_visibility_revision")
        with _batch("setting_text_fields") as batch:
            batch.drop_constraint(op.f("ck_setting_text_fields_legacy_ai_hidden"), type_="check")
            batch.drop_column("ai_hidden")
        op.drop_column("setting_field_definitions", "ai_hidden")
        for table in _ENTRIES:
            with _batch(table) as batch:
                batch.drop_constraint(op.f(f"ck_{table}_ai_mode"), type_="check")
                for column in (
                    "ai_mode", "mention_enabled", "mention_case_sensitive", "mention_exclusions",
                    "ai_hidden_builtin_fields",
                ):
                    batch.drop_column(column)
