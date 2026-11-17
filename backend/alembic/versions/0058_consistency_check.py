"""一致性检查：逐章事实台账、检查运行记录与跨章问题。只新增三张表，不改既有表。"""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None

TABLES = ("consistency_issues", "consistency_ledgers", "consistency_runs")


def _timestamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        "consistency_ledgers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consistency_ledgers")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                name=op.f("fk_consistency_ledgers_project_id_projects")),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE",
                                name=op.f("fk_consistency_ledgers_chapter_id_chapters")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_ledgers_task_id_tasks")),
        sa.UniqueConstraint("chapter_id", name=op.f("uq_consistency_ledgers_chapter_id")),
        sa.CheckConstraint("schema_version >= 1", name=op.f("ck_consistency_ledgers_schema_version_positive")),
    )
    op.create_index(op.f("ix_consistency_ledgers_project_id"), "consistency_ledgers", ["project_id"])
    op.create_index(op.f("ix_consistency_ledgers_task_id"), "consistency_ledgers", ["task_id"])

    op.create_table(
        "consistency_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("stats", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consistency_runs")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                name=op.f("fk_consistency_runs_project_id_projects")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_runs_task_id_tasks")),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_runs_actor_id_users")),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f("ix_consistency_runs_project_id"), "consistency_runs", ["project_id"])
    op.create_index(op.f("ix_consistency_runs_task_id"), "consistency_runs", ["task_id"])
    op.create_index(op.f("ix_consistency_runs_actor_id"), "consistency_runs", ["actor_id"])

    op.create_table(
        "consistency_issues",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("suggestion", sa.Text(), nullable=False, server_default=""),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("status_changed_by", sa.Integer(), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("first_run_id", sa.Integer(), nullable=True),
        sa.Column("last_seen_run_id", sa.Integer(), nullable=True),
        sa.Column("last_checked_run_id", sa.Integer(), nullable=True),
        sa.Column("reappeared", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consistency_issues")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                name=op.f("fk_consistency_issues_project_id_projects")),
        sa.ForeignKeyConstraint(["status_changed_by"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_issues_status_changed_by_users")),
        sa.ForeignKeyConstraint(["first_run_id"], ["consistency_runs.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_issues_first_run_id_consistency_runs")),
        sa.ForeignKeyConstraint(["last_seen_run_id"], ["consistency_runs.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_issues_last_seen_run_id_consistency_runs")),
        sa.ForeignKeyConstraint(["last_checked_run_id"], ["consistency_runs.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_issues_last_checked_run_id_consistency_runs")),
        sa.UniqueConstraint("project_id", "identity_key", name=op.f("uq_consistency_issues_project_id")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_consistency_issues_revision_positive")),
    )
    for column in ("project_id", "status_changed_by", "first_run_id", "last_seen_run_id", "last_checked_run_id"):
        op.create_index(op.f(f"ix_consistency_issues_{column}"), "consistency_issues", [column])


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # 先取写锁，再检查是否仍有数据。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("一致性检查降级不支持当前数据库")
    for table in TABLES:
        if db.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("仍有事实台账、检查记录或一致性问题，拒绝有损降级；请先备份并确认处置方式")
    for table in TABLES:
        op.drop_table(table)
