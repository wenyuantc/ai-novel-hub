"""Project agent conversations, messages and changes.

Revision ID: 0018
Revises: 0017
"""

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), server_default="", nullable=False),
        sa.Column("auto_execute", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("summarized_until", sa.Integer()),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        *timestamps(),
    )
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("turn_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("tool_calls", sa.JSON()),
        sa.Column("tool_call_id", sa.String(200)),
        sa.Column("tool_name", sa.String(100)),
        sa.Column("page_context", sa.JSON()),
        sa.Column("usage", sa.JSON()),
        *timestamps(),
        sa.UniqueConstraint(
            "conversation_id", "turn_task_id", "tool_call_id", name="uq_agent_messages_conversation_id"
        ),
    )
    op.create_table(
        "agent_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id", sa.Integer(), sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("tool_call_id", sa.String(200), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), server_default="proposed", nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("effects", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("undone_at", sa.DateTime(timezone=True)),
        sa.Column("seq", sa.Integer(), server_default="0", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("conversation_id", "turn_task_id", "tool_call_id", name="uq_agent_changes_conversation_id"),
    )
    for table, columns in {
        "agent_conversations": ("user_id", "project_id"),
        "agent_messages": ("conversation_id", "turn_task_id"),
        "agent_changes": ("conversation_id", "project_id", "turn_task_id"),
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    op.drop_table("agent_changes")
    op.drop_table("agent_messages")
    op.drop_table("agent_conversations")
    op.execute("UPDATE chapter_versions SET source = 'manual' WHERE source = 'agent'")
    op.execute("DELETE FROM generation_logs WHERE kind = 'agent.turn'")
    op.execute("DELETE FROM tasks WHERE kind = 'agent.turn'")
