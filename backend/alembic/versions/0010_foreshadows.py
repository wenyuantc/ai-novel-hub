"""foreshadows

伏笔管理：新建 foreshadows / foreshadow_characters / foreshadow_history 三张表，不回填。
章节字段存章节号、不做外键；删除角色只删除关联行。downgrade 删除三张表，不影响存量数据。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-26 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "foreshadows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("plant_hint", sa.Text(), server_default="", nullable=False),
        sa.Column("reveal_hint", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("planned_plant_chapter", sa.Integer(), nullable=True),
        sa.Column("actual_plant_chapter", sa.Integer(), nullable=True),
        sa.Column("planned_resolve_chapter", sa.Integer(), nullable=True),
        sa.Column("actual_resolve_chapter", sa.Integer(), nullable=True),
        sa.Column("importance", sa.Integer(), server_default="3", nullable=False),
        sa.Column("include_in_context", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.String(length=12), server_default="manual", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_foreshadows_project_id_projects"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_foreshadows")),
    )
    with op.batch_alter_table("foreshadows", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_foreshadows_project_id"), ["project_id"], unique=False)

    op.create_table(
        "foreshadow_characters",
        sa.Column("foreshadow_id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            name=op.f("fk_foreshadow_characters_character_id_characters"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["foreshadow_id"],
            ["foreshadows.id"],
            name=op.f("fk_foreshadow_characters_foreshadow_id_foreshadows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("foreshadow_id", "character_id", name=op.f("pk_foreshadow_characters")),
    )
    with op.batch_alter_table("foreshadow_characters", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_foreshadow_characters_character_id"), ["character_id"], unique=False)

    op.create_table(
        "foreshadow_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("foreshadow_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("chapter_no", sa.Integer(), nullable=True),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(length=200), server_default="", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["foreshadow_id"],
            ["foreshadows.id"],
            name=op.f("fk_foreshadow_history_foreshadow_id_foreshadows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_foreshadow_history")),
    )
    with op.batch_alter_table("foreshadow_history", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_foreshadow_history_foreshadow_id"), ["foreshadow_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("foreshadow_history", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_foreshadow_history_foreshadow_id"))
    op.drop_table("foreshadow_history")

    with op.batch_alter_table("foreshadow_characters", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_foreshadow_characters_character_id"))
    op.drop_table("foreshadow_characters")

    with op.batch_alter_table("foreshadows", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_foreshadows_project_id"))
    op.drop_table("foreshadows")
