"""AI 用量迁移的结构、旧历史、权限目录与无损往返。"""

import json
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.permissions import PERMISSION_CODES
from app.models import Base
from tests.test_consistency_migration import _engine
from tests.test_setting_fields_migration import _config, _schema_contract

TABLES = ("ai_usage_tracking", "ai_model_prices", "ai_usage_budgets", "ai_usage_calls")
CACHED_COLUMNS = {
    "ai_model_prices": ("cached_input_price_nano",),
    "ai_usage_calls": ("cached_input_tokens", "price_cached_input_nano"),
}


def _schema_before_budget_notifications(reference, table):
    schema = _schema_contract(inspect(reference), table)
    if table == "ai_usage_budgets":
        # 0073 新增配置身份；0061–0063 的迁移契约仍需与其余全部字段一致。
        del schema["columns"]["instance_key"]
    return schema


def _without_cached_input(schema, table):
    for name in CACHED_COLUMNS.get(table, ()):
        del schema["columns"][name]
        del schema["check_constraints"][f"ck_{table}_{name}"]
    return schema


def _seed_priced_call(db):
    model_key, call_key = str(uuid4()), str(uuid4())
    db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
    db.execute(text(
        "INSERT INTO ai_configs(user_id,name,provider,api_key_enc,model,is_default,instance_key) "
        "VALUES(1,'模型','openai_compat','unused','fake',0,:key)"
    ), {"key": model_key})
    db.execute(text(
        "INSERT INTO ai_model_prices(config_instance_key,provider,model,model_type,currency,input_price_nano) "
        "VALUES(:key,'openai_compat','fake','text','CNY',999)"
    ), {"key": model_key})
    db.execute(text(
        "INSERT INTO ai_usage_calls(id,started_at,finished_at,actor_id,payer_id,provider,model,model_type,"
        "kind,status,sent,currency,price_input_nano,amount_nano,pricing_status) "
        "VALUES(:key,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1,1,'openai_compat','fake','text',"
        "'model.test','succeeded',TRUE,'CNY',31,16,'priced')"
    ), {"key": call_key})


def _permissions(db, system_key="super_admin"):
    raw = db.scalar(text("SELECT permissions FROM roles WHERE system_key=:key"), {"key": system_key})
    return json.loads(raw)


def test_0061_preserves_legacy_usage_and_migrates_super_admin(tmp_path):
    path = tmp_path / "ai-usage.db"
    config = _config(path)
    command.upgrade(config, "0060")
    migrated = _engine(path)
    reference = create_engine("sqlite://")
    try:
        with migrated.begin() as db:
            old_super = _permissions(db)
            old_content = _permissions(db, "content_admin")
            old_authorization_revision = db.scalar(text("SELECT revision FROM authorization_state WHERE id=1"))
            assert "ai_usage.read" not in old_super
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'老作品','','','','planning')"))
            db.execute(text(
                "INSERT INTO generation_logs(id,project_id,actor_user_id,kind,provider,model,params,messages,"
                "status,error,output_chars,duration_ms,prompt_tokens,log_instance_key) "
                "VALUES(1,1,1,'generate','openai','legacy','{}','[]','succeeded','',0,0,199,:log_key)"
            ), {"log_key": str(uuid4())})
        command.upgrade(config, "0061")
        expected_super = [code for code in PERMISSION_CODES if not code.startswith("publication_terms.")]

        Base.metadata.create_all(reference)
        for table in TABLES:
            expected = _without_cached_input(_schema_before_budget_notifications(reference, table), table)
            if table == "ai_model_prices":
                del expected["columns"]["off_peak"]
            if table == "ai_usage_calls":
                del expected["columns"]["price_period"]
                del expected["columns"]["pending_price_schedule"]
                del expected["check_constraints"]["ck_ai_usage_calls_price_period"]
            assert _schema_contract(inspect(migrated), table) == expected

        with migrated.begin() as db:
            assert _permissions(db) == expected_super
            assert _permissions(db, "content_admin") == old_content
            assert db.scalar(text("SELECT revision FROM authorization_state WHERE id=1")) == (
                old_authorization_revision + 1
            )
            assert db.scalar(text("SELECT prompt_tokens FROM generation_logs WHERE id=1")) == 199
            assert db.scalar(text("SELECT started_at FROM ai_usage_tracking WHERE id=1")) is not None
            assert db.scalar(text("SELECT COUNT(*) FROM ai_usage_calls")) == 0
            db.execute(text(
                "INSERT INTO roles(name, permissions) VALUES ('全站用量','[\"ai_usage.read\"]')"
            ))

        with pytest.raises(RuntimeError, match="仍有角色被授予 AI 用量查看权限"):
            command.downgrade(config, "0060")
        with migrated.begin() as db:
            db.execute(text("DELETE FROM roles WHERE name='全站用量'"))
            db.execute(text(
                "INSERT INTO ai_usage_budgets(user_id,currency,limit_nano,threshold_percent) VALUES(1,'CNY',900,80)"
            ))
        with pytest.raises(RuntimeError, match="仍有 AI 用量、价格或预算数据"):
            command.downgrade(config, "0060")
        with migrated.begin() as db:
            assert _permissions(db) == expected_super
            db.execute(text("DELETE FROM ai_usage_budgets"))

        command.downgrade(config, "0060")
        with migrated.begin() as db:
            assert _permissions(db) == old_super
            assert db.scalar(text("SELECT prompt_tokens FROM generation_logs WHERE id=1")) == 199
            assert not set(TABLES).intersection(inspect(db).get_table_names())
        command.upgrade(config, "0061")
        with migrated.begin() as db:
            assert _permissions(db) == expected_super
    finally:
        migrated.dispose()
        reference.dispose()


