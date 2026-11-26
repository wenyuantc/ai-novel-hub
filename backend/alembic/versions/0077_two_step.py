"""验证器、恢复码和登录挑战。"""

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "two_step_factors",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("secret_enc", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        *(
            sa.Column(name, sa.Integer, nullable=False)
            for name in ("setup_version", "setup_expires", "last_step", "window_start", "failures")
        ),
    )
    for table in ("two_step_recovery_codes", "two_step_login_challenges"):
        fields = []
        if table == "two_step_login_challenges":
            fields = [
                sa.Column("version", sa.Integer, nullable=False),
                sa.Column("expires", sa.Integer, nullable=False),
            ]
        op.create_table(
            table,
            sa.Column("digest", sa.String(64), primary_key=True),
            sa.Column(
                "user_id", sa.Integer, sa.ForeignKey("two_step_factors.user_id", ondelete="CASCADE"), nullable=False
            ),
            *fields,
        )
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    op.create_index("ix_two_step_login_challenges_expires", "two_step_login_challenges", ["expires"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM two_step_factors WHERE enabled")):
        raise RuntimeError("存在已启用两步验证的账号，无法降级")
    if op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM admin_audit_logs WHERE action IN ('user.two_step.reset', 'user.two_step.local_recovery')"
    )):
        raise RuntimeError("存在两步验证审计记录，无法降级")
    op.drop_table("two_step_login_challenges")
    op.drop_table("two_step_recovery_codes")
    op.drop_table("two_step_factors")
