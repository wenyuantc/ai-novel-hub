"""0028存量向前升级不重写身份/会话；新语义存在时禁止有损降级。"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.permissions import BUILTIN_ROLES


def migration_config(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    path = tmp_path / "rbac.db"
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    config.attributes["configure_logger"] = False
    return config, create_engine(f"sqlite:///{path}")


def test_0029_preserves_users_sessions_and_business_foreign_keys(tmp_path):
    config, engine = migration_config(tmp_path)
    try:
        command.upgrade(config, "0028")
        before_tables = set(inspect(engine).get_table_names())
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO users(id,username,password_hash,is_admin,is_active,token_version) "
                "VALUES (1,'旧超管','secret-hash',1,0,7),(2,'普通账号','other-hash',0,1,3)"
            ))
            before = db.execute(text("SELECT * FROM users ORDER BY id")).all()
        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names()) == before_tables | {
            "roles", "user_roles", "authorization_state", "user_quotas",
        }
        with engine.begin() as db:
            assert db.execute(text("SELECT * FROM users ORDER BY id")).all() == before
            assert db.execute(text("SELECT revision FROM authorization_state WHERE id=1")).scalar_one() == 0
            assert db.execute(text("SELECT count(*) FROM user_roles")).scalar_one() == 0
            assert db.execute(text("SELECT count(*) FROM user_quotas")).scalar_one() == 0
            roles = db.execute(text("SELECT system_key,name,permissions FROM roles")).all()
            assert len(roles) == 4
            for key, name, permissions in roles:
                assert name == BUILTIN_ROLES[key][0]
                assert set(json.loads(permissions)) == set(BUILTIN_ROLES[key][2])
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        for sql in (
            "INSERT INTO authorization_state(id,revision) VALUES(2,0)",
            "UPDATE authorization_state SET revision=-1",
            "INSERT INTO user_quotas(user_id,project_mode,project_value) VALUES(1,'inherit',3)",
            "INSERT INTO user_quotas(user_id,task_mode,task_value) VALUES(1,'limited',NULL)",
        ):
            with pytest.raises(IntegrityError), engine.begin() as db:
                db.execute(text(sql))
        command.downgrade(config, "0028")
        assert set(inspect(engine).get_table_names()) == before_tables
        with engine.connect() as db:
            assert db.execute(text("SELECT * FROM users ORDER BY id")).all() == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("new_state", ["role", "assignment", "quota"])
def test_0029_refuses_lossy_downgrade_and_does_not_reuse_role_ids(tmp_path, new_state):
    config, engine = migration_config(tmp_path)
    try:
        command.upgrade(config, "0029")
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'保留账号','x')"))
            db.execute(text("INSERT INTO roles(name,permissions) VALUES('临时角色','[]')"))
            first_id = db.execute(text("SELECT id FROM roles WHERE name='临时角色'")).scalar_one()
            db.execute(text("DELETE FROM roles WHERE id=:id"), {"id": first_id})
            db.execute(text("INSERT INTO roles(name,permissions) VALUES('新角色','[]')"))
            role_id = db.execute(text("SELECT id FROM roles WHERE name='新角色'")).scalar_one()
            assert role_id > first_id
            if new_state != "role":
                db.execute(text("DELETE FROM roles WHERE id=:id"), {"id": role_id})
            if new_state == "assignment":
                db.execute(text(
                    "INSERT INTO user_roles(user_id,role_id) "
                    "SELECT 1,id FROM roles WHERE system_key='reviewer'"
                ))
            elif new_state == "quota":
                db.execute(text("INSERT INTO user_quotas(user_id) VALUES(1)"))
        with pytest.raises(RuntimeError, match="不能无损降级"):
            command.downgrade(config, "0028")
        assert {"roles", "user_roles", "user_quotas"} <= set(inspect(engine).get_table_names())
        with engine.connect() as db:
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0029"
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
