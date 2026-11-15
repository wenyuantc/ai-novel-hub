"""Agent 提案标识迁移、数据保全、约束、备份和 ID 复用。"""

import threading
import time
from uuid import UUID, uuid1, uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from app.models import AgentChange, AgentMessage
from app.services.backups.database import _sqlite_snapshot
from app.services.project_transfer.registry import NOT_EXPORTED
from tests.helpers import create_project, register
from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations


@pytest.fixture(autouse=True)
def _sqlite_migration_connections_enforce_foreign_keys(monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)


def _seed(conn):
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
    conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                      "VALUES(1,1,'旧书','','','','writing')"))
    conn.execute(text("INSERT INTO agent_conversations(id,user_id,project_id,title) VALUES(1,1,1,'旧会话')"))
    conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                      "VALUES(1,1,1,'agent.turn','paused','{}')"))
    conn.execute(text("UPDATE tasks SET statistics_key=:key WHERE id=1"), {"key": str(uuid4())})
    conn.execute(text("INSERT INTO agent_messages(id,conversation_id,turn_task_id,role,content,tool_call_id) "
                      "VALUES(5,1,1,'assistant','旧消息','call-message')"))
    conn.exec_driver_sql("INSERT INTO agent_changes(id,conversation_id,project_id,turn_task_id,tool_call_id,"
                      "tool_name,arguments,kind,preview,status,result,effects,error,seq,created_at,updated_at) "
                      "VALUES(7,1,1,1,'call-a','edit','{\"text\":\"旧正文\"}','small','{}','proposed',NULL,"
                      "'[]','',1,'2026-01-01 00:00:00','2026-01-01 00:00:00'),"
                      "(19,1,1,1,'call-b','delete','{}','small','{}','executed','{\"ok\":true}',"
                      "'[]','',2,'2026-01-01 00:00:00','2026-01-01 00:00:00')")
    conn.execute(text("INSERT INTO agent_changes(id,conversation_id,project_id,turn_task_id,tool_call_id,"
                      "tool_name,arguments,kind,preview) VALUES(:id,1,1,1,:call,'edit','{}','small','{}')"), [
        {"id": row_id, "call": f"old-{row_id}"} for row_id in range(100, 603)
    ])
    # 外部引用模拟第三方扩展：重建父表会在 FK 开启时级联删除此子行。
    conn.execute(text("CREATE TABLE agent_change_references (id INTEGER PRIMARY KEY, "
                      "change_id INTEGER REFERENCES agent_changes(id) ON DELETE CASCADE)"))
    conn.execute(text("INSERT INTO agent_change_references(id,change_id) VALUES(1,7)"))


def _canonical_uuid4(value):
    parsed = UUID(value)
    assert str(parsed) == value and parsed.version == 4


