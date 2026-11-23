"""保留跨进程取消、请求工作和删除意图的实际归属。"""

from contextlib import ExitStack, contextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


@contextmanager
def _offline(*, downgrade: bool):
    db = op.get_bind()
    with ExitStack() as guards:
        if db.dialect.name == "sqlite":
            # 先取得主库写锁；DDL、证据检查、旧意图清理及版本切换共用一次提交。
            op.execute("UPDATE alembic_version SET version_num=version_num")
            path = next(p for _, name, p in db.exec_driver_sql("PRAGMA database_list") if name == "main")
            if db.exec_driver_sql("SELECT 1 FROM task_lease_store LIMIT 1").first():
                from app.services.task_engine import sqlite_lease_pair

                if path:
                    guards.enter_context(sqlite_lease_pair.guard_downgrade(Path(path).resolve(strict=True)))
                else:
                    guards.enter_context(sqlite_lease_pair.guard_memory_migration(db))
        elif db.dialect.name == "postgresql":
            extra = ", task_work_records" if downgrade else ""
            op.execute("LOCK TABLE task_workers, task_execution_slots, projects, tasks, agent_changes"
                       + extra + " IN ACCESS EXCLUSIVE MODE")
        else:
            raise RuntimeError("跨进程取消迁移不支持当前数据库")
        if db.execute(sa.text(
            "SELECT 1 FROM task_workers WHERE stopped_at IS NULL AND fenced_at IS NULL LIMIT 1"
        )).first():
            raise RuntimeError("仍有未停止的执行者，拒绝迁移")
        yield db


def upgrade() -> None:
    with _offline(downgrade=False) as db:
        # 已停止/围栏的旧执行仍可按 C1 接管。先证明留下的槽确实属于同一任务，
        # 否则无法区分无作品任务与丢失作品身份的工作，需由 0067 的进程恢复、排空。
        if db.execute(sa.text(
            "SELECT 1 FROM task_execution_slots s WHERE NOT EXISTS ("
            "SELECT 1 FROM tasks t JOIN task_workers w ON w.worker_key=t.worker_key "
            "WHERE t.id=s.task_id AND t.statistics_key=s.statistics_key AND t.queued_at=s.queued_at "
            "AND t.execution_key=s.execution_key AND t.worker_key=s.worker_key AND t.user_id=s.user_id "
            "AND (t.serial_key=s.serial_key OR (t.serial_key IS NULL AND s.serial_key IS NULL)) "
            "AND (w.stopped_at IS NOT NULL OR w.fenced_at IS NOT NULL)) LIMIT 1"
        )).first():
            raise RuntimeError("旧任务占用的轮次或归属无法确认，请先用 0067 恢复并排空")
        op.add_column("tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
        op.add_column("projects", sa.Column("deletion_worker_key", sa.String(36)))
        op.add_column("agent_changes", sa.Column("external_worker_key", sa.String(36)))
        op.create_table(
            "task_work_records",
            sa.Column("work_key", sa.String(36), primary_key=True),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("project_id", sa.Integer()),
            sa.Column("worker_key", sa.String(36)),
            sa.Column("task_id", sa.Integer()),
            sa.Column("task_key", sa.String(36)),
            sa.Column("queued_at", sa.DateTime(timezone=True)),
            sa.Column("execution_key", sa.String(36)),
            sa.Column("operation_id", sa.String(36)),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("evidence", sa.JSON(none_as_null=True)),
        )
        for column in ("project_id", "worker_key"):
            op.create_index(f"ix_task_work_records_{column}", "task_work_records", [column])
        # 0067 不记录发起删除的请求身份。旧服务全部下线后一次性解冻，不完成软删。
        # 旧外部调用仍需按原签名、原轮次和时间阈值核对，迁移不改业务凭据。
        op.execute("UPDATE projects SET deletion_token=NULL, deletion_started_at=NULL "
                   "WHERE deletion_token IS NOT NULL AND deleted_at IS NULL")


def _unsettled_external(dialect: str) -> str:
    # 冻结 0068 的收据语义。JSON null 或损坏的标记也不能作为远端已结束的证明。
    fields = ("_external_claim_id", "_external_task_key", "_external_execution",
              "_external_snapshot_mac", "_external_outcome_unknown")
    if dialect == "sqlite":
        markers = [f"json_type(result, '$.{key}') IS NOT NULL" for key in fields]
        error_type, text_type, string = "json_type(result, '$.error')", "json_type(result, '$.text')", "text"
    else:
        markers = [f"json_typeof(result -> '{key}') IS NOT NULL" for key in fields]
        error_type, text_type, string = "json_typeof(result -> 'error')", "json_typeof(result -> 'text')", "string"
    return " OR ".join([
        "external_worker_key IS NOT NULL",
        "status NOT IN ('proposed','rejected','executed','failed','undone')",
        *markers,
        f"(kind='external' AND status='failed' AND coalesce({error_type}, '') <> '{string}')",
        f"(kind='external' AND status='executed' AND coalesce({text_type}, '') <> '{string}')",
    ])


def downgrade() -> None:
    with _offline(downgrade=True) as db:
        checks = (
            ("task_execution_slots", "1=1"),
            ("task_work_records", "1=1"),
            ("tasks", "cancel_requested_at IS NOT NULL OR status='running' OR execution_key IS NOT NULL "
             "OR admin_cancel_operation_id IS NOT NULL"),
            ("projects", "deletion_token IS NOT NULL OR deletion_worker_key IS NOT NULL"),
            ("agent_changes", _unsettled_external(db.dialect.name)),
        )
        for table, condition in checks:
            if db.execute(sa.text(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1")).first():
                raise RuntimeError(f"{table} 尚有工作或待核对的归属，拒绝降级")
        op.drop_table("task_work_records")
        op.drop_column("agent_changes", "external_worker_key")
        op.drop_column("projects", "deletion_worker_key")
        op.drop_column("tasks", "cancel_requested_at")
