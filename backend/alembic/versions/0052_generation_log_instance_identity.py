"""为生成记录添加服务端不可变的日志实例键与模型配置实例键。

Revision ID: 0052
Revises: 0051

- 旧行两个键保持 NULL，不按整数 id、actor 或时间补认身份；原位加列，不重建有外键引用的 generation_logs。
- 新插入必须携带规范小写 UUIDv4 的 log_instance_key（ORM 由服务端生成；裸 SQL 缺失时被拒绝），全局唯一。
- 两个键写入后不可修改，NULL 旧行也不能补写。
- 存在带实例键的记录时拒绝降级，避免旧代码丢失日志身份后把复用 ID 认作原记录。
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

_LOG_CHECK = "ck_generation_logs_log_instance_key_uuid4"
_MODEL_CHECK = "ck_generation_logs_model_config_instance_key_uuid4"
_INDEX = "ux_generation_logs_log_instance_key"


def _uuid4_check(column: str) -> str:
    hexadecimal = f"replace({column}, '-', '')"
    remainder = hexadecimal
    for char in "0123456789abcdef":
        remainder = f"replace({remainder}, '{char}', '')"
    return (
        f"CAST({column} AS TEXT) = {column} AND length({column}) = 36 "
        f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
        f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
        f"AND substr({column}, 15, 1) = '4' AND substr({column}, 20, 1) IN ('8', '9', 'a', 'b') "
        f"AND length({hexadecimal}) = 32 AND {remainder} = ''"
    )


def upgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # sqlite3 legacy 模式先获取写事务；原位添加列，不重建有外键引用的日志表。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
        op.execute(
            "ALTER TABLE generation_logs ADD COLUMN log_instance_key VARCHAR(36) "
            f"CONSTRAINT {_LOG_CHECK} "
            f"CHECK (log_instance_key IS NULL OR ({_uuid4_check('log_instance_key')}))"
        )
        op.execute(
            "ALTER TABLE generation_logs ADD COLUMN model_config_instance_key VARCHAR(36) "
            f"CONSTRAINT {_MODEL_CHECK} "
            f"CHECK (model_config_instance_key IS NULL OR ({_uuid4_check('model_config_instance_key')}))"
        )
        op.create_index(_INDEX, "generation_logs", ["log_instance_key"], unique=True)
        op.execute(
            "CREATE TRIGGER generation_logs_log_instance_key_required BEFORE INSERT ON generation_logs "
            "WHEN NEW.log_instance_key IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'generation log instance identity required'); END"
        )
        op.execute(
            "CREATE TRIGGER generation_logs_instance_keys_immutable BEFORE UPDATE OF "
            "log_instance_key, model_config_instance_key ON generation_logs "
            "WHEN NEW.log_instance_key IS NOT OLD.log_instance_key "
            "OR NEW.model_config_instance_key IS NOT OLD.model_config_instance_key "
            "BEGIN SELECT RAISE(ABORT, 'generation log instance identity is immutable'); END"
        )
    elif db.dialect.name == "postgresql":
        op.add_column("generation_logs", sa.Column("log_instance_key", sa.String(36), nullable=True))
        op.add_column("generation_logs", sa.Column("model_config_instance_key", sa.String(36), nullable=True))
        op.create_check_constraint(
            op.f(_LOG_CHECK), "generation_logs",
            f"log_instance_key IS NULL OR ({_uuid4_check('log_instance_key')})",
        )
        op.create_check_constraint(
            op.f(_MODEL_CHECK), "generation_logs",
            f"model_config_instance_key IS NULL OR ({_uuid4_check('model_config_instance_key')})",
        )
        op.create_index(_INDEX, "generation_logs", ["log_instance_key"], unique=True)
        op.execute(
            "CREATE OR REPLACE FUNCTION generation_logs_log_instance_key_required_fn() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN IF NEW.log_instance_key IS NULL THEN "
            "RAISE EXCEPTION 'generation log instance identity required' USING ERRCODE = '23502'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER generation_logs_log_instance_key_required BEFORE INSERT ON generation_logs "
            "FOR EACH ROW EXECUTE FUNCTION generation_logs_log_instance_key_required_fn()"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION generation_logs_instance_keys_immutable_fn() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN IF NEW.log_instance_key IS DISTINCT FROM OLD.log_instance_key "
            "OR NEW.model_config_instance_key IS DISTINCT FROM OLD.model_config_instance_key THEN "
            "RAISE EXCEPTION 'generation log instance identity is immutable' USING ERRCODE = '23514'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER generation_logs_instance_keys_immutable BEFORE UPDATE OF "
            "log_instance_key, model_config_instance_key ON generation_logs "
            "FOR EACH ROW EXECUTE FUNCTION generation_logs_instance_keys_immutable_fn()"
        )
    else:
        raise RuntimeError("生成记录实例身份迁移不支持当前数据库")


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # 检查前就取得写锁，避免扫描后有新日志提交。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute("LOCK TABLE generation_logs IN ACCESS EXCLUSIVE MODE")
    else:
        raise RuntimeError("生成记录实例身份降级不支持当前数据库")
    if db.execute(sa.text(
        "SELECT 1 FROM generation_logs WHERE log_instance_key IS NOT NULL "
        "OR model_config_instance_key IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("仍有带实例身份的生成记录，拒绝有损降级；请先完整备份并确认处置方式")
    if db.dialect.name == "sqlite":
        op.execute("DROP TRIGGER generation_logs_log_instance_key_required")
        op.execute("DROP TRIGGER generation_logs_instance_keys_immutable")
    else:
        op.execute("DROP TRIGGER generation_logs_log_instance_key_required ON generation_logs")
        op.execute("DROP TRIGGER generation_logs_instance_keys_immutable ON generation_logs")
        op.execute("DROP FUNCTION generation_logs_log_instance_key_required_fn()")
        op.execute("DROP FUNCTION generation_logs_instance_keys_immutable_fn()")
        op.drop_constraint(op.f(_MODEL_CHECK), "generation_logs", type_="check")
        op.drop_constraint(op.f(_LOG_CHECK), "generation_logs", type_="check")
    op.drop_index(_INDEX, table_name="generation_logs")
    op.drop_column("generation_logs", "model_config_instance_key")
    op.drop_column("generation_logs", "log_instance_key")
