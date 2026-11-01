"""成功登录时间，存量账号不推算历史。

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade():
    # 不重建 users 父表，保留 RESTRICT/CASCADE 子行和外键完整性。
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("users", "last_login_at")
