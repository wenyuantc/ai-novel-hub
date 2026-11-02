"""0030既升级存量内置授权，又保留原33项自定义授权及业务数据。"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.permissions import PERMISSION_CODES


def config_engine(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    path = tmp_path / "backup-migration.db"
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    config.attributes["configure_logger"] = False
    return config, create_engine(f"sqlite:///{path}")


def test_0030_upgrades_existing_builtin_only_and_keeps_business_and_custom_grants(tmp_path):
    config, engine = config_engine(tmp_path)
    try:
        command.upgrade(config, "0029")
        tables_before = set(inspect(engine).get_table_names())
        with engine.begin() as db:
            old_super = db.execute(text("SELECT permissions FROM roles WHERE system_key='super_admin'")).scalar_one()
            assert len(json.loads(old_super)) == 33
            db.execute(
                text(
                    "INSERT INTO users(id,username,password_hash,is_admin,token_version) "
                    "VALUES(1,'原超管','hash',1,8),(2,'旧角色成员','hash',0,3)"
                )
            )
            db.execute(
                text("INSERT INTO roles(id,name,permissions) VALUES(10,'旧全部权限',:permissions)"),
                {"permissions": old_super},
            )
            db.execute(text("INSERT INTO user_roles(user_id,role_id) VALUES(2,10)"))
            users = db.execute(text("SELECT * FROM users ORDER BY id")).all()
            before_other = db.execute(
                text("SELECT system_key,permissions,revision FROM roles WHERE system_key!='super_admin' ORDER BY id")
            ).all()
        command.upgrade(config, "0030")
        assert set(inspect(engine).get_table_names()) == tables_before | {
            "backup_settings",
            "backup_runs",
            "backup_artifacts",
        }
        assert not inspect(engine).get_foreign_keys("backup_runs")
        with engine.connect() as db:
            permissions, revision = db.execute(
                text(
                    "SELECT permissions,revision FROM roles WHERE system_key='super_admin'",
                )
            ).one()
            assert json.loads(permissions) == list(PERMISSION_CODES) and revision == 2
            assert db.execute(text("SELECT revision FROM authorization_state")).scalar_one() == 1
            assert db.execute(text("SELECT permissions FROM roles WHERE id=10")).scalar_one() == old_super
            assert (
                db.execute(
                    text(
                        "SELECT system_key,permissions,revision FROM roles WHERE system_key!='super_admin' ORDER BY id"
                    )
                ).all()
                == before_other
            )
            assert db.execute(text("SELECT * FROM users ORDER BY id")).all() == users
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        # 旧33项自定义角色不构成新权限使用，空模块可回退到0029且不丢旧业务。
        command.downgrade(config, "0029")
        with engine.connect() as db:
            assert (
                db.execute(text("SELECT permissions FROM roles WHERE system_key='super_admin'")).scalar_one()
                == old_super
            )
            assert db.execute(text("SELECT * FROM users ORDER BY id")).all() == users
    finally:
        engine.dispose()


@pytest.mark.parametrize("state", ["settings", "permission", "audit"])
def test_0030_refuses_lossy_downgrade(tmp_path, state):
    config, engine = config_engine(tmp_path)
    try:
        command.upgrade(config, "0030")
        with engine.begin() as db:
            if state == "settings":
                db.execute(
                    text(
                        "INSERT INTO backup_settings(id,revision,instance_key,configuration,secrets_enc,"
                        "last_operation_id,"
                        "updated_at) VALUES(1,1,'test','{}','{}','test',CURRENT_TIMESTAMP)",
                    )
                )
            elif state == "permission":
                db.execute(
                    text("INSERT INTO roles(name,permissions) VALUES('新备份委派',:permissions)"),
                    {"permissions": json.dumps(["backups.read"])},
                )
            else:
                db.execute(
                    text(
                        "INSERT INTO admin_audit_logs(operation_id,actor_user_id,actor_username,action,result,"
                        "target_type,summary,occurred_at) VALUES('test',1,'历史管理员','backup.run.accept',"
                        "'success','backup_run','{}',CURRENT_TIMESTAMP)",
                    )
                )
        with pytest.raises(RuntimeError, match="不能无损降级"):
            command.downgrade(config, "0029")
        assert "backup_runs" in inspect(engine).get_table_names()
        with engine.connect() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0030"
    finally:
        engine.dispose()


def test_0030_constraints_active_lease_artifact_identity_and_big_file_size(tmp_path):
    config, engine = config_engine(tmp_path)
    try:
        command.upgrade(config, "0030")
        insert_run = text(
            "INSERT INTO backup_runs(execution_key,instance_key,source,settings_revision,config_snapshot,"
            "database_engine,"
            "encrypted,file_format,status,active_slot,created_at,notification_status,cleanup_status) "
            "VALUES(:execution,'instance','manual',1,'{}','sqlite',0,'sqlite3',:status,:slot,CURRENT_TIMESTAMP,"
            "'not_configured','not_required')",
        )
        with engine.begin() as db:
            db.execute(insert_run, {"execution": "first", "status": "queued", "slot": 1})
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(insert_run, {"execution": "second", "status": "queued", "slot": 1})
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(insert_run, {"execution": "bad", "status": "running", "slot": None})
        with engine.begin() as db:
            db.execute(text("UPDATE backup_runs SET status='failed',active_slot=NULL"))
            db.execute(insert_run, {"execution": "second", "status": "queued", "slot": 1})
            db.execute(
                text(
                    "INSERT INTO backup_artifacts(run_id,destination,status,size_bytes,locator,"
                    "access_snapshot_enc,created_at) "
                    "VALUES(1,'local','ready',:size,'{}','{}',CURRENT_TIMESTAMP)",
                ),
                {"size": 5 * 1024**3},
            )
            assert db.execute(text("SELECT size_bytes FROM backup_artifacts")).scalar_one() == 5 * 1024**3
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO backup_artifacts(run_id,destination,status,locator,access_snapshot_enc,created_at) "
                    "VALUES(1,'local','ready','{}','{}',CURRENT_TIMESTAMP)",
                )
            )
        # PostgreSQL列编译保留64位，不能因SQLite无界Integer碰巧通过而漏掉>2GB。
        size_type = next(
            column["type"]
            for column in inspect(engine).get_columns("backup_artifacts")
            if column["name"] == "size_bytes"
        )
        assert str(size_type) == "BIGINT"
    finally:
        engine.dispose()
