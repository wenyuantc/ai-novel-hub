"""本人小说模板、独立图与持久建书收据。"""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "novel_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("format_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("source_title", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_novel_templates_revision")),
        sa.CheckConstraint("format_version = 1", name=op.f("ck_novel_templates_format_version")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_novel_templates_owner_id", "novel_templates", ["owner_id"])
    op.create_table(
        "novel_template_images",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("novel_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("local_ref", sa.String(100), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("template_id", "local_ref", name="uq_novel_template_images_template_id"),
        sa.CheckConstraint("width > 0 AND height > 0 AND byte_size > 0",
                           name=op.f("ck_novel_template_images_positive_size")),
    )
    op.create_index("ix_novel_template_images_template_id", "novel_template_images", ["template_id"])
    op.create_table(
        "novel_template_creation_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("intent_key", sa.String(36), nullable=False),
        sa.Column("request_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("result_project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_id", "intent_key", name="uq_novel_template_creation_receipts_owner_id"),
        sa.CheckConstraint("request_version = 1", name=op.f("ck_novel_template_creation_receipts_request_version")),
    )
    op.create_index("ix_novel_template_creation_receipts_owner_id", "novel_template_creation_receipts", ["owner_id"])
    op.create_index("ix_novel_template_creation_receipts_result_project_id", "novel_template_creation_receipts",
                    ["result_project_id"])


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE novel_templates, novel_template_images, novel_template_creation_receipts "
                   "IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("模板降级不支持当前数据库")
    for table in ("novel_templates", "novel_template_images", "novel_template_creation_receipts"):
        if db.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("仍有模板、图片或建书收据，拒绝有损降级；请先备份并确认处置方式")
    op.drop_table("novel_template_creation_receipts")
    op.drop_table("novel_template_images")
    op.drop_table("novel_templates")
