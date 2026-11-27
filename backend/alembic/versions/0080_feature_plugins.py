"""内置功能插件的全站启用状态。"""

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


CORE_KINDS_0079 = (
    "chapter.generate",
    "chapter.polish",
    "wizard.outline",
    "wizard.characters",
    "wizard.world",
    "character.generate",
    "organization.generate",
    "relations.parse",
    "relations.generate",
    "career.generate",
    "wizard.careers",
    "chapter.analyze",
    "chapter.revise",
    "outline.continue",
    "outline.expand",
    "chapter.batch",
    "kb.index",
    "book.import",
    "agent.turn",
    "consistency.check",
    "consistency.impact",
    "scene.restructure",
)
CODES = ["plugins.read", "plugins.update"]
ROLES = sa.table(
    "roles",
    sa.column("system_key", sa.String()),
    sa.column("permissions", sa.JSON()),
    sa.column("revision", sa.Integer()),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
AUTHORIZATION = sa.table("authorization_state", sa.column("id", sa.Integer()), sa.column("revision", sa.Integer()))


def _update_super_admin(db, *, add):
    super_admin = ROLES.c.system_key == "super_admin"
    permissions = list(db.scalars(sa.select(ROLES.c.permissions).where(super_admin)).one())
    if add:
        position = permissions.index("settings.read")
        permissions[position:position] = CODES
    else:
        permissions = [permission for permission in permissions if permission not in CODES]
    db.execute(
        ROLES.update()
        .where(super_admin)
        .values(
            permissions=permissions,
            revision=ROLES.c.revision + 1,
            updated_at=sa.func.now(),
        )
    )
    db.execute(AUTHORIZATION.update().where(AUTHORIZATION.c.id == 1).values(revision=AUTHORIZATION.c.revision + 1))


def upgrade():
    op.create_table(
        "feature_plugin_states",
        sa.Column("plugin_id", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_operation_id", sa.String(36)),
        sa.CheckConstraint("revision >= 1", name="positive_revision"),
    )
    _update_super_admin(op.get_bind(), add=True)


def downgrade():
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        db.exec_driver_sql("UPDATE alembic_version SET version_num=version_num")
    else:
        db.exec_driver_sql(
            "LOCK TABLE authorization_state, roles, tasks, feature_plugin_states, admin_audit_logs "
            "IN ACCESS EXCLUSIVE MODE"
        )
    if db.exec_driver_sql(
        "SELECT 1 FROM admin_audit_logs WHERE action IN ('feature_plugin.enable','feature_plugin.disable') LIMIT 1"
    ).first():
        raise RuntimeError("已有功能插件管理操作日志，请先备份并确认处置方式")
    # 即使临时授权后来已撤销，旧应用仍会按旧权限目录校验授权日志的 before / after。
    audit = sa.table("admin_audit_logs", sa.column("action", sa.String()), sa.column("summary", sa.JSON()))
    tasks = sa.table("tasks", sa.column("kind", sa.String()))
    if db.execute(sa.select(1).select_from(tasks).where(tasks.c.kind.not_in(CORE_KINDS_0079)).limit(1)).first():
        raise RuntimeError("仍有旧版本无法读取的任务类型，请先备份并确认处置方式")
    cancel_kind = audit.c.summary["task_kind"].as_string()
    cancel_history = sa.select(1).select_from(audit).where(
        audit.c.action == "task.cancel_request", cancel_kind.not_in((*CORE_KINDS_0079, "unknown")),
    ).limit(1)
    if db.execute(cancel_history).first():
        raise RuntimeError("仍有旧版本无法读取的任务取消审计，请先备份并确认处置方式")
    if db.exec_driver_sql("SELECT 1 FROM feature_plugin_states LIMIT 1").first():
        raise RuntimeError("仍有功能插件状态或修订，请先备份并确认处置方式")
    role_history = (
        sa.select(1)
        .select_from(audit)
        .where(
            audit.c.action.in_(("role.create", "role.update", "role.delete")),
            sa.or_(*(sa.cast(audit.c.summary, sa.Text).contains(f'"{code}"', autoescape=True) for code in CODES)),
        )
        .limit(1)
    )
    if db.execute(role_history).first():
        raise RuntimeError("已有涉及插件权限的授权审计，请先备份并确认处置方式")
    others = sa.or_(ROLES.c.system_key.is_(None), ROLES.c.system_key != "super_admin")
    if any(set(CODES).intersection(value) for value in db.scalars(sa.select(ROLES.c.permissions).where(others))):
        raise RuntimeError("仍有角色被授予功能插件权限，请先移除授权")
    _update_super_admin(db, add=False)
    op.drop_table("feature_plugin_states")
