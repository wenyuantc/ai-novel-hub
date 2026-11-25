"""0049 配置实例 UUID：原位回填、约束、备份以及降级依赖。"""

import json
import threading
import time
from uuid import UUID, uuid1, uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from app.models import AIConfig, Base
from app.services.backups.database import _sqlite_snapshot
from tests.helpers import create_ai_config, create_project, register
from tests.test_setting_fields_migration import _config, _migration_fk_on_connect


@pytest.fixture(autouse=True)
def _foreign_keys(monkeypatch):
    _migration_fk_on_connect(monkeypatch)


def _seed(conn):
    conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
    conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                      "VALUES(1,1,'旧书','','','','writing')"))
    conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                      "VALUES(1,1,1,'旧章','正文','done',2)"))
    conn.execute(text("INSERT INTO ai_configs(id,user_id,name,provider,api_key_enc,model,is_default) "
                      "VALUES(:id,1,:name,'openai_compat','密文','same-model',0)"), [
        {"id": i, "name": f"旧配置{i}"} for i in range(1, 504)
    ])
    conn.execute(text("CREATE TABLE config_references (id INTEGER PRIMARY KEY, "
                      "config_id INTEGER REFERENCES ai_configs(id) ON DELETE CASCADE)"))
    conn.execute(text("INSERT INTO config_references(id,config_id) VALUES(1,1)"))


def _canonical(value):
    assert str(UUID(value)) == value and UUID(value).version == 4


def test_0049_sqlite_backfill_constraints_and_backup(tmp_path):
    path, backup = tmp_path / "old.db", tmp_path / "snapshot.db"
    config, engine = _config(path), create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0048")
        with engine.begin() as conn:
            _seed(conn)
            old_rows = conn.execute(text("SELECT * FROM ai_configs ORDER BY id")).mappings().all()
            old_fks = inspect(conn).get_foreign_keys("ai_configs")
        command.upgrade(config, "0049")
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM ai_configs ORDER BY id")).mappings().all()
            assert [{name: row[name] for name in old_rows[0]} for row in rows] == [dict(row) for row in old_rows]
            keys = [row["instance_key"] for row in rows]
            assert len(set(keys)) == len(keys) == 503
            for key in keys:
                _canonical(key)
            assert inspect(conn).get_foreign_keys("ai_configs") == old_fks
            assert conn.scalar(text("SELECT config_id FROM config_references WHERE id=1")) == 1
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        # 任意 SQL 不得重写实例身份，即使新值是合法 UUIDv4；不妨碍普通配置更新或幂等赋值。
        with engine.begin() as conn:
            conn.execute(text("UPDATE ai_configs SET name='编辑后',instance_key=instance_key WHERE id=1"))
        for invalid in (None, "", "short", str(uuid1()), str(uuid4()).upper(), str(uuid4()), keys[1]):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("UPDATE ai_configs SET instance_key=:key WHERE id=1"), {"key": invalid})
        for invalid in ("", "short", str(uuid1()), str(uuid4()).upper(), str(uuid4()) + "\x00extra", keys[0]):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("INSERT INTO ai_configs(user_id,name,provider,api_key_enc,model,"
                                  "is_default,instance_key) "
                                  "VALUES(1,'坏配置','openai_compat','密文','model',0,:key)"), {"key": invalid})
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO ai_configs(user_id,name,provider,api_key_enc,model,is_default) "
                              "VALUES(1,'旧 SQL 一','openai_compat','密文','model',0),"
                              "(1,'旧 SQL 二','openai_compat','密文','model',0)"))
            conn.execute(text("INSERT INTO ai_configs(user_id,name,provider,api_key_enc,model,is_default,instance_key) "
                              "VALUES(1,'显式空值','openai_compat','密文','model',0,NULL)"))
            keys = list(conn.scalars(text("SELECT instance_key FROM ai_configs ORDER BY id")))
            assert len(set(keys)) == len(keys) == 506
            for key in keys:
                _canonical(key)
        info = _sqlite_snapshot(path, backup, time.monotonic() + 20, threading.Event())
        assert info.schema_revision == "0049"
        restored = create_engine(f"sqlite:///{backup}")
        try:
            with restored.connect() as conn:
                assert list(conn.scalars(text("SELECT instance_key FROM ai_configs ORDER BY id"))) == keys
                assert conn.scalar(text("SELECT config_id FROM config_references WHERE id=1")) == 1
            with pytest.raises(IntegrityError), restored.begin() as conn:
                conn.execute(text("UPDATE ai_configs SET instance_key=:key WHERE id=1"), {"key": str(uuid4())})
        finally:
            restored.dispose()
    finally:
        engine.dispose()


