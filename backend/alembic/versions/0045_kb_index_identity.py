"""索引占用与任务领取轮次；原位扩展有外键引用的父表。

Revision ID: 0045
Revises: 0044
"""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_TASK_CHECK = "execution_key IS NULL OR length(execution_key) = 36"
_DOC_CHECK = (
    "(index_task_key IS NULL AND index_task_queued_at IS NULL AND index_claim_key IS NULL) OR "
    "(index_task_key IS NOT NULL AND index_task_queued_at IS NOT NULL AND index_claim_key IS NOT NULL "
    "AND length(index_task_key) = 36 AND length(index_claim_key) = 36)"
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite 原位列约束避免重建父表导致任务、知识分块的外键级联。
        op.execute(
            f"ALTER TABLE tasks ADD COLUMN execution_key VARCHAR(36) "
            f"CONSTRAINT ck_tasks_execution_key_length CHECK ({_TASK_CHECK})"
        )
        op.add_column("kb_documents", sa.Column("index_task_key", sa.String(36), nullable=True))
        op.add_column("kb_documents", sa.Column("index_task_queued_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(
            f"ALTER TABLE kb_documents ADD COLUMN index_claim_key VARCHAR(36) "
            f"CONSTRAINT ck_kb_documents_index_claim_shape CHECK ({_DOC_CHECK})"
        )
    else:
        op.add_column("tasks", sa.Column("execution_key", sa.String(36), nullable=True))
        op.create_check_constraint(op.f("ck_tasks_execution_key_length"), "tasks", _TASK_CHECK)
        op.add_column("kb_documents", sa.Column("index_task_key", sa.String(36), nullable=True))
        op.add_column("kb_documents", sa.Column("index_task_queued_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("kb_documents", sa.Column("index_claim_key", sa.String(36), nullable=True))
        op.create_check_constraint(op.f("ck_kb_documents_index_claim_shape"), "kb_documents", _DOC_CHECK)


def downgrade() -> None:
    db = op.get_bind()
    if db.execute(sa.text(
        "SELECT 1 FROM kb_documents WHERE index_task_key IS NOT NULL OR index_task_queued_at IS NOT NULL "
        "OR index_claim_key IS NOT NULL LIMIT 1"
    )).first() is not None or db.execute(sa.text(
        "SELECT 1 FROM tasks WHERE execution_key IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有索引占用或执行轮次，拒绝有损降级；请先安全排空任务")
    if db.dialect.name != "sqlite":
        op.drop_constraint(op.f("ck_kb_documents_index_claim_shape"), "kb_documents", type_="check")
        op.drop_constraint(op.f("ck_tasks_execution_key_length"), "tasks", type_="check")
    op.drop_column("kb_documents", "index_claim_key")
    op.drop_column("kb_documents", "index_task_queued_at")
    op.drop_column("kb_documents", "index_task_key")
    op.drop_column("tasks", "execution_key")
