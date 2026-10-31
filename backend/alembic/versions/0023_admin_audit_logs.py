"""Independent administrator audit snapshots.

Revision ID: 0023
Revises: 0022
"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False, unique=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("result", sa.String(8), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.Integer()),
        sa.Column("target_label", sa.String(200)),
        sa.Column("owner_user_id", sa.Integer()),
        sa.Column("owner_username", sa.String(64)),
        sa.Column("project_id", sa.Integer()),
        sa.Column("project_title", sa.String(200)),
        sa.Column("chapter_id", sa.Integer()),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("version_ref", sa.JSON()),
        sa.Column("error_code", sa.String(32)),
        sa.Column("error_message", sa.String(200)),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_admin_audit_time", "admin_audit_logs", ["occurred_at", "id"])
    op.create_index("ix_admin_audit_actor_time", "admin_audit_logs", ["actor_user_id", "occurred_at", "id"])
    op.create_index("ix_admin_audit_target_time", "admin_audit_logs", ["target_type", "target_id", "occurred_at", "id"])


def downgrade():
    op.drop_table("admin_audit_logs")
