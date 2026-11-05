"""世界观条目全书别名。

Revision ID: 0033
Revises: 0032
"""

from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("world_settings", sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    # 当前 SQLite 也支持原位 DROP COLUMN，避免重建父表触发时间线级联删除。
    op.drop_column("world_settings", "aliases")
