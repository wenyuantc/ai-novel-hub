"""记录任务执行者的租约与恢复归属。"""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # sqlite3 的旧事务模式不会为 DDL 自动 BEGIN。让建表、加列与版本号一起提交。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    op.create_table(
        "task_lease_store",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("generation", sa.String(36), nullable=False),
        sa.Column("main_path", sa.Text(), nullable=False),
        sa.Column("main_file", sa.String(64), nullable=False),
        sa.Column("lock_file", sa.String(64), nullable=False),
        sa.Column("lease_file", sa.String(64)),
        sa.CheckConstraint("id = 1", name="singleton"),
    )
    op.create_table(
        "task_workers",
        sa.Column("worker_key", sa.String(36), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("fenced_at", sa.DateTime(timezone=True)),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("peer_url", sa.String(500)),
    )
    # 原位加列，保留既有任务和引用它的记录。
    op.add_column("tasks", sa.Column("worker_key", sa.String(36)))
    op.add_column("tasks", sa.Column("recovered_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE task_workers, tasks IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("任务执行者租约降级不支持当前数据库")
    if db.execute(sa.text(
        "SELECT 1 FROM task_workers WHERE stopped_at IS NULL AND fenced_at IS NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有未停止的执行者登记，拒绝降级；请先停止所有任务引擎")
    if db.execute(sa.text(
        "SELECT 1 FROM tasks WHERE status='running' AND worker_key IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有带执行者身份的运行中任务，拒绝降级；请先完成接管并正常停止引擎")
    if db.dialect.name == "sqlite":
        from app.services.task_engine.sqlite_lease_pair import guard_downgrade

        database = next(path for _, name, path in db.exec_driver_sql("PRAGMA database_list") if name == "main")
        if database:
            guards = guard_downgrade(Path(database).resolve(strict=True))
            # 此 Alembic 环境每次迁移使用专用 NullPool 引擎和单连接。仅在版本变更实际提交或回滚后放锁。
            sa.event.listen(db.engine.pool, "checkin", lambda *_args: guards.close(), once=True)
    op.drop_column("tasks", "recovered_at")
    op.drop_column("tasks", "worker_key")
    op.drop_table("task_workers")
    op.drop_table("task_lease_store")
