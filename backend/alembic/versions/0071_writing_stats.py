"""按人和作品记录写作统计与码字目标。"""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def _personal_key():
    return (
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("UPDATE alembic_version SET version_num=version_num")
    op.add_column("projects", sa.Column("writing_stats_started_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "writing_daily_stats",
        *_personal_key(),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("manual_words", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ai_words", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("manual_words >= 0 AND ai_words >= 0", name="nonnegative_words"),
    )
    op.create_table(
        "writing_goals",
        *_personal_key(),
        sa.Column("period", sa.String(4)),
        sa.Column("target_words", sa.BigInteger()),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint("revision > 0", name="revision"),
        sa.CheckConstraint(
            "(period IS NULL AND target_words IS NULL) OR "
            "(period IS NOT NULL AND period IN ('day', 'week') AND target_words IS NOT NULL AND target_words > 0)",
            name="goal_shape",
        ),
    )
    op.create_table(
        "writing_stats_tracking",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="singleton"),
    )
    op.execute("INSERT INTO writing_stats_tracking (id) VALUES (1)")


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("降级前需要连接数据库检查个人写作数据")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    elif db.dialect.name == "postgresql":
        db.exec_driver_sql("LOCK TABLE writing_daily_stats, writing_goals IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("写作统计降级不支持当前数据库")
    for table in ("writing_daily_stats", "writing_goals"):
        if db.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first():
            raise RuntimeError("存在个人写作数据，请先备份，拒绝降级")
    for table in ("writing_goals", "writing_daily_stats", "writing_stats_tracking"):
        op.drop_table(table)
    op.drop_column("projects", "writing_stats_started_at")
