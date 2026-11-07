"""系列、作品成员与共享设定的单书绑定（不重建父表）。"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_PARENT_INDEXES = (
    ("projects", "uq_projects_id_user_id", ("id", "user_id")),
    ("characters", "uq_characters_project_id_id", ("project_id", "id")),
    ("world_settings", "uq_world_settings_project_id_id", ("project_id", "id")),
)


def _timestamps():
    return tuple(
        sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
        for name in ("created_at", "updated_at")
    )


def upgrade() -> None:
    for table, name, columns in _PARENT_INDEXES:
        op.create_index(name, table, list(columns), unique=True)
    op.create_table(
        "series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("id", "owner_id"),
        sa.CheckConstraint("revision >= 0", name="revision"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_series_owner_id", "series", ["owner_id"])
    op.create_table(
        "series_books",
        sa.Column("project_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("project_id", "series_id"),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"], ["projects.id", "projects.user_id"],
            name="fk_series_books_project_owner", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["series_id", "owner_id"], ["series.id", "series.owner_id"],
            name="fk_series_books_series_owner", ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_series_books_series_id", "series_books", ["series_id"])
    op.create_table(
        "series_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("series.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("ai_settings", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("series_id", "id", "kind"),
        sa.CheckConstraint("kind IN ('character', 'world')", name="kind"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_series_entries_series_id_kind_name", "series_entries", ["series_id", "kind", "display_name"])
    op.create_table(
        "series_entry_uses",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("series_entry_id", sa.Integer(), primary_key=True),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=True),
        sa.Column("world_setting_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "(kind = 'character' AND character_id IS NOT NULL AND world_setting_id IS NULL) OR "
            "(kind = 'world' AND character_id IS NULL AND world_setting_id IS NOT NULL)",
            name="local_entity_kind",
        ),
        sa.UniqueConstraint("project_id", "character_id", name="uq_series_entry_uses_local_character"),
        sa.UniqueConstraint("project_id", "world_setting_id", name="uq_series_entry_uses_local_world"),
        sa.ForeignKeyConstraint(
            ["project_id", "series_id"], ["series_books.project_id", "series_books.series_id"],
            name="fk_series_entry_uses_book", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["series_id", "series_entry_id", "kind"],
            ["series_entries.series_id", "series_entries.id", "series_entries.kind"],
            name="fk_series_entry_uses_entry", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "character_id"], ["characters.project_id", "characters.id"],
            name="fk_series_entry_uses_character", deferrable=True, initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "world_setting_id"], ["world_settings.project_id", "world_settings.id"],
            name="fk_series_entry_uses_world", deferrable=True, initially="DEFERRED",
        ),
    )
    op.create_index("ix_series_entry_uses_series_entry_id", "series_entry_uses", ["series_entry_id"])


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM series LIMIT 1").first() is not None:
        raise RuntimeError("存在系列数据，不能无损降级；请保留整库备份并先安全处理所有系列")
    for table in ("series_entry_uses", "series_entries", "series_books", "series"):
        op.drop_table(table)
    for table, name, _ in reversed(_PARENT_INDEXES):
        op.drop_index(name, table_name=table)