def test_0049_sqlite_empty_and_safe_downgrade_preserve_configs_and_legacy_tasks(tmp_path):
    path = tmp_path / "safe.db"
    config, engine = _config(path), create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0048")
        with engine.begin() as conn:
            _seed(conn)
            conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                              "VALUES(1,1,1,'chapter.generate','succeeded',:data)"),
                         {"data": json.dumps({"_project_model": {"version": 1}, "story": "保留"})})
            old_configs = conn.execute(text("SELECT * FROM ai_configs ORDER BY id")).all()
            original_task = conn.scalar(text("SELECT input FROM tasks WHERE id=1"))
        command.upgrade(config, "0049")
        command.downgrade(config, "0048")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
            assert "instance_key" not in {col["name"] for col in inspect(conn).get_columns("ai_configs")}
            assert conn.execute(text("SELECT * FROM ai_configs ORDER BY id")).all() == old_configs
            assert conn.scalar(text("SELECT input FROM tasks WHERE id=1")) == original_task
            assert conn.scalar(text("SELECT config_id FROM config_references WHERE id=1")) == 1
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


def test_0049_sqlite_empty_database_upgrades_and_downgrades(tmp_path):
    path = tmp_path / "empty.db"
    config = _config(path)
    command.upgrade(config, "0049")
    command.downgrade(config, "0048")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


@pytest.mark.parametrize("schema_source", ["migration", "create_all"])
def test_sqlite_generated_uuid_collision_never_ignores_or_replaces_existing_config(tmp_path, schema_source):
    path = tmp_path / "collision.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        if schema_source == "migration":
            command.upgrade(_config(path), "0049")
        else:
            Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
            raw = conn.connection.driver_connection
            raw.create_function("randomblob", 1, lambda length: b"\0" * length)
            raw.create_function("random", 0, lambda: 0)
            conn.exec_driver_sql("INSERT INTO ai_configs(user_id,name,provider,api_key_enc,model,is_default) "
                                 "VALUES(1,'原配置','openai_compat','密文','model',0)")
            before = conn.execute(text("SELECT id,name,instance_key FROM ai_configs")).one()
            _canonical(before.instance_key)
            for action in ("OR IGNORE", "OR REPLACE", ""):
                values = ("(1,'重复一','openai_compat','密文','model',0),"
                          "(1,'重复二','openai_compat','密文','model',0)") if not action else (
                              "(1,'重复','openai_compat','密文','model',0)"
                          )
                with pytest.raises(IntegrityError), conn.begin_nested():
                    conn.exec_driver_sql(
                        f"INSERT {action} INTO ai_configs(user_id,name,provider,api_key_enc,model,is_default) "
                        f"VALUES {values}"
                    )
                assert conn.execute(text("SELECT id,name,instance_key FROM ai_configs")).all() == [before]
                assert conn.scalar(text("SELECT count(*) FROM ai_configs WHERE instance_key IS NULL")) == 0
    finally:
        engine.dispose()