def test_0062_preserves_fixed_rates_and_protects_period_history(tmp_path):
    path = tmp_path / "usage-price-period.db"
    config = _config(path)
    command.upgrade(config, "0061")
    migrated = _engine(path)
    reference = create_engine("sqlite://")
    try:
        original = {table: _schema_contract(inspect(migrated), table) for table in TABLES}
        with migrated.begin() as db:
            _seed_priced_call(db)

        command.upgrade(config, "0062")
        Base.metadata.create_all(reference)
        for table in TABLES:
            assert _schema_contract(inspect(migrated), table) == _without_cached_input(
                _schema_before_budget_notifications(reference, table), table,
            )

        with migrated.begin() as db:
            assert db.execute(text("SELECT input_price_nano, off_peak FROM ai_model_prices")).one() == (999, None)
            assert db.execute(text(
                "SELECT price_input_nano, amount_nano, price_period, pending_price_schedule FROM ai_usage_calls"
            )).one() == (31, 16, None, None)
            with pytest.raises(IntegrityError):
                db.execute(text("UPDATE ai_usage_calls SET price_period = 'arbitrary'"))
            rule = json.dumps({"start": "23:00", "end": "07:00", "timezone": "Asia/Shanghai", "input_price_nano": 0})
            db.execute(text("UPDATE ai_model_prices SET off_peak = :rule"), {"rule": rule})

        with pytest.raises(RuntimeError, match="仍有分时定价或分时用量记录"):
            command.downgrade(config, "0061")
        with migrated.begin() as db:
            db.execute(text("UPDATE ai_model_prices SET off_peak = NULL"))
            db.execute(text("UPDATE ai_usage_calls SET price_period = 'off_peak'"))
        with pytest.raises(RuntimeError, match="仍有分时定价或分时用量记录"):
            command.downgrade(config, "0061")
        with migrated.begin() as db:
            db.execute(text("UPDATE ai_usage_calls SET price_period = 'fixed'"))

        command.downgrade(config, "0061")
        for table in TABLES:
            assert _schema_contract(inspect(migrated), table) == original[table]
        with migrated.connect() as db:
            assert db.scalar(text("SELECT amount_nano FROM ai_usage_calls")) == 16
            assert db.scalar(text("SELECT input_price_nano FROM ai_model_prices")) == 999
    finally:
        migrated.dispose()
        reference.dispose()


