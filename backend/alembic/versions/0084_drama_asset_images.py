"""短剧造型图片与生成记录。"""

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "drama_asset_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("owner_type", sa.String(length=8), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("media_object_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=12), server_default="generated", nullable=False),
        sa.Column("status", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("prompt", sa.Text(), server_default="", nullable=False),
        sa.Column("reference_image_ids", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column("is_selected", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(status = 'ready') = (media_object_id IS NOT NULL)", name=op.f("ck_drama_asset_images_ready_object")
        ),
        sa.CheckConstraint("NOT is_selected OR status = 'ready'", name=op.f("ck_drama_asset_images_selected_ready")),
        sa.CheckConstraint(
            "owner_type IN ('look','scene','prop') AND owner_id > 0", name=op.f("ck_drama_asset_images_owner")
        ),
        sa.CheckConstraint("source IN ('generated','uploaded','novel')", name=op.f("ck_drama_asset_images_source")),
        sa.CheckConstraint("status IN ('pending','ready','failed')", name=op.f("ck_drama_asset_images_status")),
        sa.ForeignKeyConstraint(
            ["media_object_id"],
            ["media_objects.id"],
            name=op.f("fk_drama_asset_images_media_object_id_media_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["drama_series.id"],
            name=op.f("fk_drama_asset_images_series_id_drama_series"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_drama_asset_images_task_id_tasks"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drama_asset_images")),
        sa.UniqueConstraint("media_object_id", name=op.f("uq_drama_asset_images_media_object_id")),
        sa.UniqueConstraint("task_id", "owner_type", "owner_id", name=op.f("uq_drama_asset_images_task_id")),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_drama_asset_images_owner", "drama_asset_images", ["series_id", "owner_type", "owner_id"], unique=False
    )
    op.create_index(
        "uq_drama_asset_images_selected",
        "drama_asset_images",
        ["owner_type", "owner_id"],
        unique=True,
        sqlite_where=sa.text("is_selected"),
        postgresql_where=sa.text("is_selected"),
    )


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        db.exec_driver_sql("LOCK TABLE drama_asset_images IN ACCESS EXCLUSIVE MODE")
    if db.exec_driver_sql("SELECT 1 FROM drama_asset_images LIMIT 1").first():
        raise RuntimeError("仍有短剧资产图，请先备份并确认处置方式")
    op.drop_table("drama_asset_images")
