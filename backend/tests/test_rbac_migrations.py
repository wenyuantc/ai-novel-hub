"""0028存量向前升级不重写身份/会话；新语义存在时禁止有损降级。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.permissions import BUILTIN_ROLES
from app.core.security import hash_password
from app.main import create_app
from tests.helpers import make_settings


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
        command.upgrade(config, "0029")
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
                # 历史0029冻结33项。备份权限由0030、AI用量权限由0061加入超管角色。
                expected = {code for code in BUILTIN_ROLES[key][2]
                            if not code.startswith(("backups.", "ai_usage."))}
                assert set(json.loads(permissions)) == expected
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


def test_0061_upgraded_database_keeps_existing_admin_and_usage_access(tmp_path):
    config, engine = migration_config(tmp_path)
    username, password = "legacy_admin", "test-only-password"
    try:
        command.upgrade(config, "0060")
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO users (id, username, password_hash, is_admin) VALUES (1, :name, :password_hash, TRUE)"
            ), {"name": username, "password_hash": hash_password(password)})
        command.upgrade(config, "0061")
        # 当前应用需要完整的迁移头，0061 在 0060 旧账号上单独执行。
        command.upgrade(config, "head")

        async def verify_http() -> None:
            settings = make_settings(tmp_path, database_url=config.get_main_option("sqlalchemy.url"),
                                     admin_username=username, admin_password=password)
            app = create_app(settings, start_task_engine=False)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            ) as client:
                login = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
                assert login.status_code == 200 and login.json()["id"] == 1
                assert "ai_usage.read" in login.json()["effective_permissions"]
                usage = await client.get("/api/v1/admin/ai-usage")
                assert usage.status_code == 200 and usage.json()["tracking_since"] is not None

        asyncio.run(verify_http())
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
