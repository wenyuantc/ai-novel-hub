"""作品与系列协作、审计快照及生成记录的真实触发者。

原位扩展 generation_logs，避免重建被 chapter_versions 引用的 SQLite 父表。
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def _timestamps():
    return tuple(
        sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
        for name in ("created_at", "updated_at")
    )


def _create_collaborators(scope):
    table = f"{scope}_collaborators"
    parent = "projects" if scope == "project" else "series"
    parent_id = f"{scope}_id"
    columns = [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(parent_id, sa.Integer(), sa.ForeignKey(f"{parent}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), server_default="editor" if scope == "series" else None, nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
    ]
    if scope == "project":
        columns.append(sa.Column("allow_owner_ai", sa.Boolean(), server_default=sa.false(), nullable=False))
    columns.extend([
        sa.Column("invitation_id", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint(parent_id, "user_id"),
        sa.UniqueConstraint("invitation_id"),
        sa.CheckConstraint("role IN ('viewer', 'editor')" if scope == "project" else "role = 'editor'", name="role"),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'declined', 'revoked')", name="status"),
        sa.CheckConstraint("revision > 0", name="revision"),
        sa.CheckConstraint("invited_by_user_id IS NULL OR invited_by_user_id <> user_id", name="inviter_not_target"),
    ])
    op.create_table(table, *columns, sqlite_autoincrement=True)
    op.create_index(f"ix_{scope}_collaborators_user_status", table, ["user_id", "status"])


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite ADD COLUMN 支持带可空引用的内联外键，Alembic 的独立 ADD CONSTRAINT 不支持。
        op.execute(
            "ALTER TABLE generation_logs ADD COLUMN actor_user_id INTEGER "
            "CONSTRAINT fk_generation_logs_actor_user_id_users REFERENCES users(id) ON DELETE SET NULL"
        )
    else:
        op.add_column("generation_logs", sa.Column("actor_user_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_generation_logs_actor_user_id_users", "generation_logs", "users", ["actor_user_id"], ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_generation_logs_actor_user_id", "generation_logs", ["actor_user_id"])
    # 旧服务只能由作者产生项目记录，按迁移时作者回填；不以 task_id 推测新记录。
    op.execute(
        "UPDATE generation_logs SET actor_user_id = "
        "(SELECT user_id FROM projects WHERE projects.id = generation_logs.project_id)"
    )
    _create_collaborators("project")
    _create_collaborators("series")
    op.create_table(
        "collaboration_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("resource_label", sa.String(200)),
        sa.Column("invitation_id", sa.String(36)),
        sa.Column("actor_user_id", sa.Integer()),
        sa.Column("actor_username", sa.String(64)),
        sa.Column("target_user_id", sa.Integer()),
        sa.Column("target_username", sa.String(64)),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("result", sa.String(8), nullable=False),
        sa.Column("error_code", sa.String(32)),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.UniqueConstraint("operation_id"),
        sa.CheckConstraint("scope IN ('project', 'series')", name="scope"),
        sa.CheckConstraint("result IN ('success', 'failure')", name="result"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_collaboration_audit_scope_resource", "collaboration_audit_events",
        ["scope", "resource_id", "occurred_at", "id"],
    )
    op.create_index(
        "ix_collaboration_audit_actor_time", "collaboration_audit_events", ["actor_user_id", "occurred_at", "id"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("project_collaborators", "series_collaborators", "collaboration_audit_events"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("仍有协作授权或审计数据，拒绝有损降级")
    if connection.execute(sa.text(
        "SELECT 1 FROM generation_logs g LEFT JOIN projects p ON p.id = g.project_id "
        "WHERE g.actor_user_id IS NULL OR p.id IS NULL OR g.actor_user_id <> p.user_id LIMIT 1"
    )).first() is not None:
        raise RuntimeError("存在非作者或未知触发者的生成记录，拒绝有损降级")
    op.drop_table("collaboration_audit_events")
    op.drop_table("series_collaborators")
    op.drop_table("project_collaborators")
    op.drop_index("ix_generation_logs_actor_user_id", table_name="generation_logs")
    if connection.dialect.name == "sqlite":
        op.execute("ALTER TABLE generation_logs DROP COLUMN actor_user_id")
    else:
        op.drop_constraint("fk_generation_logs_actor_user_id_users", "generation_logs", type_="foreignkey")
        op.drop_column("generation_logs", "actor_user_id")
