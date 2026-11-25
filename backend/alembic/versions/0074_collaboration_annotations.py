"""建立独立的协作批注与留言。"""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def _parent(name: str, table: str):
    return sa.Column(name, sa.Integer(), sa.ForeignKey(f"{table}.id", ondelete="CASCADE"), nullable=False)


def _created_at():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "annotation_threads",
        sa.Column("id", sa.Integer(), primary_key=True),
        _parent("project_id", "projects"), _parent("chapter_id", "chapters"), _parent("scene_id", "scenes"),
        sa.Column("original_start", sa.Integer(), nullable=False),
        sa.Column("original_end", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.Column("scene_hash", sa.String(64), nullable=False),
        sa.Column("anchor_valid", sa.Boolean(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        _created_at(),
        sa.CheckConstraint("original_start >= 0 AND original_end > original_start", name="original_range"),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="current_range"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_annotation_threads_scene_id", "annotation_threads", ["scene_id"])
    op.create_index("ix_annotation_threads_chapter", "annotation_threads", ["project_id", "chapter_id", "id"])
    op.create_table(
        "annotation_messages",
        sa.Column("id", sa.Integer(), primary_key=True), _parent("thread_id", "annotation_threads"),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("author_name", sa.String(64), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.String(36)), _created_at(),
        sa.UniqueConstraint("author_id", "operation_id", name="uq_annotation_messages_author_operation"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_annotation_messages_thread", "annotation_messages", ["thread_id", "id"])


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("需要连接数据库检查批注记录")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    else:
        db.exec_driver_sql("LOCK TABLE annotation_threads, annotation_messages IN ACCESS EXCLUSIVE MODE")
    if db.exec_driver_sql("SELECT 1 FROM annotation_threads LIMIT 1").first():
        raise RuntimeError("存在协作批注，请先备份，拒绝降级")
    op.drop_table("annotation_messages")
    op.drop_table("annotation_threads")