def test_0049_downgrade_rejects_unsettled_and_new_bindings(tmp_path):
    path = tmp_path / "dependent.db"
    config, engine = _config(path), create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0048")
        with engine.begin() as conn:
            _seed(conn)
            conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                              "VALUES(1,1,1,'chapter.generate','succeeded','{}')"))
        command.upgrade(config, "0049")
        cases = [
            ("queued", {}, None),
            ("paused", {}, None),
            ("succeeded", {"nested": [{"_project_model": {"version": 2, "config_instance_key": str(uuid4())}}]}, None),
            ("succeeded", {"_shared_embedding": {"version": 2, "mode": "keyword", "model": None}}, None),
            ("failed", {}, {"_analysis_pending": None}),
            ("failed", {}, {"_batch_outcome_unknown": "损坏"}),
            ("failed", {"_project_model": None}, None),
            ("failed", {"copy": r'"{\"\u005fproject\u005fmodel\":{\"version\":2'}, None),
        ]
        for status, data, checkpoint in cases:
            with engine.begin() as conn:
                conn.execute(text("UPDATE tasks SET status=:status,input=:input,checkpoint=:checkpoint WHERE id=1"), {
                    "status": status, "input": json.dumps(data),
                    "checkpoint": json.dumps(checkpoint) if checkpoint is not None else None,
                })
            with pytest.raises(RuntimeError, match="降级"):
                command.downgrade(config, "0048")
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0049"
        with engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET status='succeeded',input='{}',checkpoint=NULL WHERE id=1"))
            conn.execute(text("INSERT INTO retrieval_chunks(project_id,source,ref_id,text,tokens,length,"
                              "embedding_key,enabled,pinned) VALUES(1,'memory',1,'正文','{}',2,"
                              "'shared:v2:unknown:2',1,0)"))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0048")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM retrieval_chunks"))
            conn.execute(text("INSERT INTO agent_conversations(id,user_id,project_id,title) VALUES(1,1,1,'会话')"))
            conn.execute(text("INSERT INTO agent_changes(conversation_id,project_id,turn_task_id,tool_call_id,"
                              "tool_name,arguments,kind,preview,status,result,change_key) "
                              "VALUES(1,1,1,'call','tool','{}','external','{}','failed',:result,:key)"), {
                "result": json.dumps({"error": "未知", "_external_snapshot_mac": None}), "key": str(uuid4()),
            })
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0048")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agent_changes"))
            conn.execute(text("INSERT INTO scene_review_proposals(id,task_id,chapter_id,task_key,run_key,token,"
                              "content_revision,status,payload) VALUES(1,1,1,:task,:run,:token,0,'applied','{}')"), {
                "task": str(uuid4()), "run": str(uuid4()), "token": str(uuid4()),
            })
            conn.execute(text("INSERT INTO scene_review_payload_chunks(proposal_id,ordinal,content) "
                              "VALUES(1,0,:first),(1,1,:second)"), {
                "first": '{"_model_', "second": 'binding":{"version":2}}',
            })
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0048")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM scene_review_payload_chunks"))
            conn.execute(text("DELETE FROM scene_review_proposals"))
            conn.execute(text("INSERT INTO retrieval_chunks(project_id,source,ref_id,text,tokens,length,"
                              "embedding_key,enabled,pinned) VALUES(1,'memory',1,'正文','{}',2,"
                              "'shared:v1:old:2',1,0)"))
        command.downgrade(config, "0048")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT embedding_key FROM retrieval_chunks")) == "shared:v1:old:2"
    finally:
        engine.dispose()


@pytest.mark.parametrize("phase", ["backfill", "trigger"])
def test_0049_failed_upgrade_rolls_back_and_retries(tmp_path, monkeypatch, phase):
    path = tmp_path / "retry.db"
    config, engine = _config(path), create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0048")
        with engine.begin() as conn:
            _seed(conn)
        original = async_sqlalchemy.async_engine_from_config

        def faulty(*args, **kwargs):
            migration_engine = original(*args, **kwargs)

            @event.listens_for(migration_engine.sync_engine, "before_cursor_execute")
            def fail(conn, cursor, statement, parameters, context, executemany):
                if (phase == "backfill" and statement.startswith("UPDATE ai_configs SET instance_key=")) or (
                    phase == "trigger" and statement.startswith("CREATE TRIGGER ai_configs_instance_key_immutable")
                ):
                    raise RuntimeError("模拟迁移中断")
            return migration_engine

        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", faulty)
        with pytest.raises(RuntimeError, match="模拟迁移中断"):
            command.upgrade(config, "0049")
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", original)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
            assert "instance_key" not in {col["name"] for col in inspect(conn).get_columns("ai_configs")}
            assert conn.scalar(text("SELECT count(*) FROM ai_configs")) == 503
        command.upgrade(config, "0049")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(DISTINCT instance_key) FROM ai_configs")) == 503
    finally:
        engine.dispose()


