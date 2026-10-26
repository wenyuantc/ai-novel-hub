"""inspiration

灵感模式与项目结构化字段：projects 新增 synopsis（简介）/ theme（主题）/ tags（类型标签，JSON 列表）/
selling_points（核心卖点）/ audience（目标读者）/ target_words（目标总字数，可空），均带默认值、不回填；
新建 inspiration_sessions（灵感会话，user_id CASCADE，project_id / converted_project_id SET NULL）。
downgrade 删除新表与新列。

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-27 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("synopsis", sa.Text(), server_default="", nullable=False))
        batch_op.add_column(sa.Column("theme", sa.String(length=200), server_default="", nullable=False))
        batch_op.add_column(sa.Column("tags", sa.JSON(), server_default="[]", nullable=False))
        batch_op.add_column(sa.Column("selling_points", sa.Text(), server_default="", nullable=False))
        batch_op.add_column(sa.Column("audience", sa.String(length=100), server_default="", nullable=False))
        batch_op.add_column(sa.Column("target_words", sa.Integer(), nullable=True))

    op.create_table(
        "inspiration_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), server_default="", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("state", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("converted_project_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_inspiration_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_inspiration_sessions_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["converted_project_id"],
            ["projects.id"],
            name=op.f("fk_inspiration_sessions_converted_project_id_projects"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspiration_sessions")),
    )
    with op.batch_alter_table("inspiration_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_inspiration_sessions_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_inspiration_sessions_project_id"), ["project_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_inspiration_sessions_converted_project_id"), ["converted_project_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("inspiration_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inspiration_sessions_converted_project_id"))
        batch_op.drop_index(batch_op.f("ix_inspiration_sessions_project_id"))
        batch_op.drop_index(batch_op.f("ix_inspiration_sessions_user_id"))
    op.drop_table("inspiration_sessions")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("target_words")
        batch_op.drop_column("audience")
        batch_op.drop_column("selling_points")
        batch_op.drop_column("tags")
        batch_op.drop_column("theme")
        batch_op.drop_column("synopsis")
