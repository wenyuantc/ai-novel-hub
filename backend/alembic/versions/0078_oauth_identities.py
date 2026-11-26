"""本地用户的第三方身份，以及跨 worker 一次性授权流程。"""
import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("has_local_password", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table("oauth_identities",
                    sa.Column("provider", sa.String(16), primary_key=True),
                    sa.Column("subject", sa.String(64), primary_key=True),
                    sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
                    sa.UniqueConstraint("user_id", "provider"))
    op.create_table("oauth_states",
                    sa.Column("digest", sa.String(64), primary_key=True),
                    sa.Column("browser_digest", sa.String(64), nullable=False),
                    sa.Column("provider", sa.String(16), nullable=False),
                    sa.Column("intent", sa.String(8), nullable=False),
                    sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
                    sa.Column("token_version", sa.Integer()),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_oauth_states_expires_at", "oauth_states", ["expires_at"])


def downgrade() -> None:
    db = op.get_bind()
    if db.scalar(sa.text("SELECT COUNT(*) FROM oauth_identities")) or db.scalar(
        sa.text("SELECT COUNT(*) FROM users WHERE NOT has_local_password")
    ):
        raise RuntimeError("请先处理第三方身份及登录方式再降级")
    op.drop_table("oauth_states")
    op.drop_table("oauth_identities")
    op.drop_column("users", "has_local_password")
