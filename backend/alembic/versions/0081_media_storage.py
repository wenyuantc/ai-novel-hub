"""媒体对象、存储位置、配额和卷报告。"""

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_storage_locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("secrets_enc", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.CheckConstraint("kind IN ('local','s3')", name=op.f("ck_media_storage_locations_kind_valid")),
        sa.CheckConstraint(
            "state IN ('active','readonly','retired')", name=op.f("ck_media_storage_locations_state_valid")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_media_storage_locations_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_storage_locations")),
    )
    op.create_index(
        "uq_media_storage_locations_active",
        "media_storage_locations",
        ["state"],
        unique=True,
        sqlite_where=sa.text("state = 'active'"),
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "media_storage_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("default_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("last_operation_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("default_quota_bytes >= 0", name=op.f("ck_media_storage_settings_nonnegative_quota")),
        sa.CheckConstraint("id = 1", name=op.f("ck_media_storage_settings_singleton")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_media_storage_settings_positive_revision")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_storage_settings")),
    )
    op.create_table(
        "media_objects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("volume_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("plugin_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("mime", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("variant", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_token", sa.String(length=36), nullable=True),
        sa.Column("cleanup_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('image','video')", name=op.f("ck_media_objects_kind_valid")),
        sa.CheckConstraint("scope IN ('user','project')", name=op.f("ck_media_objects_scope_valid")),
        sa.CheckConstraint("status IN ('pending','ready','deleting')", name=op.f("ck_media_objects_status_valid")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_media_objects_nonnegative_size")),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["media_storage_locations.id"],
            name=op.f("fk_media_objects_location_id_media_storage_locations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name=op.f("fk_media_objects_owner_user_id_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["media_objects.id"],
            name=op.f("fk_media_objects_parent_id_media_objects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_media_objects_project_id_projects"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_objects")),
        sa.UniqueConstraint("location_id", "storage_key", name=op.f("uq_media_objects_location_id")),
        sa.UniqueConstraint("parent_id", "variant", name=op.f("uq_media_objects_parent_id")),
    )
    op.create_index("ix_media_objects_owner_status", "media_objects", ["owner_user_id", "status"], unique=False)
    op.create_index(op.f("ix_media_objects_project_id"), "media_objects", ["project_id"], unique=False)
    op.create_index("ix_media_objects_status_created", "media_objects", ["status", "created_at"], unique=False)
    op.create_table(
        "media_volume_reports",
        sa.Column("worker_key", sa.String(length=36), nullable=False),
        sa.Column("volume_id", sa.String(length=36), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_key", name=op.f("pk_media_volume_reports")),
    )

    db = op.get_bind()
    db.exec_driver_sql(
        "INSERT INTO media_storage_locations (kind,configuration,secrets_enc,state) VALUES ('local','{}','{}','active')"
    )
    db.exec_driver_sql("INSERT INTO media_storage_settings (id,revision,default_quota_bytes) VALUES (1,1,5368709120)")
    _update_permissions(db, add=True)


CODES = ("media_storage.read", "media_storage.configure")
ROLES = sa.table(
    "roles",
    sa.column("system_key", sa.String()),
    sa.column("permissions", sa.JSON()),
    sa.column("revision", sa.Integer()),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _update_permissions(db, *, add):
    super_admin = ROLES.c.system_key == "super_admin"
    values = list(db.scalars(sa.select(ROLES.c.permissions).where(super_admin)).one())
    if add:
        position = values.index("settings.read")
        values[position:position] = CODES
    else:
        values = [code for code in values if code not in CODES]
    db.execute(
        ROLES.update()
        .where(super_admin)
        .values(
            permissions=values,
            revision=ROLES.c.revision + 1,
            updated_at=sa.func.now(),
        )
    )
    db.exec_driver_sql("UPDATE authorization_state SET revision=revision+1 WHERE id=1")


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    else:
        db.exec_driver_sql(
            "LOCK TABLE authorization_state, roles, admin_audit_logs, media_objects, "
            "media_storage_locations, media_storage_settings, media_volume_reports IN ACCESS EXCLUSIVE MODE"
        )
    if db.exec_driver_sql("SELECT 1 FROM media_objects LIMIT 1").first():
        raise RuntimeError("仍有媒体文件，请先备份并确认处置方式")
    if (
        db.exec_driver_sql("SELECT 1 FROM media_storage_settings WHERE revision != 1 LIMIT 1").first()
        or db.exec_driver_sql(
            "SELECT 1 FROM media_storage_locations WHERE state != 'active' OR kind != 'local' LIMIT 1"
        ).first()
    ):
        raise RuntimeError("已有媒体存储设置，请先备份并确认处置方式")
    audit = sa.table("admin_audit_logs", sa.column("action", sa.String()), sa.column("summary", sa.JSON()))
    history = (
        sa.select(1)
        .select_from(audit)
        .where(
            sa.or_(
                audit.c.action == "media_storage.update",
                sa.and_(
                    audit.c.action.in_(("role.create", "role.update", "role.delete")),
                    sa.or_(
                        *(sa.cast(audit.c.summary, sa.Text).contains(f'"{code}"', autoescape=True) for code in CODES)
                    ),
                ),
            )
        )
        .limit(1)
    )
    if db.execute(history).first():
        raise RuntimeError("已有媒体存储审计，请先备份并确认处置方式")
    others = sa.or_(ROLES.c.system_key.is_(None), ROLES.c.system_key != "super_admin")
    if any(set(CODES).intersection(p) for p in db.scalars(sa.select(ROLES.c.permissions).where(others))):
        raise RuntimeError("仍有角色被授予媒体存储权限，请先移除授权")
    _update_permissions(db, add=False)
    op.drop_table("media_volume_reports")
    op.drop_table("media_objects")
    op.drop_table("media_storage_settings")
    op.drop_table("media_storage_locations")
