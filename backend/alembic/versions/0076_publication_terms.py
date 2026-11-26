"""发布前检查的全站和个人词表。"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

CODES = ["publication_terms.read", "publication_terms.update"]
ROLES = sa.table(
    "roles", sa.column("system_key", sa.String()), sa.column("permissions", sa.JSON()),
    sa.column("revision", sa.Integer()), sa.column("updated_at", sa.DateTime(timezone=True)),
)
AUTHORIZATION = sa.table("authorization_state", sa.column("id", sa.Integer()), sa.column("revision", sa.Integer()))


def _update_super_admin(db, *, add):
    super_admin = ROLES.c.system_key == "super_admin"
    permissions = list(db.scalars(sa.select(ROLES.c.permissions).where(super_admin)).one())
    if add:
        position = permissions.index("settings.update") + 1
        permissions[position:position] = CODES
    else:
        permissions = [permission for permission in permissions if permission not in CODES]
    db.execute(ROLES.update().where(super_admin).values(
        permissions=permissions, revision=ROLES.c.revision + 1, updated_at=sa.func.now(),
    ))
    db.execute(AUTHORIZATION.update().where(AUTHORIZATION.c.id == 1).values(revision=AUTHORIZATION.c.revision + 1))


def upgrade():
    op.create_table(
        "publication_terms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("terms", sa.JSON(), nullable=False),
        sa.CheckConstraint("(id = 0 AND owner_id IS NULL) OR "
                           "(id > 0 AND owner_id IS NOT NULL AND id = owner_id)", name="scope"),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
    )
    _update_super_admin(op.get_bind(), add=True)


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    else:
        db.exec_driver_sql("LOCK TABLE authorization_state, roles, publication_terms, admin_audit_logs "
                           "IN ACCESS EXCLUSIVE MODE")
    if db.exec_driver_sql("SELECT 1 FROM publication_terms LIMIT 1").first():
        raise RuntimeError("已有发布检查词表或修订历史，请先备份并确认处置方式")
    if db.exec_driver_sql("SELECT 1 FROM admin_audit_logs WHERE action='publication_terms.update' LIMIT 1").first():
        raise RuntimeError("已有全站词表管理操作日志，请先备份并确认处置方式")
    # 即使临时授权后来已撤销，旧应用仍会按旧权限目录校验授权日志的 before / after。
    audit = sa.table("admin_audit_logs", sa.column("action", sa.String()), sa.column("summary", sa.JSON()))
    role_history = sa.select(1).select_from(audit).where(
        audit.c.action.in_(("role.create", "role.update", "role.delete")),
        sa.or_(*(sa.cast(audit.c.summary, sa.Text).contains(f'"{code}"', autoescape=True) for code in CODES)),
    ).limit(1)
    if db.execute(role_history).first():
        raise RuntimeError("已有涉及发布检查权限的授权审计，请先备份并确认处置方式")
    others = sa.or_(ROLES.c.system_key.is_(None), ROLES.c.system_key != "super_admin")
    if any(set(CODES).intersection(value) for value in db.scalars(sa.select(ROLES.c.permissions).where(others))):
        raise RuntimeError("仍有角色被授予全站词表权限，请先移除授权")
    _update_super_admin(db, add=False)
    op.drop_table("publication_terms")
