"""保存正常领取的执行轮次占用，保证插件 ID 在网络操作期间稳定。"""

from contextlib import ExitStack
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # 让建表、索引和版本号在同一个真实事务内生效。
        op.execute("UPDATE alembic_version SET version_num=version_num")
        # 网络操作要在事务外等待。删除插件后，旧请求不能写进新建的同 ID 插件。
        with op.batch_alter_table("mcp_plugins", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
            pass
    op.create_table(
        "task_execution_slots",
        sa.Column("execution_key", sa.String(36), primary_key=True),
        sa.Column("worker_key", sa.String(36), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("statistics_key", sa.String(36), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("serial_key", sa.String(100)),
    )
    for column in ("worker_key", "user_id", "serial_key"):
        op.create_index(f"ix_task_execution_slots_{column}", "task_execution_slots", [column])


def downgrade() -> None:
    db = op.get_bind()
    with ExitStack() as guards:
        if db.dialect.name == "sqlite":
            op.execute("UPDATE alembic_version SET version_num=version_num")
            from app.services.task_engine.sqlite_lease_pair import guard_downgrade

            path = next(path for _, name, path in db.exec_driver_sql("PRAGMA database_list") if name == "main")
            if path:
                # 生命周期锁覆盖核对和删表，主库写事务继续串行化登记直到版本变更提交。
                guards.enter_context(guard_downgrade(Path(path).resolve(strict=True)))
        elif db.dialect.name == "postgresql":
            op.execute("LOCK TABLE task_workers, task_execution_slots IN ACCESS EXCLUSIVE MODE")
        else:
            raise RuntimeError("任务调度名额降级不支持当前数据库")
        if db.execute(sa.text(
            "SELECT 1 FROM task_workers WHERE stopped_at IS NULL AND fenced_at IS NULL LIMIT 1"
        )).first():
            raise RuntimeError("仍有未停止的执行者，拒绝降级")
        if db.execute(sa.text(
            "SELECT 1 FROM task_execution_slots s WHERE NOT EXISTS ("
            "SELECT 1 FROM task_workers w WHERE w.worker_key=s.worker_key "
            "AND (w.stopped_at IS NOT NULL OR w.fenced_at IS NOT NULL)) LIMIT 1"
        )).first():
            raise RuntimeError("任务占用的归属尚未确认，拒绝降级")
        op.drop_table("task_execution_slots")
        if db.dialect.name == "sqlite":
            with op.batch_alter_table("mcp_plugins", recreate="always", table_kwargs={"sqlite_autoincrement": False}):
                pass
