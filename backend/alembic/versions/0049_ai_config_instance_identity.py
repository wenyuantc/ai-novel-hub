"""为模型配置添加不可复用、不可修改的实例身份。

Revision ID: 0049
Revises: 0048
"""

import json
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_BINDINGS = frozenset({"_project_model", "_resolved_project_model", "_model_binding", "_shared_embedding"})
_RECEIPTS = frozenset({
    "_analysis_pending", "_analysis_completion", "_chapter_claim", "_batch_attempt", "_batch_completion",
    "_batch_outcome_unknown", "_wizard_pending", "_wizard_completion", "rich_analysis", "_external_claim_id",
    "_external_task_key", "_external_execution", "_external_outcome_unknown", "_external_snapshot_mac",
})


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


def _backfill(db) -> None:
    """按主键分页随机回填，不使用配置内容、旧 id 或时间推断身份。"""
    last_id = None
    while True:
        statement = "SELECT id FROM ai_configs"
        params = {}
        if last_id is not None:
            statement += " WHERE id > :last_id"
            params["last_id"] = last_id
        rows = db.execute(sa.text(statement + " ORDER BY id LIMIT 500"), params).scalars().all()
        if not rows:
            return
        db.execute(sa.text("UPDATE ai_configs SET instance_key=:key WHERE id=:id"), [
            {"id": row_id, "key": str(uuid4())} for row_id in rows
        ])
        last_id = rows[-1]


def _identity_dependency(value, depth=0) -> bool:
    """只在持久载荷可证明不含新绑定及未决收据时允许降级。"""
    if depth >= 64:
        return True
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _RECEIPTS or key in ("config_instance_key", "_config_instance_key"):
                return True
            if key in _BINDINGS and (
                not isinstance(item, dict) or type(item.get("version")) is not int or item["version"] != 1
            ):
                return True
            if _identity_dependency(item, depth + 1):
                return True
        return False
    if isinstance(value, list):
        return any(_identity_dependency(item, depth + 1) for item in value)
    if isinstance(value, str):
        if not value.lstrip().startswith(("{", "[", '"')):
            return False
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            # 无法解析的转义结构可能隐藏 Unicode 编码的保留键，不能据此认定无依赖。
            return "\\" in value or any(key in value for key in _BINDINGS | _RECEIPTS | {"config_instance_key"})
        return _identity_dependency(parsed, depth + 1) if isinstance(parsed, (dict, list, str)) else False
    return False


def _has_dependencies(db) -> bool:
    task_rows = db.execute(sa.text(
        "SELECT status,input,checkpoint,result,output_text,error,execution_key,admin_cancel_operation_id FROM tasks"
    ))
    for row in task_rows:
        if (row[0] not in ("succeeded", "failed", "cancelled") or row[6] is not None or row[7] is not None
                or any(_identity_dependency(value) for value in row[1:6])):
            return True
    for row in db.execute(sa.text("SELECT status,arguments,preview,result,effects,error FROM agent_changes")):
        if row[0] not in ("proposed", "rejected", "executed", "failed", "undone") or any(
            _identity_dependency(value) for value in row[1:]
        ):
            return True
    for row in db.execute(sa.text("SELECT content,tool_calls FROM agent_messages")):
        if any(_identity_dependency(value) for value in row):
            return True
    for row in db.execute(sa.text("SELECT params,messages,error FROM generation_logs")):
        if any(_identity_dependency(value) for value in row):
            return True
    for status, payload in db.execute(sa.text("SELECT status,payload FROM scene_review_proposals")):
        if status == "pending" or _identity_dependency(payload):
            return True
    # 分块候选可能在任意位置断开 JSON 键，不能仅逐片搜索后声称无新绑定。
    if db.execute(sa.text("SELECT 1 FROM scene_review_payload_chunks LIMIT 1")).first() is not None:
        return True
    if db.execute(sa.text(
        "SELECT 1 FROM retrieval_chunks WHERE lower(embedding_key) LIKE 'shared:%' "
        "AND lower(embedding_key) NOT LIKE 'shared:v1:%' LIMIT 1"
    )).first() is not None:
        return True
    return db.execute(sa.text(
        "SELECT 1 FROM kb_documents WHERE status IN ('pending','parsing','indexing') "
        "OR index_task_key IS NOT NULL OR index_task_queued_at IS NOT NULL OR index_claim_key IS NOT NULL LIMIT 1"
    )).first() is not None


def upgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # sqlite3 legacy 模式先获取写事务；原位添加列，不重建有外键引用的配置表。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
        op.execute(
            "ALTER TABLE ai_configs ADD COLUMN instance_key VARCHAR(36) "
            "CONSTRAINT ck_ai_configs_instance_key_uuid4 "
            f"CHECK (instance_key IS NULL OR ({_uuid4_check('instance_key')}))"
        )
        _backfill(db)
        op.create_index("ux_ai_configs_instance_key", "ai_configs", ["instance_key"], unique=True)
        op.execute(
            "CREATE TRIGGER ai_configs_instance_key_generate AFTER INSERT ON ai_configs "
            "WHEN NEW.instance_key IS NULL BEGIN UPDATE ai_configs SET instance_key = ("
            "WITH candidate(key) AS MATERIALIZED (SELECT "
            "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
            "substr(lower(hex(randomblob(2))), 2, 3) || '-' || substr('89ab', (random() & 3) + 1, 1) || "
            "substr(lower(hex(randomblob(2))), 2, 3) || '-' || lower(hex(randomblob(6)))) "
            "SELECT candidate.key FROM candidate WHERE NOT EXISTS "
            "(SELECT 1 FROM ai_configs WHERE instance_key = candidate.key)) WHERE id = NEW.id; "
            "SELECT CASE WHEN (SELECT instance_key FROM ai_configs WHERE id = NEW.id) IS NULL "
            "THEN RAISE(ABORT, 'AI config instance identity required') END; END"
        )
        op.execute(
            "CREATE TRIGGER ai_configs_instance_key_immutable BEFORE UPDATE OF instance_key ON ai_configs "
            "WHEN OLD.instance_key IS NOT NULL AND NEW.instance_key IS NOT OLD.instance_key "
            "BEGIN SELECT RAISE(ABORT, 'AI config instance identity is immutable'); END"
        )
    elif db.dialect.name == "postgresql":
        op.add_column("ai_configs", sa.Column("instance_key", sa.String(36), nullable=True))
        _backfill(db)
        op.alter_column("ai_configs", "instance_key", existing_type=sa.String(36), nullable=False)
        op.create_check_constraint(
            op.f("ck_ai_configs_instance_key_uuid4"), "ai_configs",
            f"instance_key IS NULL OR ({_uuid4_check('instance_key')})",
        )
        op.create_index("ux_ai_configs_instance_key", "ai_configs", ["instance_key"], unique=True)
        op.execute(
            "CREATE OR REPLACE FUNCTION ai_configs_instance_key_generate_fn() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.instance_key IS NULL THEN NEW.instance_key := gen_random_uuid()::text; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER ai_configs_instance_key_generate BEFORE INSERT ON ai_configs "
            "FOR EACH ROW EXECUTE FUNCTION ai_configs_instance_key_generate_fn()"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION ai_configs_instance_key_immutable_fn() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.instance_key IS DISTINCT FROM OLD.instance_key THEN "
            "RAISE EXCEPTION 'AI config instance identity is immutable' USING ERRCODE = '23514'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER ai_configs_instance_key_immutable BEFORE UPDATE OF instance_key ON ai_configs "
            "FOR EACH ROW EXECUTE FUNCTION ai_configs_instance_key_immutable_fn()"
        )
    else:
        raise RuntimeError("模型配置身份迁移不支持当前数据库")


def downgrade() -> None:
    db = op.get_bind()
    if db.dialect.name == "sqlite":
        # 检查前就取得写锁，避免并发受理在扫描后提交新绑定。
        db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
    elif db.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE ai_configs, tasks, agent_changes, agent_messages, generation_logs, "
            "scene_review_proposals, scene_review_payload_chunks, retrieval_chunks, kb_documents "
            "IN ACCESS EXCLUSIVE MODE"
        )
    else:
        raise RuntimeError("模型配置身份降级不支持当前数据库")
    if _has_dependencies(db):
        raise RuntimeError("仍有模型实例绑定或未决执行事实，拒绝有损降级；请先排空并完整备份")
    if db.dialect.name == "sqlite":
        op.execute("DROP TRIGGER ai_configs_instance_key_generate")
        op.execute("DROP TRIGGER ai_configs_instance_key_immutable")
    else:
        op.execute("DROP TRIGGER ai_configs_instance_key_generate ON ai_configs")
        op.execute("DROP TRIGGER ai_configs_instance_key_immutable ON ai_configs")
        op.execute("DROP FUNCTION ai_configs_instance_key_generate_fn()")
        op.execute("DROP FUNCTION ai_configs_instance_key_immutable_fn()")
        op.drop_constraint(op.f("ck_ai_configs_instance_key_uuid4"), "ai_configs", type_="check")
    op.drop_index("ux_ai_configs_instance_key", table_name="ai_configs")
    op.drop_column("ai_configs", "instance_key")