def test_0049_failed_downgrade_restores_trigger_and_retries(tmp_path, monkeypatch):
    path = tmp_path / "downgrade-retry.db"
    config, engine = _config(path), create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0048")
        with engine.begin() as conn:
            _seed(conn)
        command.upgrade(config, "0049")
        with engine.connect() as conn:
            key = conn.scalar(text("SELECT instance_key FROM ai_configs WHERE id=1"))
        original = async_sqlalchemy.async_engine_from_config

        def faulty(*args, **kwargs):
            migration_engine = original(*args, **kwargs)

            @event.listens_for(migration_engine.sync_engine, "before_cursor_execute")
            def fail(conn, cursor, statement, parameters, context, executemany):
                if "DROP INDEX" in statement and "ux_ai_configs_instance_key" in statement:
                    raise RuntimeError("模拟降级中断")
            return migration_engine

        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", faulty)
        with pytest.raises(RuntimeError, match="模拟降级中断"):
            command.downgrade(config, "0048")
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", original)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0049"
            assert conn.scalar(text("SELECT instance_key FROM ai_configs WHERE id=1")) == key
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE ai_configs SET instance_key=:key WHERE id=1"), {"key": str(uuid4())})
        command.downgrade(config, "0048")
    finally:
        engine.dispose()


async def test_create_all_direct_sql_generates_unique_uuid_and_never_accepts_update(app, client):
    user = await register(client, "config-instance-direct-sql")
    async with app.state.sessionmaker() as db:
        await db.execute(text("INSERT INTO ai_configs(user_id,name,provider,api_key_enc,model,is_default) "
                              "VALUES(:user,'直接一','openai_compat','密文','model',0),"
                              "(:user,'直接二','openai_compat','密文','model',0)"), {"user": user["id"]})
        await db.commit()
        rows = (await db.execute(text("SELECT id,instance_key FROM ai_configs ORDER BY id"))).all()
        assert len(rows) == 2 and rows[0].instance_key != rows[1].instance_key
        for row in rows:
            _canonical(row.instance_key)
        with pytest.raises(IntegrityError):
            await db.execute(text("UPDATE ai_configs SET instance_key=NULL WHERE id=:id"), {"id": rows[0].id})
        await db.rollback()


async def test_new_config_id_reuse_gets_new_uuid_and_single_book_excludes_configs(app, client):
    await register(client, "config-instance-owner")
    supplied = str(uuid4())
    original = await create_ai_config(client, instance_key=supplied)
    assert "instance_key" not in original
    async with app.state.sessionmaker() as db:
        initial_key = (await db.get(AIConfig, original["id"])).instance_key
    supplied_update = str(uuid4())
    updated = await client.patch(f"/api/v1/ai-configs/{original['id']}", json={
        "name": "更新后的模型", "instance_key": supplied_update,
    })
    assert updated.status_code == 200 and "instance_key" not in updated.json()
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        row = await db.get(AIConfig, original["id"])
        old_id, old_key = row.id, row.instance_key
        _canonical(old_key)
        assert old_key == initial_key and old_key not in (supplied, supplied_update)
        with pytest.raises(IntegrityError):
            await db.execute(update(AIConfig).where(AIConfig.id == old_id).values(instance_key=str(uuid4())))
        await db.rollback()
        row = await db.get(AIConfig, old_id)
        await db.delete(row)
        await db.flush()
        replacement = AIConfig(id=old_id, user_id=row.user_id, name=row.name, provider=row.provider,
                               base_url=row.base_url, api_key_enc=row.api_key_enc, model=row.model,
                               model_type=row.model_type, is_default=row.is_default)
        db.add(replacement)
        await db.commit()
        _canonical(replacement.instance_key)
        assert replacement.id == old_id and replacement.instance_key != old_key
    backup = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert backup.status_code == 200, backup.text
    assert "ai_configs" not in backup.json()["tables"]
    assert old_key not in backup.text and replacement.instance_key not in backup.text
