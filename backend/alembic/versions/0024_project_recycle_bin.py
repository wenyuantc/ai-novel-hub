"""Project recycle bin and honest system audit actors.

Revision ID: 0024
Revises: 0023
"""

from contextlib import contextmanager

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


@contextmanager
def _sqlite_rebuild():
    """重建父表时不触发子表级联，完成后核实所有外键，恢复原 PRAGMA。"""
    bind = op.get_bind()
    sequences = {}
    if bind.dialect.name == "sqlite":
        sequences = dict(bind.exec_driver_sql(
            "SELECT name, seq FROM sqlite_sequence WHERE name IN ('projects','admin_audit_logs')"
        ).all())
    enabled = bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    if enabled:
        with op.get_context().autocommit_block():
            bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        yield
        for name, sequence in sequences.items():
            bind.execute(sa.text(
                "UPDATE sqlite_sequence SET seq=CASE WHEN seq < :seq THEN :seq ELSE seq END WHERE name=:name"
            ), {"seq": sequence, "name": name})
        if bind.dialect.name == "sqlite" and bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise RuntimeError("作品回收站迁移后外键校验失败")
    finally:
        if enabled:
            with op.get_context().autocommit_block():
                bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade():
    with _sqlite_rebuild():
        with op.batch_alter_table("projects", table_kwargs={"sqlite_autoincrement": True}) as batch:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
            batch.add_column(sa.Column("last_deleted_at", sa.DateTime(timezone=True), nullable=True))
            batch.add_column(sa.Column("deleted_by_user_id", sa.Integer(), nullable=True))
            batch.add_column(sa.Column("deleted_by_username", sa.String(64), nullable=True))
            batch.add_column(sa.Column("deletion_started_at", sa.DateTime(timezone=True), nullable=True))
            batch.add_column(sa.Column("deletion_token", sa.String(36), nullable=True))
            batch.drop_constraint("fk_projects_user_id_users", type_="foreignkey")
            batch.create_foreign_key("fk_projects_user_id_users", "users", ["user_id"], ["id"], ondelete="RESTRICT")
            batch.create_check_constraint(op.f("ck_projects_deletion_state"), "deleted_at IS NULL OR deletion_started_at IS NULL")
            batch.create_check_constraint(
                op.f("ck_projects_deletion_intent"), "(deletion_started_at IS NULL) = (deletion_token IS NULL)"
            )
            batch.create_index("ix_projects_deleted_at_id", ["deleted_at", "id"])
        with op.batch_alter_table("admin_audit_logs", table_kwargs={"sqlite_autoincrement": True}) as batch:
            batch.alter_column("actor_user_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT id FROM projects WHERE deleted_at IS NOT NULL OR deletion_started_at IS NOT NULL OR last_deleted_at IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("存在回收站或删除中作品，拒绝降级导致作品重新可见")
    rows = bind.execute(sa.text("SELECT action, summary, actor_user_id FROM admin_audit_logs")).mappings()
    import json
    for row in rows:
        summary = row["summary"]
        if isinstance(summary, str):
            summary = json.loads(summary)
        if row["actor_user_id"] is None or row["action"] in ("project.restore", "project.purge", "project.expire") or (
            isinstance(summary, dict) and summary.get("lifecycle_action") is not None
        ):
            raise RuntimeError("存在新生命周期审计记录，拒绝破坏历史日志的降级")
    with _sqlite_rebuild():
        with op.batch_alter_table("projects", table_kwargs={"sqlite_autoincrement": False}) as batch:
            batch.drop_index("ix_projects_deleted_at_id")
            batch.drop_constraint(op.f("ck_projects_deletion_state"), type_="check")
            batch.drop_constraint(op.f("ck_projects_deletion_intent"), type_="check")
            batch.drop_constraint("fk_projects_user_id_users", type_="foreignkey")
            batch.create_foreign_key("fk_projects_user_id_users", "users", ["user_id"], ["id"], ondelete="CASCADE")
            for column in ("deleted_at", "last_deleted_at", "deleted_by_user_id", "deleted_by_username", "deletion_started_at", "deletion_token"):
                batch.drop_column(column)
        with op.batch_alter_table("admin_audit_logs", table_kwargs={"sqlite_autoincrement": True}) as batch:
            batch.alter_column("actor_user_id", existing_type=sa.Integer(), nullable=False)
