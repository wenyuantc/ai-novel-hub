"""Agent 会话的协作邀请轮次（E4i-d 裁决 7）。

Revision ID: 0054
Revises: 0053

- agent_conversations 原位增加可空 collaboration_invitation_id（String(36)），不重建被 agent_messages 等引用的父表。
- 旧行保持 NULL（作者会话 / 升级前）；协作者 HTTP 入口（E4i-d）开放后才会写入非空值，不补认。
- 降级：已有协作轮次标记时拒绝（与 0053 等同，不做有损降级：删列后再升级，旧轮次会话会被误判为非本轮或丢失只读依据）；
  无标记时直接删列。检查前先取写锁（SQLite 写 alembic_version、PostgreSQL ACCESS EXCLUSIVE），避免扫描后有新标记提交。
  Agent 表不进入单书备份。
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_conversations", sa.Column("collaboration_invitation_id", sa.String(length=36), nullable=True),
    )


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE agent_conversations IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("Agent 会话协作轮次降级不支持当前数据库")
    if db.execute(sa.text(
        "SELECT 1 FROM agent_conversations WHERE collaboration_invitation_id IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有协作者创建的 Agent 会话轮次标记，拒绝有损降级；请先完整备份并确认处置方式")
    op.drop_column("agent_conversations", "collaboration_invitation_id")
