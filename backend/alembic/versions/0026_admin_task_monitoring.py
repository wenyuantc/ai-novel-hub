"""管理员任务监控及持久取消受理。

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade():
    # ADD COLUMN 不重建 tasks 父表，不触发日志/智能体外键的级联副作用。
    op.add_column("tasks", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("admin_cancel_operation_id", sa.String(36), nullable=True))
    op.add_column("tasks", sa.Column("admin_cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_admin_cancel_operation_id", "tasks", ["admin_cancel_operation_id"])


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM tasks WHERE admin_cancel_operation_id IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有尚未收尾的管理员取消请求，拒绝降级；请先安全处理并保留审计")
    op.drop_index("ix_tasks_admin_cancel_operation_id", table_name="tasks")
    op.drop_column("tasks", "admin_cancel_requested_at")
    op.drop_column("tasks", "admin_cancel_operation_id")
    op.drop_column("tasks", "queued_at")
