"""分析派生索引的持久去重收据。

Revision ID: 0051
Revises: 0050
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def _uuid_check(column: str, *, version4: bool = False) -> str:
    hexadecimal = f"replace({column}, '-', '')"
    remainder = hexadecimal
    for char in "0123456789abcdef":
        remainder = f"replace({remainder}, '{char}', '')"
    version = (
        f"AND substr({column}, 15, 1) = '4' AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b') "
        if version4 else ""
    )
    return (
        f"CAST({column} AS TEXT) = {column} AND length({column}) = 36 "
        f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
        f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
        f"{version}AND length({hexadecimal}) = 32 AND {remainder} = ''"
    )


def _hash_check(column: str) -> str:
    remainder = column
    for char in "0123456789abcdef":
        remainder = f"replace({remainder}, '{char}', '')"
    return f"length({column}) = 64 AND {remainder} = ''"


def _int_check(column: str, *, allow_zero: bool = False) -> str:
    minimum = 0 if allow_zero else 1
    return f"CAST({column} AS INTEGER) = {column} AND {column} BETWEEN {minimum} AND 2147483647"


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError("分析索引收据不支持当前数据库")
    memory_check = ("json_valid(memory_refs) AND json_type(memory_refs) = 'array'" if dialect == "sqlite"
                    else "json_typeof(memory_refs) = 'array'")
    op.create_table(
        "analysis_index_receipts",
        sa.Column("receipt_key", sa.String(36), nullable=False),
        sa.Column("source_kind", sa.String(8), nullable=False),
        sa.Column("source_key", sa.String(36), nullable=False),
        sa.Column("outcome", sa.String(8), nullable=False),
        sa.Column("source_task_key", sa.String(36), nullable=False),
        sa.Column("parent_task_key", sa.String(36), nullable=False),
        sa.Column("parent_execution_key", sa.String(36), nullable=False),
        sa.Column("parent_queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("actor_token_version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("grant_invitation_id", sa.String(36), nullable=True),
        sa.Column("grant_revision", sa.Integer(), nullable=True),
        sa.Column("analysis_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.Column("memory_refs", sa.JSON(), nullable=False),
        sa.Column("memory_hash", sa.String(64), nullable=False),
        sa.Column("child_task_key", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("receipt_key"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.CheckConstraint("source_kind IN ('single', 'batch')", name=op.f("ck_analysis_index_receipts_source_kind")),
        *(sa.CheckConstraint(_uuid_check(name, version4=name == "receipt_key"),
                            name=op.f(f"ck_analysis_index_receipts_{name}_uuid")) for name in (
            "receipt_key", "source_key", "source_task_key", "parent_task_key", "parent_execution_key",
        )),
        sa.CheckConstraint(f"child_task_key IS NULL OR ({_uuid_check('child_task_key')})",
                           name=op.f("ck_analysis_index_receipts_child_task_key_uuid")),
        sa.CheckConstraint(
            "outcome = 'queued' AND child_task_key IS NOT NULL OR outcome = 'no_work' AND child_task_key IS NULL",
            name=op.f("ck_analysis_index_receipts_outcome_child"),
        ),
        sa.CheckConstraint(
            "grant_invitation_id IS NULL AND grant_revision IS NULL OR "
            f"grant_invitation_id IS NOT NULL AND grant_revision IS NOT NULL AND ({_int_check('grant_revision')}) AND "
            f"({_uuid_check('grant_invitation_id')})", name=op.f("ck_analysis_index_receipts_grant_identity"),
        ),
        sa.CheckConstraint(" AND ".join(
            f"({_int_check(name, allow_zero=name == 'actor_token_version')})" for name in (
                "actor_id", "actor_token_version", "project_id", "chapter_id", "analysis_id",
            )
        ), name=op.f("ck_analysis_index_receipts_positive_identity")),
        sa.CheckConstraint(memory_check, name=op.f("ck_analysis_index_receipts_memory_refs_array")),
        sa.CheckConstraint("outcome = 'no_work' OR json_array_length(memory_refs) > 0",
                           name=op.f("ck_analysis_index_receipts_queued_memory")),
        sa.CheckConstraint("source_kind <> 'single' OR source_task_key = parent_task_key",
                           name=op.f("ck_analysis_index_receipts_single_source_task")),
        sa.CheckConstraint(
            "receipt_key <> source_key AND receipt_key <> source_task_key AND receipt_key <> parent_task_key "
            "AND receipt_key <> parent_execution_key AND (child_task_key IS NULL OR "
            "(receipt_key <> child_task_key AND child_task_key <> source_key AND child_task_key <> source_task_key "
            "AND child_task_key <> parent_task_key AND child_task_key <> parent_execution_key))",
            name=op.f("ck_analysis_index_receipts_distinct_identities"),
        ),
        *(sa.CheckConstraint(_hash_check(name), name=op.f(f"ck_analysis_index_receipts_{name}_sha256")) for name in (
            "analysis_hash", "content_hash", "input_hash", "selection_hash", "memory_hash",
        )),
        sa.UniqueConstraint("source_kind", "source_key", name="uq_analysis_index_receipts_source"),
        sa.UniqueConstraint("child_task_key", name=op.f("uq_analysis_index_receipts_child_task_key")),
    )
    op.create_index(op.f("ix_analysis_index_receipts_project_id"), "analysis_index_receipts", ["project_id"])
    if dialect == "sqlite":
        op.execute("CREATE TRIGGER analysis_index_receipts_immutable BEFORE UPDATE ON analysis_index_receipts "
                   "BEGIN SELECT RAISE(ABORT, 'analysis index receipt is immutable'); END")
    elif dialect == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION analysis_index_receipts_immutable_fn() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'analysis index receipt is immutable' USING ERRCODE = '23514'; END; $$"
        )
        op.execute("CREATE TRIGGER analysis_index_receipts_immutable BEFORE UPDATE ON analysis_index_receipts "
                   "FOR EACH ROW EXECUTE FUNCTION analysis_index_receipts_immutable_fn()")


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        # 与派生时先锁 Task、再写收据的顺序一致，避免空表检查与并发创建穿插。
        db.execute(sa.text("LOCK TABLE tasks, analysis_index_receipts IN ACCESS EXCLUSIVE MODE"))
    elif db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE tasks SET id = id WHERE 0"))
    if db.execute(sa.text("SELECT 1 FROM analysis_index_receipts LIMIT 1")).first() is not None:
        raise RuntimeError("存在分析索引去重收据，不能安全降级")
    if db.execute(sa.text(
        "SELECT 1 FROM tasks WHERE kind IN ('chapter.analyze','chapter.batch','kb.index') AND "
        "(status NOT IN ('succeeded','failed','cancelled') OR execution_key IS NOT NULL "
        "OR admin_cancel_operation_id IS NOT NULL OR admin_cancel_requested_at IS NOT NULL) LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有未完成的分析或索引任务，不能安全降级")
    op.drop_index(op.f("ix_analysis_index_receipts_project_id"), table_name="analysis_index_receipts")
    op.drop_table("analysis_index_receipts")
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS analysis_index_receipts_immutable_fn()")
