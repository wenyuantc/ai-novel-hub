"""0052 的旧日志、数据库身份约束与整库备份。"""

import threading
import time
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.services.backups.database import _sqlite_snapshot
from tests.test_generation_log_source_migration import _seed
from tests.test_setting_fields_migration import _config, _migration_fk_on_connect


@pytest.fixture(autouse=True)
def _sqlite_foreign_keys(monkeypatch):
    _migration_fk_on_connect(monkeypatch)


def test_0052_legacy_rows_identity_constraints_backup_and_safe_downgrade(tmp_path):
    path, backup = tmp_path / "logs.db", tmp_path / "snapshot.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        command.upgrade(config, "0051")
        with engine.begin() as db:
            _seed(db)
            old = db.execute(text("SELECT * FROM generation_logs WHERE id=1")).mappings().one()
            old_columns = list(old.keys())
            old_values = tuple(old[name] for name in old_columns)
            foreign_keys = inspect(db).get_foreign_keys("generation_logs")
        command.upgrade(config, "0052")
        log_key, model_key = str(uuid4()), str(uuid4())
        with engine.begin() as db:
            row = db.execute(text("SELECT * FROM generation_logs WHERE id=1")).mappings().one()
            assert tuple(row[name] for name in old_columns) == old_values
            assert row["log_instance_key"] is None and row["model_config_instance_key"] is None
            assert inspect(db).get_foreign_keys("generation_logs") == foreign_keys
            db.execute(text("INSERT INTO generation_logs(id,project_id,actor_user_id,kind,status,provider,model,"
                            "params,messages,error,output_chars,duration_ms,log_instance_key,model_source,"
                            "model_config_owner_id,model_config_instance_key) VALUES "
                            "(2,1,1,'generate','success','p','m','{}','[]','',0,0,:log,'actor',1,:model)"),
                       {"log": log_key, "model": model_key})
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
        for statement, values in (
            ("UPDATE generation_logs SET log_instance_key=:key WHERE id=1", {"key": str(uuid4())}),
            ("UPDATE generation_logs SET log_instance_key=:key WHERE id=2", {"key": str(uuid4())}),
            ("UPDATE generation_logs SET model_config_instance_key=:key WHERE id=2", {"key": str(uuid4())}),
            ("INSERT INTO generation_logs(project_id,kind,status,provider,model,params,messages,error,output_chars,"
             "duration_ms) VALUES(1,'generate','success','','','{}','[]','',0,0)", {}),
            ("INSERT INTO generation_logs(project_id,kind,status,provider,model,params,messages,error,output_chars,"
             "duration_ms,log_instance_key) VALUES(1,'generate','success','','','{}','[]','',0,0,:key)",
             {"key": log_key}),
            ("INSERT INTO generation_logs(project_id,kind,status,provider,model,params,messages,error,output_chars,"
             "duration_ms,log_instance_key) VALUES(1,'generate','success','','','{}','[]','',0,0,:key)",
             {"key": "not-a-uuid"}),
        ):
            with pytest.raises(IntegrityError), engine.begin() as db:
                db.execute(text(statement), values)
        _sqlite_snapshot(path, backup, time.monotonic() + 20, threading.Event())
        snapshot = create_engine(f"sqlite:///{backup}")
        try:
            with snapshot.connect() as db:
                assert db.execute(text("SELECT log_instance_key,model_config_instance_key FROM generation_logs "
                                       "WHERE id=2")).one() == (log_key, model_key)
        finally:
            snapshot.dispose()
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0051")
        with engine.begin() as db:
            db.execute(text("DELETE FROM generation_logs WHERE id=2"))
        command.downgrade(config, "0051")
        with engine.connect() as db:
            assert db.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


def test_0052_empty_database_can_downgrade(tmp_path):
    config = _config(tmp_path / "fresh.db")
    command.upgrade(config, "0052")
    command.downgrade(config, "0051")
