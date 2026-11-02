"""实例整库备份与四项可委派权限。

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

# 冻结本迁移的目录，不导入可继续演进的应用常量，也不改写0029。
OLD_CODES = [
    "dashboard.read", "users.read", "users.create", "users.status", "users.reset_password", "users.delete",
    "quotas.read", "quotas.update", "projects.read", "projects.edit", "projects.recycle", "recycle_bin.read",
    "recycle_bin.restore", "recycle_bin.purge", "tasks.read", "tasks.cancel", "audit.read", "audit.cleanup",
    "settings.read", "settings.update", "workshop.read", "workshop.approve", "workshop.reject", "workshop.publish",
    "workshop.update", "workshop.hide", "workshop.unhide", "workshop.delete", "global_skills.create",
    "global_skills.update", "global_skills.references", "global_skills.delete", "global_skills.restore_builtin",
]
BACKUP_CODES = ["backups.read", "backups.configure", "backups.run", "backups.download"]
NEW_CODES = OLD_CODES[:20] + BACKUP_CODES + OLD_CODES[20:]


def role_tables():
    return (
        sa.table("roles", sa.column("id", sa.Integer), sa.column("system_key", sa.String),
                 sa.column("permissions", sa.JSON), sa.column("revision", sa.Integer),
                 sa.column("updated_at", sa.DateTime(timezone=True))),
        sa.table("authorization_state", sa.column("id", sa.Integer), sa.column("revision", sa.Integer)),
    )


def upgrade():
    roles, state = role_tables()
    conn = op.get_bind()
    old_super = conn.execute(sa.select(roles).where(roles.c.system_key == "super_admin")).mappings().one()
    if set(old_super["permissions"]) != set(OLD_CODES):
        raise RuntimeError("内置超级管理员目录无效；请先修复授权状态")
    op.create_table(
        "backup_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("instance_key", sa.String(36), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("secrets_enc", sa.JSON(), nullable=False),
        sa.Column("next_scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("last_operation_id", sa.String(36), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_backup_settings_singleton")),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_backup_settings_positive_revision")),
    )
    op.create_table(
        "backup_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("execution_key", sa.String(36), nullable=False, unique=True),
        sa.Column("instance_key", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), unique=True),
        sa.Column("scheduled_date", sa.String(10), unique=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("actor_username", sa.String(64)),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("database_engine", sa.String(16), nullable=False),
        sa.Column("schema_revision", sa.String(64)),
        sa.Column("encrypted", sa.Boolean(), nullable=False),
        sa.Column("file_format", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active_slot", sa.Integer(), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)), sa.Column("error_message", sa.Text()),
        sa.Column("notification_status", sa.String(24), nullable=False),
        sa.Column("notification_error", sa.Text()),
        sa.Column("cleanup_status", sa.String(16), nullable=False),
        sa.Column("cleanup_error", sa.Text()),
        sa.CheckConstraint("source IN ('manual','scheduled')", name=op.f("ck_backup_runs_source_valid")),
        sa.CheckConstraint("status IN ('queued','running','succeeded','partial','failed','interrupted')",
                           name=op.f("ck_backup_runs_status_valid")),
        sa.CheckConstraint("(status IN ('queued','running') AND active_slot IS NOT NULL AND active_slot = 1) OR "
                           "(status NOT IN ('queued','running') AND active_slot IS NULL)",
                           name=op.f("ck_backup_runs_active_status")),
        sa.CheckConstraint("settings_revision >= 1", name=op.f("ck_backup_runs_positive_settings_revision")),
        sa.CheckConstraint("notification_status IN ('not_configured','pending','sending','sent','failed','unknown')",
                           name=op.f("ck_backup_runs_notification_status_valid")),
        sa.CheckConstraint("cleanup_status IN ('not_required','pending','succeeded','failed','unknown')",
                           name=op.f("ck_backup_runs_cleanup_status_valid")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_backup_runs_created_at_id", "backup_runs", ["created_at", "id"])
    op.create_table(
        "backup_artifacts", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("backup_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("destination", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("filename", sa.String(160)),
        sa.Column("size_bytes", sa.BigInteger()), sa.Column("checksum_sha256", sa.String(64)),
        sa.Column("locator", sa.JSON(), nullable=False), sa.Column("access_snapshot_enc", sa.JSON(), nullable=False),
        sa.Column("version_id", sa.String(1024)), sa.Column("multipart_upload_id", sa.String(1024)),
        sa.Column("error_code", sa.String(64)), sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "destination", name=op.f("uq_backup_artifacts_run_id")),
        sa.CheckConstraint("destination IN ('local','s3')", name=op.f("ck_backup_artifacts_destination_valid")),
        sa.CheckConstraint("status IN ('pending','ready','failed','unknown','deleting','deleted','missing')",
                           name=op.f("ck_backup_artifacts_status_valid")),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name=op.f("ck_backup_artifacts_size_nonnegative")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_backup_artifacts_destination_status", "backup_artifacts", ["destination", "status"])
    conn.execute(roles.update().where(roles.c.id == old_super["id"]).values(
        permissions=NEW_CODES, revision=roles.c.revision + 1, updated_at=sa.func.now(),
    ))
    conn.execute(state.update().where(state.c.id == 1).values(revision=state.c.revision + 1))


def downgrade():
    conn = op.get_bind()
    roles, state = role_tables()
    backup_permissions_used = any(
        set(row.permissions) & set(BACKUP_CODES)
        for row in conn.execute(sa.select(roles.c.permissions).where(
            sa.or_(roles.c.system_key.is_(None), roles.c.system_key != "super_admin"),
        ))
    )
    unsafe = (
        any(conn.scalar(sa.text("SELECT count(*) FROM " + table)) for table in (
            "backup_settings", "backup_runs", "backup_artifacts",
        ))
        or backup_permissions_used
        or conn.scalar(sa.text("SELECT count(*) FROM admin_audit_logs WHERE action IN "
                               "('backup.settings.update','backup.run.accept','backup.download.authorize')"))
    )
    if unsafe:
        raise RuntimeError("存在备份数据、新权限授权或审计，不能无损降级；请备份并前滚修复")
    conn.execute(roles.update().where(roles.c.system_key == "super_admin").values(
        permissions=OLD_CODES, revision=roles.c.revision + 1, updated_at=sa.func.now(),
    ))
    conn.execute(state.update().where(state.c.id == 1).values(revision=state.c.revision + 1))
    op.drop_table("backup_artifacts")
    op.drop_table("backup_runs")
    op.drop_table("backup_settings")
