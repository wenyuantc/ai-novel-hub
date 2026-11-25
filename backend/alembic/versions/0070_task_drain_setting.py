"""声明排空设置的持久格式并保护降级。"""

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 稀疏 JSON 保留原值，缺少排空键的旧存量记录直接采用部署基线。
    pass


def downgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("降级前需要连接数据库检查排空设置及审计")
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # 保持读取保护和 Alembic 版本切换处在同一写事务。
        db.exec_driver_sql("UPDATE alembic_version SET version_num = version_num")
        overrides = """
            SELECT 1 FROM system_settings
            WHERE json_type(overrides, '$.task_admission_closed') IS NOT NULL
            LIMIT 1
        """
        audit = """
            SELECT 1 FROM admin_audit_logs AS log, json_each(log.summary, '$.settings_changes') AS item
            WHERE log.action = 'system_settings.update'
              AND CASE WHEN item.type = 'object' THEN json_extract(item.value, '$.key') END
                  = 'task_admission_closed'
            LIMIT 1
        """
    elif db.dialect.name == "postgresql":
        db.exec_driver_sql("LOCK TABLE system_settings, admin_audit_logs IN SHARE MODE")
        overrides = """
            SELECT 1 FROM system_settings
            WHERE overrides -> 'task_admission_closed' IS NOT NULL
            LIMIT 1
        """
        audit = """
            SELECT 1 FROM admin_audit_logs WHERE action = 'system_settings.update'
              AND CAST(summary AS jsonb) @> CAST('{"settings_changes":[{"key":"task_admission_closed"}]}' AS jsonb)
            LIMIT 1
        """
    else:
        raise RuntimeError("排空设置迁移不支持当前数据库")
    if db.exec_driver_sql(overrides).first() is not None:
        raise RuntimeError("仍有排空设置覆盖，拒绝降级")
    if db.exec_driver_sql(audit).first() is not None:
        raise RuntimeError("仍有排空设置审计，拒绝降级")