def test_0047_sqlite_old_data_fks_constraints_backup_and_downgrade(tmp_path):
    path, backup = tmp_path / "agent.db", tmp_path / "snapshot.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0046")
        with engine.begin() as conn:
            _seed(conn)
            original_columns = [column["name"] for column in inspect(conn).get_columns("agent_changes")]
            old_rows = conn.execute(text("SELECT * FROM agent_changes ORDER BY id")).all()
            old_fks = inspect(conn).get_foreign_keys("agent_changes")
        command.upgrade(config, "0047")
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            identity_column = next(column for column in inspect(conn).get_columns("agent_changes")
                                   if column["name"] == "change_key")
            assert identity_column["nullable"] is False
            rows = conn.execute(text("SELECT * FROM agent_changes ORDER BY id")).mappings().all()
            assert [tuple(row[name] for name in original_columns) for row in rows] == old_rows
            keys = [row["change_key"] for row in rows]
            assert len(set(keys)) == len(rows) == 505
            for key in keys:
                _canonical_uuid4(key)
            # 即使旧 Task 后来被统计初始化补过 UUID，旧消息/提案仍不能猜认其原轮次。
            assert all(row["turn_task_key"] is None for row in rows)
            assert conn.scalar(text("SELECT turn_task_key FROM agent_messages WHERE id=5")) is None
            retained_task_key = conn.scalar(text("SELECT statistics_key FROM tasks WHERE id=1"))
            assert inspect(conn).get_foreign_keys("agent_changes") == old_fks
            assert conn.scalar(text("SELECT change_id FROM agent_change_references WHERE id=1")) == 7
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()

        invalids = [None, "", "short", "g" * 36, str(uuid4()).upper(), str(UUID(int=0)),
                    str(uuid4()) + "\x00extra", b"a" * 36]
        for invalid in invalids:
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("UPDATE agent_changes SET change_key=:key WHERE id=7"), {"key": invalid})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE agent_changes SET change_key=:key WHERE id=7"), {"key": keys[1]})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("INSERT INTO agent_changes(conversation_id,project_id,turn_task_id,tool_call_id,"
                              "tool_name,arguments,kind,preview) VALUES(1,1,1,'missing','edit','{}','small','{}')"))

        for table, row_id in (("agent_changes", 7), ("agent_messages", 5)):
            for invalid in ("", "short", str(uuid4()).upper(), "g" * 36, str(uuid4()) + "\x00extra"):
                with pytest.raises(IntegrityError), engine.begin() as conn:
                    conn.execute(text(f"UPDATE {table} SET turn_task_key=:key WHERE id=:id"),
                                 {"key": invalid, "id": row_id})
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            for table, row_id in (("agent_changes", 7), ("agent_messages", 5)):
                conn.execute(text(f"UPDATE {table} SET turn_task_key=:key WHERE id=:id"),
                             {"key": retained_task_key, "id": row_id})
            conn.execute(text("DELETE FROM tasks WHERE id=1"))
            for table, row_id in (("agent_changes", 7), ("agent_messages", 5)):
                assert conn.execute(text(f"SELECT turn_task_id,turn_task_key FROM {table} WHERE id=:id"),
                                    {"id": row_id}).one() == (None, retained_task_key)
            conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input,statistics_key) "
                              "VALUES(1,1,1,'agent.turn','paused','{}',:key)"), {"key": str(uuid4())})
            for table, row_id in (("agent_changes", 7), ("agent_messages", 5)):
                assert conn.execute(text(f"SELECT turn_task_id,turn_task_key FROM {table} WHERE id=:id"),
                                    {"id": row_id}).one() == (None, retained_task_key)

        info = _sqlite_snapshot(path, backup, time.monotonic() + 20, threading.Event())
        assert info.schema_revision == "0047"
        snapshot = create_engine(f"sqlite:///{backup}")
        try:
            with snapshot.connect() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                assert list(conn.scalars(text("SELECT change_key FROM agent_changes ORDER BY id"))) == keys
                assert conn.scalar(text("SELECT change_id FROM agent_change_references WHERE id=1")) == 7
                assert conn.scalar(text("SELECT turn_task_key FROM agent_messages WHERE id=5")) == retained_task_key
                assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
            with pytest.raises(IntegrityError), snapshot.begin() as conn:
                conn.execute(text("INSERT INTO agent_changes(conversation_id,project_id,turn_task_id,tool_call_id,"
                                  "tool_name,arguments,kind,preview) "
                                  "VALUES(1,1,1,'missing','edit','{}','small','{}')"))
        finally:
            snapshot.dispose()
        assert {"agent_changes", "agent_messages"} <= set(NOT_EXPORTED)

        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0046")
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
            assert list(conn.scalars(text("SELECT change_key FROM agent_changes ORDER BY id"))) == keys
            assert conn.scalar(text("SELECT count(*) FROM agent_change_references")) == 1
            conn.execute(text("DELETE FROM agent_changes"))
            assert conn.scalar(text("SELECT count(*) FROM agent_change_references")) == 0
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0046")
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT turn_task_key FROM agent_messages WHERE id=5")) == retained_task_key
            conn.execute(text("UPDATE agent_messages SET turn_task_key=NULL WHERE id=5"))
        command.downgrade(config, "0046")
        with engine.connect() as conn:
            assert "change_key" not in {column["name"] for column in inspect(conn).get_columns("agent_changes")}
            assert "turn_task_key" not in {column["name"] for column in inspect(conn).get_columns("agent_messages")}
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


def test_0047_sqlite_empty_upgrade_downgrade(tmp_path):
    path = tmp_path / "empty.db"
    config = _config(path)
    command.upgrade(config, "0046")
    command.upgrade(config, "0047")
    command.downgrade(config, "0046")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0046"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


