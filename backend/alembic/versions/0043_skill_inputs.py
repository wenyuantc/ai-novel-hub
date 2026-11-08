"""Skill 自定义输入项：skills.inputs（JSON 列表，默认 []）。

存量 Skill 由 server_default 回填为空列表，正文不做任何改动（没有输入项时占位不替换）。
原位 ADD / DROP COLUMN，不重建 SQLite 上的 skills 父表（skill_references、agent_messages 引用它）。

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("inputs", sa.JSON(), server_default="[]", nullable=False))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM skills WHERE CAST(inputs AS TEXT) NOT IN ('[]', '') LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有声明了输入项的 Skill，拒绝有损降级；请先删除这些输入项或导出备份")
    op.drop_column("skills", "inputs")
