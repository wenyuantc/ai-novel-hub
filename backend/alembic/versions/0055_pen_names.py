"""作者私人笔名与单作品署名；拒绝有数据的有损降级。"""

import sqlalchemy as sa
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pen_names",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_id", "name", name="uq_pen_names_owner_id"),
        sa.UniqueConstraint("id", "owner_id", name="uq_pen_names_id"),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_pen_names_revision")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_pen_names_owner_id", "pen_names", ["owner_id"])
    op.create_table(
        "project_pen_names",
        sa.Column("project_id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("pen_name_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_id"], ["projects.id", "projects.user_id"],
            name="fk_project_pen_names_project_owner", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pen_name_id", "owner_id"], ["pen_names.id", "pen_names.owner_id"],
            name="fk_project_pen_names_pen_owner", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_project_pen_names_pen_name_id", "project_pen_names", ["pen_name_id"])


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE pen_names, project_pen_names IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("笔名降级不支持当前数据库")
    if db.execute(sa.text("SELECT 1 FROM pen_names LIMIT 1")).first() is not None:
        raise RuntimeError("仍有笔名数据，拒绝有损降级；请先备份并确认处置方式")
    if db.execute(sa.text("SELECT 1 FROM project_pen_names LIMIT 1")).first() is not None:
        raise RuntimeError("仍有作品署名，拒绝有损降级；请先备份并确认处置方式")
    op.drop_index("ix_project_pen_names_pen_name_id", table_name="project_pen_names")
    op.drop_table("project_pen_names")
    op.drop_index("ix_pen_names_owner_id", table_name="pen_names")
    op.drop_table("pen_names")
