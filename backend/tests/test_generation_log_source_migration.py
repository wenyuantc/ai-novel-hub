"""0048 日志可信模型来源的迁移、约束与整库备份。"""

import threading
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from app.models import Base
from app.services.backups.database import _sqlite_snapshot
from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations, _schema_contract


@pytest.fixture(autouse=True)
def _sqlite_migration_connections_enforce_foreign_keys(monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)


def _seed(conn):
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    conn.execute(text("INSERT INTO users(id,username,password_hash) "
                      "VALUES(1,'owner','hash'),(2,'config-owner','hash')"))
    conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                      "VALUES(1,1,'旧书','','','','writing')"))
    conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                      "VALUES(1,1,1,'旧章','正文','done',2)"))
    conn.execute(text("INSERT INTO ai_configs(id,user_id,name,provider,api_key_enc,model,is_default) "
                      "VALUES(1,2,'外部配置','openai_compat','密文','model',0)"))
    conn.exec_driver_sql("INSERT INTO generation_logs(id,project_id,actor_user_id,chapter_id,kind,ai_config_id,"
                         "provider,model,params,messages,status,error,output_chars,duration_ms) "
                         "VALUES(1,1,1,1,'generate',1,'openai_compat','model',"
                         "'{\"_model_source\":\"owner\",\"model_config_owner_id\":2}',"
                         "'[]','success','',3,5)")
    conn.execute(text("INSERT INTO chapter_versions(id,chapter_id,seq,source,content,word_count,generation_log_id) "
                      "VALUES(1,1,1,'generate','旧正文',3,1)"))


def test_0048_sqlite_legacy_data_constraints_backup_and_safe_downgrade(tmp_path):
    path, backup = tmp_path / "source.db", tmp_path / "snapshot.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0047")
        with engine.begin() as conn:
            _seed(conn)
            old_columns = [column["name"] for column in inspect(conn).get_columns("generation_logs")]
            old_row = conn.execute(text("SELECT * FROM generation_logs WHERE id=1")).one()
            old_fks = inspect(conn).get_foreign_keys("generation_logs")
        command.upgrade(config, "0048")
        Base.metadata.create_all(expected)
        actual_contract = _schema_contract(inspect(engine), "generation_logs")
        model_contract = _schema_contract(inspect(expected), "generation_logs")
        # 0044 的 SQLite 内联 actor FK 反射本就丢名称/选项；下方另校验迁移前后不变及实际 FK 行为。
        actual_contract.pop("foreign_keys")
        model_contract.pop("foreign_keys")
        # 当前模型已含 0052 的实例键；0048 时点只比较当时已存在的结构（0052 另有专门迁移测试）。
        for name in ("log_instance_key", "model_config_instance_key"):
            model_contract["columns"].pop(name)
            model_contract["check_constraints"].pop(f"ck_generation_logs_{name}_uuid4")
        model_contract["indexes"].pop("ux_generation_logs_log_instance_key")
        assert actual_contract == model_contract
        with engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM generation_logs WHERE id=1")).mappings().one()
            assert tuple(row[name] for name in old_columns) == old_row
            assert (row["model_source"], row["model_config_owner_id"]) == (None, None)
            assert inspect(conn).get_foreign_keys("generation_logs") == old_fks
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()

        for source, owner_id in (("owner", None), (None, 2), ("bogus", 2), ("", 2), ("actor", 0),
                                 ("owner", -1), ("actor", 2**31), ("actor", 1.5)):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text("UPDATE generation_logs SET model_source=:source,model_config_owner_id=:owner "
                                  "WHERE id=1"), {"source": source, "owner": owner_id})
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.execute(text("UPDATE generation_logs SET model_source='owner',model_config_owner_id=2 WHERE id=1"))
            conn.execute(text("DELETE FROM ai_configs WHERE id=1"))
            conn.execute(text("DELETE FROM users WHERE id=2"))
            assert conn.execute(text("SELECT ai_config_id,model_source,model_config_owner_id "
                                     "FROM generation_logs WHERE id=1")).one() == (None, "owner", 2)
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1

        info = _sqlite_snapshot(path, backup, time.monotonic() + 20, threading.Event())
        assert info.schema_revision == "0048"
        snapshot = create_engine(f"sqlite:///{backup}")
        try:
            with snapshot.connect() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                assert conn.execute(text("SELECT model_source,model_config_owner_id FROM generation_logs WHERE id=1"))\
                    .one() == ("owner", 2)
                assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
                assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        finally:
            snapshot.dispose()

        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0047")
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
            assert conn.execute(text("SELECT model_source,model_config_owner_id FROM generation_logs WHERE id=1"))\
                .one() == ("owner", 2)
            conn.execute(text("UPDATE generation_logs SET model_source=NULL,model_config_owner_id=NULL WHERE id=1"))
        command.downgrade(config, "0047")
        with engine.connect() as conn:
            assert not {"model_source", "model_config_owner_id"} & {
                column["name"] for column in inspect(conn).get_columns("generation_logs")
            }
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()
        expected.dispose()


def test_0048_sqlite_empty_database_and_downgrade(tmp_path):
    path = tmp_path / "fresh.db"
    config = _config(path)
    command.upgrade(config, "0048")
    command.downgrade(config, "0047")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


def test_0048_sqlite_interrupted_upgrade_rolls_back_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "retry.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0047")
        with engine.begin() as conn:
            _seed(conn)
        original = async_sqlalchemy.async_engine_from_config

        def failed_factory(*args, **kwargs):
            migration_engine = original(*args, **kwargs)

            @event.listens_for(migration_engine.sync_engine, "before_cursor_execute")
            def fail_after_first_column(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith("ALTER TABLE generation_logs ADD COLUMN model_source"):
                    raise RuntimeError("模拟迁移中断")

            return migration_engine

        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", failed_factory)
        with pytest.raises(RuntimeError, match="模拟迁移中断"):
            command.upgrade(config, "0048")
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", original)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
            assert not {"model_source", "model_config_owner_id"} & {
                column["name"] for column in inspect(conn).get_columns("generation_logs")
            }
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
        command.upgrade(config, "0048")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


def test_0048_sqlite_interrupted_downgrade_rolls_back_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "downgrade-retry.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0047")
        with engine.begin() as conn:
            _seed(conn)
        command.upgrade(config, "0048")
        original = async_sqlalchemy.async_engine_from_config

        def failed_factory(*args, **kwargs):
            migration_engine = original(*args, **kwargs)

            @event.listens_for(migration_engine.sync_engine, "before_cursor_execute")
            def fail_after_first_drop(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith("ALTER TABLE generation_logs DROP COLUMN model_config_owner_id"):
                    raise RuntimeError("模拟降级中断")

            return migration_engine

        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", failed_factory)
        with pytest.raises(RuntimeError, match="模拟降级中断"):
            command.downgrade(config, "0047")
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", original)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
            assert {"model_source", "model_config_owner_id"} <= {
                column["name"] for column in inspect(conn).get_columns("generation_logs")
            }
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
        command.downgrade(config, "0047")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()
