"""设定缩略图的不可变资源和当前绑定。"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_organizations_project_id_id", "organizations", ["project_id", "id"], unique=True)
    op.create_index("uq_careers_project_id_id", "careers", ["project_id", "id"], unique=True)
    op.create_index("uq_series_entries_series_id_id", "series_entries", ["series_id", "id"], unique=True)
    op.create_table(
        "setting_image_resources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("series.id", ondelete="CASCADE")),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("(project_id IS NULL) <> (series_id IS NULL)",
                           name=op.f("ck_setting_image_resources_one_scope")),
        sa.CheckConstraint("width > 0 AND height > 0 AND byte_size > 0",
                           name=op.f("ck_setting_image_resources_positive_size")),
        sa.UniqueConstraint("project_id", "id", name="uq_setting_image_resources_project_id"),
        sa.UniqueConstraint("series_id", "id", name="uq_setting_image_resources_series_id"),
    )
    op.create_index("ix_setting_image_resources_project_id", "setting_image_resources", ["project_id"])
    op.create_index("ix_setting_image_resources_series_id", "setting_image_resources", ["series_id"])
    op.create_table(
        "setting_thumbnail_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("image_resource_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("series.id", ondelete="CASCADE")),
        sa.Column("character_id", sa.Integer()),
        sa.Column("world_setting_id", sa.Integer()),
        sa.Column("organization_id", sa.Integer()),
        sa.Column("career_id", sa.Integer()),
        sa.Column("series_entry_id", sa.Integer()),
        sa.CheckConstraint(
            "(project_id IS NOT NULL AND series_id IS NULL AND series_entry_id IS NULL AND "
            "((CASE WHEN character_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN world_setting_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN organization_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN career_id IS NOT NULL THEN 1 ELSE 0 END)) = 1) OR "
            "(project_id IS NULL AND series_id IS NOT NULL AND series_entry_id IS NOT NULL AND "
            "character_id IS NULL AND world_setting_id IS NULL AND organization_id IS NULL AND career_id IS NULL)",
            name=op.f("ck_setting_thumbnail_bindings_one_subject"),
        ),
        sa.ForeignKeyConstraint(["project_id", "character_id"], ["characters.project_id", "characters.id"],
                                name="fk_thumbnail_character", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "world_setting_id"], ["world_settings.project_id", "world_settings.id"],
                                name="fk_thumbnail_world", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "organization_id"], ["organizations.project_id", "organizations.id"],
                                name="fk_thumbnail_organization", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "career_id"], ["careers.project_id", "careers.id"],
                                name="fk_thumbnail_career", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["series_id", "series_entry_id"], ["series_entries.series_id", "series_entries.id"],
                                name="fk_thumbnail_series_entry", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "image_resource_id"],
                                ["setting_image_resources.project_id", "setting_image_resources.id"],
                                name="fk_thumbnail_project_resource", deferrable=True, initially="DEFERRED"),
        sa.ForeignKeyConstraint(["series_id", "image_resource_id"],
                                ["setting_image_resources.series_id", "setting_image_resources.id"],
                                name="fk_thumbnail_series_resource", deferrable=True, initially="DEFERRED"),
        sa.UniqueConstraint("character_id", name="uq_setting_thumbnail_bindings_character_id"),
        sa.UniqueConstraint("world_setting_id", name="uq_setting_thumbnail_bindings_world_setting_id"),
        sa.UniqueConstraint("organization_id", name="uq_setting_thumbnail_bindings_organization_id"),
        sa.UniqueConstraint("career_id", name="uq_setting_thumbnail_bindings_career_id"),
        sa.UniqueConstraint("series_entry_id", name="uq_setting_thumbnail_bindings_series_entry_id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_setting_thumbnail_bindings_project_id", "setting_thumbnail_bindings", ["project_id"])
    op.create_index("ix_setting_thumbnail_bindings_series_id", "setting_thumbnail_bindings", ["series_id"])


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE setting_image_resources, setting_thumbnail_bindings IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("缩略图降级不支持当前数据库")
    if db.execute(sa.text("SELECT 1 FROM setting_image_resources LIMIT 1")).first() is not None:
        raise RuntimeError("仍有缩略图资源，拒绝有损降级")
    if db.execute(sa.text("SELECT 1 FROM setting_thumbnail_bindings LIMIT 1")).first() is not None:
        raise RuntimeError("仍有缩略图绑定，拒绝有损降级")
    op.drop_table("setting_thumbnail_bindings")
    op.drop_table("setting_image_resources")
    op.drop_index("uq_series_entries_series_id_id", table_name="series_entries")
    op.drop_index("uq_careers_project_id_id", table_name="careers")
    op.drop_index("uq_organizations_project_id_id", table_name="organizations")
