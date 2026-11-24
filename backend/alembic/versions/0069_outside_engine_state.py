"""持久化备份归属、灵感与智能体操作占用、项目导入预览。"""

from contextlib import ExitStack, contextmanager
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None

_CLAIM_TABLES = ("inspiration_generation_claims", "agent_conversation_operations")
_PREVIEW_TABLES = ("project_import_preview_quota", "project_import_previews", "project_import_preview_chunks")
_CONVERSATION_KEY_INDEX = "uq_agent_conversations_instance_key"


@contextmanager
def _offline(*, downgrade: bool):
    db = op.get_bind()
    with ExitStack() as guards:
        if db.dialect.name == "sqlite":
            # 显式写事务把 SQLite DDL、归属核对和 Alembic 版本切换绑定在一起。
            op.execute("UPDATE alembic_version SET version_num=version_num")
            path = next(p for _, name, p in db.exec_driver_sql("PRAGMA database_list") if name == "main")
            if db.exec_driver_sql("SELECT 1 FROM task_lease_store LIMIT 1").first():
                from app.services.task_engine import sqlite_lease_pair

                if path:
                    guards.enter_context(sqlite_lease_pair.guard_downgrade(Path(path).resolve(strict=True)))
                else:
                    guards.enter_context(sqlite_lease_pair.guard_memory_migration(db))
        elif db.dialect.name == "postgresql":
            tables = ("task_workers", "backup_runs", "backup_artifacts", "agent_conversations", "inspiration_sessions")
            if downgrade:
                tables += (*_CLAIM_TABLES, *_PREVIEW_TABLES)
            op.execute("LOCK TABLE " + ", ".join(tables) + " IN ACCESS EXCLUSIVE MODE")
        else:
            raise RuntimeError("引擎外状态迁移不支持当前数据库")
        if db.exec_driver_sql(
            "SELECT 1 FROM task_workers WHERE stopped_at IS NULL AND fenced_at IS NULL LIMIT 1",
        ).first():
            raise RuntimeError("仍有未停止的执行者，拒绝迁移")
        yield db


def _create_operation_tables() -> None:
    op.create_table(
        "inspiration_generation_claims",
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("inspiration_sessions.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("step", sa.String(32), primary_key=True),
        sa.Column("operation_key", sa.String(36), nullable=False),
        sa.Column("worker_key", sa.String(36), nullable=False),
    )
    op.create_table(
        "agent_conversation_operations",
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("operation_key", sa.String(36), nullable=False),
        sa.Column("worker_key", sa.String(36), nullable=False),
    )
    for table in _CLAIM_TABLES:
        op.create_index(f"ix_{table}_worker_key", table, ["worker_key"])


def _add_conversation_keys(db) -> None:
    sqlite = db.dialect.name == "sqlite"
    # SQLite 按原表加列而不重建，避免触发既有消息和提案的级联。空默认值仅用于迁移回填。
    op.add_column("agent_conversations", sa.Column(
        "instance_key", sa.String(36), nullable=not sqlite, server_default="" if sqlite else None,
    ))
    last_id = None
    while True:
        after = " WHERE id > :last_id" if last_id is not None else ""
        rows = db.execute(sa.text(
            f"SELECT id FROM agent_conversations{after} ORDER BY id LIMIT 500",
        ), {"last_id": last_id} if after else {}).scalars().all()
        if not rows:
            break
        db.execute(sa.text("UPDATE agent_conversations SET instance_key=:key WHERE id=:id"), [
            {"id": row_id, "key": str(uuid4())} for row_id in rows
        ])
        last_id = rows[-1]
    if sqlite:
        op.execute(
            "CREATE TRIGGER agent_conversations_instance_key_insert BEFORE INSERT ON agent_conversations "
            "WHEN length(NEW.instance_key) <> 36 BEGIN SELECT RAISE(ABORT, 'agent conversation identity required'); END"
        )
        op.execute(
            "CREATE TRIGGER agent_conversations_instance_key_update "
            "BEFORE UPDATE OF instance_key ON agent_conversations "
            "WHEN NEW.instance_key IS NOT OLD.instance_key "
            "BEGIN SELECT RAISE(ABORT, 'agent conversation identity is immutable'); END"
        )
    else:
        op.alter_column("agent_conversations", "instance_key", existing_type=sa.String(36), nullable=False)
    op.create_index(_CONVERSATION_KEY_INDEX, "agent_conversations", ["instance_key"], unique=True)


def _create_preview_tables() -> None:
    op.create_table(
        "project_import_preview_quota",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name="singleton"),
        sa.CheckConstraint("revision >= 1", name="positive_revision"),
    )
    op.create_table(
        "project_import_previews",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="positive_revision"),
        sa.CheckConstraint("size_bytes BETWEEN 1 AND 104857600", name="size_limit"),
    )
    op.create_index("ix_project_import_previews_expiry", "project_import_previews", ["expires_at", "revision"])
    op.create_index("ix_project_import_previews_user_expiry", "project_import_previews",
                    ["user_id", "expires_at", "revision"])
    op.create_table(
        "project_import_preview_chunks",
        sa.Column("token_hash", sa.String(64), sa.ForeignKey("project_import_previews.token_hash", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("chunk_index", sa.Integer(), primary_key=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="nonnegative_index"),
    )


def upgrade() -> None:
    with _offline(downgrade=False) as db:
        op.add_column("backup_runs", sa.Column("worker_key", sa.String(36)))
        op.add_column("backup_runs", sa.Column("recovery_worker_key", sa.String(36)))
        op.add_column("backup_runs", sa.Column("execution_host", sa.String(255)))
        op.add_column("backup_artifacts", sa.Column("writer_host", sa.String(255)))
        op.add_column("inspiration_sessions", sa.Column("state_revision", sa.Integer(), nullable=False,
                                                       server_default=sa.text("0")))
        _add_conversation_keys(db)
        _create_operation_tables()
        _create_preview_tables()


def downgrade() -> None:
    with _offline(downgrade=True) as db:
        checks = (
            ("backup_runs", "worker_key IS NOT NULL OR recovery_worker_key IS NOT NULL"),
            ("backup_artifacts", "writer_host IS NOT NULL AND status IN ('deleting','unknown')"),
            *[(table, "1=1") for table in _CLAIM_TABLES],
            ("project_import_previews", "expires_at > CURRENT_TIMESTAMP"),
        )
        for table, condition in checks:
            if db.exec_driver_sql(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1").first():
                raise RuntimeError(f"{table} 尚有活动操作或待核对的归属，拒绝降级")
        for table in reversed((*_CLAIM_TABLES, *_PREVIEW_TABLES)):
            op.drop_table(table)
        if db.dialect.name == "sqlite":
            for action in ("update", "insert"):
                op.execute(f"DROP TRIGGER agent_conversations_instance_key_{action}")
        op.drop_index(_CONVERSATION_KEY_INDEX, table_name="agent_conversations")
        op.drop_column("agent_conversations", "instance_key")
        op.drop_column("inspiration_sessions", "state_revision")
        op.drop_column("backup_artifacts", "writer_host")
        for column in ("execution_host", "recovery_worker_key", "worker_key"):
            op.drop_column("backup_runs", column)
