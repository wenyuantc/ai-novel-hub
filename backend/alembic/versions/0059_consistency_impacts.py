"""修改影响追踪：事实台账保留上一版（三个可空列），新增每章一行的影响评估表。"""

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

PREV_COLUMNS = ("prev_facts", "prev_source_hash", "prev_text_hash")


def upgrade() -> None:
    with op.batch_alter_table("consistency_ledgers") as batch:
        batch.add_column(sa.Column("prev_facts", sa.JSON(none_as_null=True), nullable=True))
        batch.add_column(sa.Column("prev_source_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("prev_text_hash", sa.String(64), nullable=True))

    op.create_table(
        "consistency_impacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("text_hash", sa.String(64), nullable=True),
        sa.Column("base_source_hash", sa.String(64), nullable=True),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("uncovered", sa.JSON(), nullable=False),
        sa.Column("acknowledged", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consistency_impacts")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE",
                                name=op.f("fk_consistency_impacts_project_id_projects")),
        sa.ForeignKeyConstraint(["chapter_id"], ["chapters.id"], ondelete="CASCADE",
                                name=op.f("fk_consistency_impacts_chapter_id_chapters")),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_impacts_actor_id_users")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL",
                                name=op.f("fk_consistency_impacts_task_id_tasks")),
        sa.UniqueConstraint("chapter_id", name=op.f("uq_consistency_impacts_chapter_id")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_consistency_impacts_revision_positive")),
    )
    for column in ("project_id", "actor_id", "task_id"):
        op.create_index(op.f(f"ix_consistency_impacts_{column}"), "consistency_impacts", [column])


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # 先取写锁，再检查是否仍有数据。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE consistency_impacts, consistency_ledgers IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("修改影响追踪降级不支持当前数据库")
    if (db.execute(sa.text("SELECT 1 FROM consistency_impacts LIMIT 1")).first() is not None
            or db.execute(sa.text(
                "SELECT 1 FROM consistency_ledgers WHERE prev_facts IS NOT NULL OR prev_source_hash IS NOT NULL "
                "OR prev_text_hash IS NOT NULL LIMIT 1")).first() is not None):
        raise RuntimeError("仍有修改影响记录或上一版事实台账，拒绝有损降级；请先备份并确认处置方式")
    op.drop_table("consistency_impacts")
    with op.batch_alter_table("consistency_ledgers") as batch:
        for column in PREV_COLUMNS:
            batch.drop_column(column)
