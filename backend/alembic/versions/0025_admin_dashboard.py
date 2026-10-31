"""Independent administrator statistics.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_dashboard_tracking",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_admin_dashboard_tracking_singleton")),
    )
    op.create_table(
        "admin_dashboard_daily",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        *(sa.Column(name, sa.BigInteger(), nullable=False) for name in (
            "users", "projects", "chapters", "total_words", "pending_submissions"
        )),
        *(sa.CheckConstraint(f"{name} >= 0", name=op.f(f"ck_admin_dashboard_daily_{name}_nonnegative")) for name in (
            "users", "projects", "chapters", "total_words", "pending_submissions"
        )),
    )
    op.create_table(
        "admin_task_statistics",
        sa.Column("key", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("finished_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("origin IN ('baseline', 'new')", name=op.f("ck_admin_task_statistics_origin")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled', 'interrupted')",
            name=op.f("ck_admin_task_statistics_status"),
        ),
    )
    op.create_index("ix_admin_task_statistics_created_event_at", "admin_task_statistics", ["created_event_at"])
    op.create_index(
        "ix_admin_task_statistics_status_finished_event_at", "admin_task_statistics", ["status", "finished_event_at"]
    )
    # ADD COLUMN / CREATE INDEX 都不重建 tasks，避免触发既有子表的级联。
    op.add_column("tasks", sa.Column("statistics_key", sa.String(36), nullable=True))
    op.create_index("ix_tasks_statistics_key", "tasks", ["statistics_key"], unique=True)


def downgrade():
    bind = op.get_bind()
    for table in ("admin_dashboard_daily", "admin_task_statistics"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("存在已记录的管理员统计，拒绝丢失历史的降级；请先显式备份并处理统计数据")
    op.drop_index("ix_tasks_statistics_key", table_name="tasks")
    op.drop_column("tasks", "statistics_key")
    op.drop_table("admin_task_statistics")
    op.drop_table("admin_dashboard_daily")
    op.drop_table("admin_dashboard_tracking")
