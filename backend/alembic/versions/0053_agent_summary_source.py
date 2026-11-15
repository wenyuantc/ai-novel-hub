"""Agent 会话摘要来源标记（E4i-b 裁决 13）。

Revision ID: 0053
Revises: 0052

- agent_conversations 原位增加可空 summary_source（String(80)），不重建被 agent_messages 等引用的父表。
- 旧行保持 NULL（作者 / 升级前摘要），不按 actor 或时间补认来源。
- 降级：已有协作来源标记时拒绝（与 0052 等同，不做有损降级：删列后再升级，标记为 NULL 的编辑者摘要会永久不可验证）；
  无标记时直接删列。检查前先取写锁（SQLite 写 alembic_version、PostgreSQL ACCESS EXCLUSIVE），避免扫描后有新标记提交。
  Agent 表不进入单书备份。
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_conversations", sa.Column("summary_source", sa.String(length=80), nullable=True))


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE agent_conversations IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("Agent 摘要来源降级不支持当前数据库")
    if db.execute(sa.text(
        "SELECT 1 FROM agent_conversations WHERE summary_source IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有带协作来源标记的 Agent 摘要，拒绝有损降级；请先完整备份并确认处置方式")
    op.drop_column("agent_conversations", "summary_source")