def test_0063_preserves_legacy_amounts_and_protects_cached_input_history(tmp_path):
    path = tmp_path / "usage-cache-price.db"
    config = _config(path)
    command.upgrade(config, "0062")
    migrated = _engine(path)
    reference = create_engine("sqlite://")
    try:
        original = {table: _schema_contract(inspect(migrated), table) for table in TABLES}
        with migrated.begin() as db:
            _seed_priced_call(db)
        command.upgrade(config, "0063")
        Base.metadata.create_all(reference)
        for table in TABLES:
            assert _schema_contract(inspect(migrated), table) == _schema_before_budget_notifications(reference, table)

        with migrated.begin() as db:
            assert db.execute(text(
                "SELECT price_input_nano, amount_nano, cached_input_tokens, price_cached_input_nano FROM ai_usage_calls"
            )).one() == (31, 16, None, None)
            for invalid in (
                "UPDATE ai_model_prices SET cached_input_price_nano = -1",
                "UPDATE ai_usage_calls SET cached_input_tokens = 0",
                "UPDATE ai_usage_calls SET price_cached_input_nano = -1",
            ):
                with pytest.raises(IntegrityError):
                    db.execute(text(invalid))
            db.execute(text("UPDATE ai_usage_calls SET input_tokens = 5, input_source = 'provider'"))

        for table, column, value in (
            ("ai_model_prices", "cached_input_price_nano", 0),
            ("ai_model_prices", "off_peak", json.dumps({"cached_input_price_nano": 0})),
            ("ai_usage_calls", "cached_input_tokens", 0),
            ("ai_usage_calls", "price_cached_input_nano", 0),
            ("ai_usage_calls", "pending_price_schedule", json.dumps({"cached_input_price_nano": 0})),
            ("ai_usage_calls", "pending_price_schedule", json.dumps({"off_peak": {"cached_input_price_nano": 0}})),
        ):
            with migrated.begin() as db:
                db.execute(text(f"UPDATE {table} SET {column} = :value"), {"value": value})
            with pytest.raises(RuntimeError, match="仍有缓存命中定价或用量记录"):
                command.downgrade(config, "0062")
            with migrated.begin() as db:
                assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0063"
                db.execute(text(f"UPDATE {table} SET {column} = NULL"))

        rule = {"start": "23:00", "end": "07:00", "cached_input_price_nano": None}
        schedule = {"input_price_nano": 50, "cached_input_price_nano": None, "off_peak": rule}
        multiple_windows = {**rule, "additional_windows": [{"start": "12:00", "end": "13:00"}]}
        multiple_schedule = {**schedule, "off_peak": multiple_windows}
        with migrated.begin() as db:
            db.execute(text("UPDATE ai_model_prices SET off_peak = :rule"), {"rule": json.dumps(multiple_windows)})
        with pytest.raises(RuntimeError, match="仍有多个空闲时段"):
            command.downgrade(config, "0062")
        with migrated.begin() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0063"
            assert json.loads(db.scalar(text("SELECT off_peak FROM ai_model_prices"))) == multiple_windows
            db.execute(text("UPDATE ai_model_prices SET off_peak = :rule"), {"rule": json.dumps(rule)})
            db.execute(text(
                "INSERT INTO ai_usage_calls(id,started_at,actor_id,payer_id,provider,model,model_type,"
                "kind,currency,pending_price_schedule) "
                "VALUES(:key,CURRENT_TIMESTAMP,1,1,'openai_compat','fake','text','model.test','CNY',:schedule)"
            ), {"key": str(uuid4()), "schedule": json.dumps(multiple_schedule)})
        with pytest.raises(RuntimeError, match="仍有多个空闲时段"):
            command.downgrade(config, "0062")
        with migrated.begin() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0063"
            assert json.loads(db.scalar(text(
                "SELECT pending_price_schedule FROM ai_usage_calls WHERE status = 'pending'"
            ))) == multiple_schedule
            db.execute(text("UPDATE ai_usage_calls SET pending_price_schedule = :schedule WHERE status = 'pending'"), {
                "schedule": json.dumps(schedule),
            })
        command.downgrade(config, "0062")
        for table in TABLES:
            assert _schema_contract(inspect(migrated), table) == original[table]
        with migrated.connect() as db:
            old_rule = {"start": "23:00", "end": "07:00"}
            assert json.loads(db.scalar(text("SELECT off_peak FROM ai_model_prices"))) == old_rule
            assert json.loads(db.scalar(text(
                "SELECT pending_price_schedule FROM ai_usage_calls WHERE status = 'pending'"
            ))) == {
                "input_price_nano": 50, "off_peak": old_rule,
            }
            assert db.scalar(text("SELECT amount_nano FROM ai_usage_calls WHERE status = 'succeeded'")) == 16
    finally:
        migrated.dispose()
        reference.dispose()
