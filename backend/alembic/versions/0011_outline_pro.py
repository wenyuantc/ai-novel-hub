"""outline pro

大纲进阶：新建 chapter_plans（细纲）/ outline_snapshots（大纲快照），chapters 加 plan_id（SET NULL，与
chapter_plans.chapter_id 双向冗余），projects 加 wizard_progress（回填 {}）。不改动存量数据。
downgrade 删除两张表与两列，存量大纲 / 章节保留。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-26 22:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("wizard_progress", sa.JSON(), server_default="{}", nullable=False))

    op.create_table(
        "chapter_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("outline_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("key_events", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("character_ids", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("emotion", sa.String(length=100), server_default="", nullable=False),
        sa.Column("goal", sa.Text(), server_default="", nullable=False),
        sa.Column("est_words", sa.Integer(), server_default="3000", nullable=False),
        sa.Column("chapter_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_chapter_plans_project_id_projects"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["outline_id"], ["outlines.id"], name=op.f("fk_chapter_plans_outline_id_outlines"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"], ["chapters.id"], name=op.f("fk_chapter_plans_chapter_id_chapters"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chapter_plans")),
        sa.UniqueConstraint("chapter_id", name=op.f("uq_chapter_plans_chapter_id")),
    )
    with op.batch_alter_table("chapter_plans", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chapter_plans_project_id"), ["project_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_chapter_plans_outline_id"), ["outline_id"], unique=False)

    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plan_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_chapters_plan_id"), ["plan_id"], unique=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_chapters_plan_id_chapter_plans"), "chapter_plans", ["plan_id"], ["id"], ondelete="SET NULL"
        )

    op.create_table(
        "outline_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=200), server_default="", nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_outline_snapshots_project_id_projects"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outline_snapshots")),
    )
    with op.batch_alter_table("outline_snapshots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_outline_snapshots_project_id"), ["project_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("outline_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_outline_snapshots_project_id"))
    op.drop_table("outline_snapshots")

    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_chapters_plan_id_chapter_plans"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_chapters_plan_id"))
        batch_op.drop_column("plan_id")

    with op.batch_alter_table("chapter_plans", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_chapter_plans_outline_id"))
        batch_op.drop_index(batch_op.f("ix_chapter_plans_project_id"))
    op.drop_table("chapter_plans")

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("wizard_progress")
