"""章节规划、支线推进和明确交汇；不重建已有正文父表。"""

from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _id():
    return sa.Column("id", sa.Integer(), primary_key=True, nullable=False)


def _project():
    return sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)


def _index(table, column):
    op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.add_column("projects", sa.Column("planning_revision", sa.Integer(), server_default="0", nullable=False))
    op.create_table(
        "planning_units", _id(), _project(),
        sa.Column("chapter_id", sa.Integer(), sa.ForeignKey("chapters.id", ondelete="SET NULL"), unique=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("chapter_plans.id", ondelete="SET NULL"), unique=True),
        sa.Column("pov_character_id", sa.Integer(), sa.ForeignKey("characters.id", ondelete="SET NULL")),
        sa.Column("last_source_label", sa.String(300), server_default="", nullable=False),
        sa.Column("last_volume_label", sa.String(300), server_default="", nullable=False),
        sa.Column("detached_character_ids", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("binding_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retired", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("binding_epoch >= 0", name="binding_epoch_nonnegative"),
        sa.CheckConstraint("NOT retired OR (chapter_id IS NULL AND plan_id IS NULL)", name="retired_detached"),
        sqlite_autoincrement=True,
    )
    _index("planning_units", "project_id")
    _index("planning_units", "pov_character_id")
    op.create_table(
        "planning_tags", _id(), _project(),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("tone", sa.String(16), server_default="default", nullable=False),
        *_timestamps(), sa.UniqueConstraint("project_id", "name"), sqlite_autoincrement=True,
    )
    _index("planning_tags", "project_id")
    for table, column, target in (
        ("planning_unit_tags", "tag_id", "planning_tags"),
        ("planning_unit_world", "world_id", "world_settings"),
        ("planning_unit_organizations", "organization_id", "organizations"),
        ("planning_unit_careers", "career_id", "careers"),
        ("planning_unit_relationships", "timeline_id", "setting_timelines"),
    ):
        op.create_table(
            table,
            sa.Column("unit_id", sa.Integer(), sa.ForeignKey("planning_units.id", ondelete="CASCADE"),
                      primary_key=True, nullable=False),
            sa.Column(column, sa.Integer(), sa.ForeignKey(f"{target}.id", ondelete="CASCADE"),
                      primary_key=True, nullable=False),
            *_timestamps(),
        )
        _index(table, column)
    op.create_table(
        "subplots", _id(), _project(),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("tone", sa.String(16), server_default="default", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(), sqlite_autoincrement=True,
    )
    _index("subplots", "project_id")
    op.create_table(
        "subplot_nodes", _id(),
        sa.Column("subplot_id", sa.Integer(), sa.ForeignKey("subplots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("unit_id", sa.Integer(), sa.ForeignKey("planning_units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        *_timestamps(), sa.UniqueConstraint("subplot_id", "unit_id"), sqlite_autoincrement=True,
    )
    _index("subplot_nodes", "subplot_id")
    _index("subplot_nodes", "unit_id")
    op.create_table(
        "subplot_crossings", _id(), _project(),
        sa.Column("unit_id", sa.Integer(), sa.ForeignKey("planning_units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(), sqlite_autoincrement=True,
    )
    _index("subplot_crossings", "project_id")
    _index("subplot_crossings", "unit_id")
    op.create_table(
        "subplot_crossing_participants",
        sa.Column("crossing_id", sa.Integer(), sa.ForeignKey("subplot_crossings.id", ondelete="CASCADE"),
                  primary_key=True, nullable=False),
        sa.Column("subplot_id", sa.Integer(), sa.ForeignKey("subplots.id", deferrable=True, initially="DEFERRED"),
                  primary_key=True, nullable=False),
        *_timestamps(),
    )
    _index("subplot_crossing_participants", "subplot_id")


def downgrade() -> None:
    for table in ("planning_units", "planning_tags", "subplots"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("存在章节规划资料，不能无损降级；请先备份并明确处理规划数据")
    for table in (
        "subplot_crossing_participants", "subplot_crossings", "subplot_nodes", "subplots",
        "planning_unit_relationships", "planning_unit_careers", "planning_unit_organizations",
        "planning_unit_world", "planning_unit_tags", "planning_tags", "planning_units",
    ):
        op.drop_table(table)
    op.drop_column("projects", "planning_revision")
