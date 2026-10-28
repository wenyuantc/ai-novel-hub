"""Skills (user / global) and remote MCP plugins.

Revision ID: 0019
Revises: 0018
"""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade():
    skills = op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("slug", sa.String(48), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), server_default="", nullable=False),
        sa.Column("category", sa.String(30), server_default="", nullable=False),
        sa.Column("triggers", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "source_skill_id",
            sa.Integer(),
            sa.ForeignKey("skills.id", ondelete="SET NULL", name="fk_skills_source_skill_id_skills"),
            nullable=True,
        ),
        sa.Column("builtin_key", sa.String(48), nullable=True),
        *timestamps(),
        sa.UniqueConstraint("owner_user_id", "slug", name="uq_skills_owner_user_id"),
        sa.UniqueConstraint("builtin_key", name="uq_skills_builtin_key"),
    )
    op.create_index("ix_skills_owner_user_id", "skills", ["owner_user_id"])
    op.create_index("ix_skills_source_skill_id", "skills", ["source_skill_id"])
    op.create_index(
        "uq_skills_global_slug",
        "skills",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NULL"),
        sqlite_where=sa.text("owner_user_id IS NULL"),
    )
    op.create_table(
        "skill_references",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        *timestamps(),
    )
    op.create_index("ix_skill_references_skill_id", "skill_references", ["skill_id"])
    op.create_table(
        "mcp_plugins",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(24), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("headers_encrypted", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("tools", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("tool_prefs", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("tools_refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("last_tested_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text(), server_default="", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("user_id", "slug", name="uq_mcp_plugins_user_id"),
    )
    op.create_index("ix_mcp_plugins_user_id", "mcp_plugins", ["user_id"])

    with op.batch_alter_table("agent_conversations") as batch:
        batch.add_column(sa.Column("mcp_plugin_ids", sa.JSON(), nullable=True))
    with op.batch_alter_table("agent_messages") as batch:
        batch.add_column(sa.Column("skill_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("skill_snapshot", sa.JSON(), nullable=True))
        batch.create_foreign_key(
            "fk_agent_messages_skill_id_skills", "skills", ["skill_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_agent_messages_skill_id", ["skill_id"])

    # 内置 Skill：正文为本项目自行编写（app/skills/builtin/*.md）
    from app.skills import load_builtin

    op.bulk_insert(skills, [{**item, "owner_user_id": None} for item in load_builtin()])


def downgrade():
    with op.batch_alter_table("agent_messages") as batch:
        batch.drop_index("ix_agent_messages_skill_id")
        batch.drop_constraint("fk_agent_messages_skill_id_skills", type_="foreignkey")
        batch.drop_column("skill_snapshot")
        batch.drop_column("skill_id")
    with op.batch_alter_table("agent_conversations") as batch:
        batch.drop_column("mcp_plugin_ids")
    op.drop_table("mcp_plugins")
    op.drop_table("skill_references")
    op.drop_index("uq_skills_global_slug", table_name="skills")
    op.drop_table("skills")
