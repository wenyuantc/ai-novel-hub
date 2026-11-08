"""提示词组件：作者个人的可复用提示词片段。

只新增 prompt_components 表（user_id CASCADE、(user_id, slug) 唯一）；不改动已有数据。
旧的模板覆盖与 Skill 正文不可能包含 `{{> 标识 }}`（在 Jinja 中是语法错误），无需回填。

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompt_components",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(48), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), server_default="", nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_prompt_components_revision_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_prompt_components_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_components")),
        sa.UniqueConstraint("user_id", "slug", name=op.f("uq_prompt_components_user_id")),
    )
    op.create_index(op.f("ix_prompt_components_user_id"), "prompt_components", ["user_id"])


def downgrade() -> None:
    # 覆盖与 Skill 中的 `{{> 标识 }}` 依赖组件内容；删表后它们会失效，有数据时拒绝有损降级
    if op.get_bind().execute(sa.text("SELECT 1 FROM prompt_components LIMIT 1")).first() is not None:
        raise RuntimeError("仍有提示词组件，拒绝有损降级；请先导出组件库并删除全部组件")
    op.drop_index(op.f("ix_prompt_components_user_id"), table_name="prompt_components")
    op.drop_table("prompt_components")
