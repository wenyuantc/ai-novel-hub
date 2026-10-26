"""chapter analysis

章节分析与回写：新建 chapter_analyses / analysis_changes 两张表，
projects 加 auto_analyze（回填 true），tasks 加 serial_key（同键排队串行）。
downgrade 删除两张表与两列，不影响存量数据。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-26 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("auto_analyze", sa.Boolean(), server_default=sa.true(), nullable=False))

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("serial_key", sa.String(length=100), nullable=True))
        batch_op.create_index(batch_op.f("ix_tasks_serial_key"), ["serial_key"], unique=False)

    op.create_table(
        "chapter_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("chapter_no", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("plot_points", sa.JSON(), nullable=False),
        sa.Column("hooks", sa.JSON(), nullable=False),
        sa.Column("conflict", sa.JSON(), nullable=False),
        sa.Column("emotion", sa.JSON(), nullable=False),
        sa.Column("changes_raw", sa.JSON(), nullable=False),
        sa.Column("foreshadow_candidates", sa.JSON(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("suggestions", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=200), server_default="", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["chapter_id"], ["chapters.id"], name=op.f("fk_chapter_analyses_chapter_id_chapters"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_chapter_analyses_project_id_projects"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_chapter_analyses_task_id_tasks"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chapter_analyses")),
    )
    with op.batch_alter_table("chapter_analyses", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chapter_analyses_chapter_id"), ["chapter_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_chapter_analyses_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_chapter_analyses_task_id"), ["task_id"], unique=False)

    op.create_table(
        "analysis_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("analysis_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("chapter_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("field_key", sa.String(length=120), server_default="", nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("reason", sa.String(length=200), server_default="", nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["chapter_analyses.id"],
            name=op.f("fk_analysis_changes_analysis_id_chapter_analyses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_analysis_changes_project_id_projects"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_changes")),
    )
    with op.batch_alter_table("analysis_changes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_analysis_changes_analysis_id"), ["analysis_id"], unique=False)
        batch_op.create_index("ix_analysis_changes_project_id_field_key", ["project_id", "field_key"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("analysis_changes", schema=None) as batch_op:
        batch_op.drop_index("ix_analysis_changes_project_id_field_key")
        batch_op.drop_index(batch_op.f("ix_analysis_changes_analysis_id"))
    op.drop_table("analysis_changes")

    with op.batch_alter_table("chapter_analyses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_chapter_analyses_task_id"))
        batch_op.drop_index(batch_op.f("ix_chapter_analyses_project_id"))
        batch_op.drop_index(batch_op.f("ix_chapter_analyses_chapter_id"))
    op.drop_table("chapter_analyses")

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_serial_key"))
        batch_op.drop_column("serial_key")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("auto_analyze")