def test_0047_sqlite_direct_upgrade_from_empty_database(tmp_path):
    path = tmp_path / "fresh.db"
    config = _config(path)
    command.upgrade(config, "0047")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
            assert "turn_task_key" in {column["name"] for column in inspect(conn).get_columns("agent_messages")}
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure_point", ["backfill", "later_ddl"])
def test_0047_sqlite_failed_backfill_rolls_back_ddl_and_can_retry(tmp_path, monkeypatch, failure_point):
    path = tmp_path / "retry.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0046")
        with engine.begin() as conn:
            _seed(conn)
        original = async_sqlalchemy.async_engine_from_config

        def failed_factory(*args, **kwargs):
            migration_engine = original(*args, **kwargs)

            @event.listens_for(migration_engine.sync_engine, "before_cursor_execute")
            def fail_backfill(conn, cursor, statement, parameters, context, executemany):
                if ((failure_point == "backfill" and statement.startswith("UPDATE agent_changes SET change_key="))
                        or (failure_point == "later_ddl" and statement.startswith(
                            "ALTER TABLE agent_messages ADD COLUMN turn_task_key"
                        ))):
                    raise RuntimeError("模拟迁移回填失败")

            return migration_engine

        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", failed_factory)
        with pytest.raises(RuntimeError, match="模拟迁移回填失败"):
            command.upgrade(config, "0047")
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", original)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0046"
            assert "change_key" not in {column["name"] for column in inspect(conn).get_columns("agent_changes")}
            assert "turn_task_key" not in {column["name"] for column in inspect(conn).get_columns("agent_messages")}
            assert conn.scalar(text("SELECT count(*) FROM sqlite_master WHERE name LIKE "
                                    "'agent_changes_change_key_nonempty_%'")) == 0
            assert conn.scalar(text("SELECT count(*) FROM agent_changes")) == 505
            assert conn.scalar(text("SELECT change_id FROM agent_change_references WHERE id=1")) == 7
        command.upgrade(config, "0047")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
            assert conn.scalar(text("SELECT count(DISTINCT change_key) FROM agent_changes")) == 505
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


async def test_new_agent_change_uuid_differs_when_integer_id_is_reused(app, client):
    await register(client, "agent-change-key")
    project = await create_project(client)
    conversation = await client.post(f"/api/v1/projects/{project['id']}/agent/conversations", json={})
    assert conversation.status_code == 201, conversation.text
    async with app.state.sessionmaker() as db:
        original = AgentChange(conversation_id=conversation.json()["id"], project_id=project["id"],
                               tool_call_id="same", tool_name="edit", kind="small", arguments={}, preview={})
        db.add(original)
        await db.flush()
        old_id, old_key, created_at = original.id, original.change_key, original.created_at
        _canonical_uuid4(old_key)
        await db.delete(original)
        await db.flush()
        replacement = AgentChange(id=old_id, conversation_id=conversation.json()["id"], project_id=project["id"],
                                  tool_call_id="same", tool_name="edit", kind="small", arguments={}, preview={},
                                  created_at=created_at)
        db.add(replacement)
        await db.commit()
        _canonical_uuid4(replacement.change_key)
        assert replacement.id == old_id and replacement.change_key != old_key


async def test_orm_task_uuid_shape_and_duplicate_change_identity(app, client):
    await register(client, "agent-identity-shape")
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/agent/conversations", json={})
    assert response.status_code == 201, response.text
    conversation_id = response.json()["id"]
    canonical = str(uuid1())  # 原任务 UUID 只要求规范格式，不误限为 UUIDv4。
    async with app.state.sessionmaker() as db:
        message = AgentMessage(conversation_id=conversation_id, role="assistant", turn_task_key=canonical)
        change = AgentChange(conversation_id=conversation_id, project_id=project["id"], turn_task_key=canonical,
                             tool_call_id="first", tool_name="edit", kind="small", arguments={}, preview={})
        db.add_all([message, change])
        await db.commit()
        key = change.change_key
    async with app.state.sessionmaker() as db:
        db.add(AgentChange(conversation_id=conversation_id, project_id=project["id"], change_key=key,
                           tool_call_id="second", tool_name="edit", kind="small", arguments={}, preview={}))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
    async with app.state.sessionmaker() as db:
        db.add(AgentMessage(conversation_id=conversation_id, role="assistant", turn_task_key=canonical.upper()))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


async def test_single_project_backup_excludes_agent_identities(app, client):
    await register(client, "agent-identity-export")
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/agent/conversations", json={})
    assert response.status_code == 201, response.text
    async with app.state.sessionmaker() as db:
        change = AgentChange(conversation_id=response.json()["id"], project_id=project["id"],
                             tool_call_id="private", tool_name="edit", kind="small", arguments={}, preview={},
                             turn_task_key=str(uuid4()))
        db.add(change)
        await db.commit()
        key, task_key = change.change_key, change.turn_task_key
    backup = await client.get(f"/api/v1/projects/{project['id']}/backup")
    assert backup.status_code == 200, backup.text
    assert not {"agent_conversations", "agent_messages", "agent_changes"} & set(backup.json()["tables"])
    assert key not in backup.text and task_key not in backup.text
