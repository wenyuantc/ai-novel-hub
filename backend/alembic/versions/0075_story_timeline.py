"""作品固定历法和章景日期。"""

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def _target(name, table):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(f"{table}.id", ondelete="CASCADE"),
                     primary_key=True, autoincrement=False)


def upgrade():
    op.create_table(
        "story_calendars", _target("project_id", "projects"),
        sa.Column("era_name", sa.String(64), nullable=False),
        sa.Column("months", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
    )
    for table, key, parent in (
        ("chapter_story_dates", "chapter_id", "chapters"), ("scene_story_dates", "scene_id", "scenes"),
    ):
        op.create_table(
            table, _target(key, parent),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("month_id", sa.String(64), nullable=False),
            sa.Column("day", sa.Integer(), nullable=False),
            sa.CheckConstraint("year > 0 AND day > 0", name="positive_date"),
        )


def downgrade():
    if op.get_context().as_sql:
        raise RuntimeError("需要连接数据库检查故事日期")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    else:
        db.exec_driver_sql("LOCK TABLE story_calendars, chapter_story_dates, scene_story_dates IN ACCESS EXCLUSIVE MODE")
    for table in ("scene_story_dates", "chapter_story_dates", "story_calendars"):
        if db.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first():
            raise RuntimeError("存在故事历法或日期，请先备份，拒绝降级")
    for table in ("scene_story_dates", "chapter_story_dates", "story_calendars"):
        op.drop_table(table)
