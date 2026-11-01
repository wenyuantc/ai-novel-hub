"""固定角色、授权序列点与用户资源配额。

Revision ID: 0029
Revises: 0028
"""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    roles = op.create_table(
        "roles", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("system_key", sa.String(32), unique=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_roles_revision_positive")),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "user_roles", sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True),
    )
    state = op.create_table(
        "authorization_state", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name=op.f("ck_authorization_state_singleton")),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_authorization_state_revision_nonnegative")),
    )
    op.create_table(
        "user_quotas", sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("project_mode", sa.String(16), nullable=False, server_default="inherit"),
        sa.Column("project_value", sa.Integer()),
        sa.Column("task_mode", sa.String(16), nullable=False, server_default="inherit"),
        sa.Column("task_value", sa.Integer()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_user_quotas_revision_positive")),
        sa.CheckConstraint("(project_mode = 'limited' AND project_value IS NOT NULL AND project_value >= 0) OR "
                           "(project_mode IN ('inherit', 'unlimited') AND project_value IS NULL)",
                           name=op.f("ck_user_quotas_project_config")),
        sa.CheckConstraint("(task_mode = 'limited' AND task_value IS NOT NULL AND task_value >= 0) OR "
                           "(task_mode IN ('inherit', 'unlimited') AND task_value IS NULL)",
                           name=op.f("ck_user_quotas_task_config")),
    )
    op.bulk_insert(state, [{"id": 1, "revision": 0}])
    op.bulk_insert(roles, [{'system_key': 'super_admin', 'name': '超级管理员', 'description': '全部管理能力；角色与授权不可委派', 'permissions': ['dashboard.read', 'users.read', 'users.create', 'users.status', 'users.reset_password', 'users.delete', 'quotas.read', 'quotas.update', 'projects.read', 'projects.edit', 'projects.recycle', 'recycle_bin.read', 'recycle_bin.restore', 'recycle_bin.purge', 'tasks.read', 'tasks.cancel', 'audit.read', 'audit.cleanup', 'settings.read', 'settings.update', 'workshop.read', 'workshop.approve', 'workshop.reject', 'workshop.publish', 'workshop.update', 'workshop.hide', 'workshop.unhide', 'workshop.delete', 'global_skills.create', 'global_skills.update', 'global_skills.references', 'global_skills.delete', 'global_skills.restore_builtin'], 'revision': 1}, {'system_key': 'content_admin', 'name': '内容管理员', 'description': '全站人工作品维护、回收恢复与工坊管理', 'permissions': ['projects.read', 'projects.edit', 'projects.recycle', 'workshop.read', 'workshop.approve', 'workshop.reject', 'workshop.publish', 'workshop.update', 'workshop.hide', 'workshop.unhide', 'workshop.delete', 'recycle_bin.read', 'recycle_bin.restore'], 'revision': 1}, {'system_key': 'reviewer', 'name': '审核员', 'description': '工坊投稿审核与上下架', 'permissions': ['workshop.read', 'workshop.approve', 'workshop.reject', 'workshop.hide', 'workshop.unhide'], 'revision': 1}, {'system_key': 'user', 'name': '普通用户', 'description': '个人资源与公开功能，无后台管理能力', 'permissions': [], 'revision': 1}])


def downgrade():
    conn = op.get_bind()
    unsafe = (
        conn.scalar(sa.text("SELECT count(*) FROM roles WHERE system_key IS NULL"))
        or conn.scalar(sa.text("SELECT count(*) FROM user_roles"))
        or conn.scalar(sa.text("SELECT count(*) FROM user_quotas"))
        or conn.scalar(sa.text("SELECT count(*) FROM admin_audit_logs WHERE action IN "
                              "('role.create','role.update','role.delete','user.roles.update',"
                              "'user.roles.bootstrap','user.quotas.update')"))
    )
    if unsafe:
        raise RuntimeError("存在新角色、分配、配额或审计，不能无损降级；请备份并前滚修复")
    op.drop_table("user_quotas")
    op.drop_table("user_roles")
    op.drop_table("roles")
    op.drop_table("authorization_state")
