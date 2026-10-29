"""Instance-local prompt workshop.

Revision ID: 0020
Revises: 0019
"""

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def content_columns():
    return [
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("prompt_key", sa.String(100)),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), server_default="", nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("tags", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("is_anonymous", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "workshop_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        *content_columns(),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("is_official", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(8), server_default="active", nullable=False),
        sa.Column("hidden_by", sa.String(8)),
        sa.Column("like_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("download_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for field in ("author_id", "status", "prompt_key"):
        op.create_index(f"ix_workshop_items_{field}", "workshop_items", [field])
    op.create_table(
        "workshop_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        *content_columns(),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("workshop_items.id", ondelete="CASCADE")),
        sa.Column("submitter_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(10), server_default="pending", nullable=False),
        sa.Column("review_note", sa.String(500), server_default="", nullable=False),
        sa.Column("reviewer_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )
    for field in ("item_id", "submitter_id", "status"):
        op.create_index(f"ix_workshop_submissions_{field}", "workshop_submissions", [field])
    op.create_index(
        "uq_workshop_pending_item",
        "workshop_submissions",
        ["item_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    for table in ("workshop_likes", "workshop_imports"):
        op.create_table(
            table,
            sa.Column(
                "item_id", sa.Integer(), sa.ForeignKey("workshop_items.id", ondelete="CASCADE"), primary_key=True
            ),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        )


def downgrade():
    for table in ("workshop_imports", "workshop_likes", "workshop_submissions", "workshop_items"):
        op.drop_table(table)
