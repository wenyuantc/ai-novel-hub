"""作品笔记：独立原文及修订号。"""

from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_project_notes_revision_positive")),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"],
                                name=op.f("fk_project_notes_project_id_projects"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_notes")),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f("ix_project_notes_project_id"), "project_notes", ["project_id"], unique=False)


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM project_notes LIMIT 1")).first() is not None:
        raise RuntimeError("存在作品笔记，不能无损降级；请先备份并明确处理笔记数据")
    op.drop_index(op.f("ix_project_notes_project_id"), table_name="project_notes")
    op.drop_table("project_notes")
