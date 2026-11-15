"""为共享作品派生的灵感会话保留原邀请身份。

Revision ID: 0050
Revises: 0049
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspiration_origins",
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("invitation_id", sa.String(length=36), nullable=False),
        sa.Column("origin_key", sa.String(length=36), nullable=False),
        sa.CheckConstraint("project_id > 0", name=op.f("ck_inspiration_origins_positive_project")),
        sa.CheckConstraint("length(invitation_id) = 36", name=op.f("ck_inspiration_origins_invitation_shape")),
        sa.CheckConstraint("length(origin_key) = 36", name=op.f("ck_inspiration_origins_origin_shape")),
        sa.ForeignKeyConstraint(["session_id"], ["inspiration_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("origin_key", name=op.f("uq_inspiration_origins_origin_key")),
    )
    op.create_index(op.f("ix_inspiration_origins_project_id"), "inspiration_origins", ["project_id"])


def downgrade() -> None:
    # 旧代码没有来源检查；丢弃绑定会重新暴露已撤销或已删除作品的快照。
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE inspiration_origins IN ACCESS EXCLUSIVE MODE"))
    elif bind.dialect.name == "sqlite":
        # 先获取 SQLite 写事务，避免检查空表后并发插入来源再被 DROP。
        bind.execute(sa.text("UPDATE inspiration_origins SET origin_key = origin_key WHERE 0"))
    if bind.execute(sa.text("SELECT 1 FROM inspiration_origins LIMIT 1")).first() is not None:
        raise RuntimeError("存在共享作品派生的灵感会话，不能安全降级")
    op.drop_index(op.f("ix_inspiration_origins_project_id"), table_name="inspiration_origins")
    op.drop_table("inspiration_origins")
