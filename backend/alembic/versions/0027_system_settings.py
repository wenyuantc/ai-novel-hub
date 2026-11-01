"""实例级运行设置。

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("overrides", sa.JSON(), nullable=False),
        sa.Column("last_operation_id", sa.String(36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_system_settings_singleton")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_system_settings_positive_revision")),
    )


def downgrade():
    op.drop_table("system_settings")
