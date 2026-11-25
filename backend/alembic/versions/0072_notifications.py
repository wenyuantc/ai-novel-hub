"""新增站内收件记录和个人偏好；既有邀请、任务不回填。"""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def _fk(name, table, **kw):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(f"{table}.id", ondelete="CASCADE"), **kw)


def _kind():
    return sa.CheckConstraint("kind IN ('invitation','task','budget','consistency')", name="kind")


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        _fk("user_id", "users", primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _kind(),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False, unique=True),
        _fk("recipient_id", "users", nullable=False),
        sa.Column("event_key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("variant", sa.String(16)),
        _fk("project_id", "projects"),
        _fk("series_id", "series"),
        sa.Column("grant_id", sa.String(36)),
        _fk("task_id", "tasks"),
        sa.Column("task_key", sa.String(36)),
        _fk("run_id", "consistency_runs"),
        _fk("budget_id", "ai_usage_budgets"),
        sa.Column("budget_key", sa.String(36)),
        sa.Column("currency", sa.String(3)),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        _kind(),
        sa.UniqueConstraint("recipient_id", "event_key"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_notifications_recipient_time_id", "notifications", ["recipient_id", "created_at", "id"])


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("需要连接数据库检查通知记录")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    elif db.dialect.name == "postgresql":
        db.exec_driver_sql("LOCK TABLE notification_preferences, notifications IN ACCESS EXCLUSIVE MODE")
    for table in ("notifications", "notification_preferences"):
        if db.exec_driver_sql(f"SELECT 1 FROM {table} LIMIT 1").first():
            raise RuntimeError("存在个人通知数据，请先备份，拒绝降级")
    op.drop_table("notifications")
    op.drop_table("notification_preferences")
