import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import delete, inspect, select, text

from app.models import AdminAuditLog, AuthorizationState, Role, Task
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from tests.test_task_worker_migration import migration_db as migration_db


async def test_feature_plugin_schema_and_role_roundtrip(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0079")
    super_permissions = select(Role.permissions).where(Role.system_key == "super_admin")
    async with engine.connect() as db:
        before = await db.scalar(super_permissions)
        revision = await db.scalar(select(AuthorizationState.revision))
    await asyncio.to_thread(command.upgrade, cfg, "0080")
    async with engine.connect() as db:
        columns = await db.run_sync(lambda connection: inspect(connection).get_columns("feature_plugin_states"))
        assert {column["name"] for column in columns} == {
            "plugin_id",
            "enabled",
            "revision",
            "updated_at",
            "last_operation_id",
        }
        expected = before.copy()
        start = expected.index("settings.read")
        expected[start:start] = ["plugins.read", "plugins.update"]
        assert await db.scalar(super_permissions) == expected
        assert await db.scalar(select(AuthorizationState.revision)) == revision + 1
    await asyncio.to_thread(command.downgrade, cfg, "0079")
    async with engine.connect() as db:
        assert not await db.run_sync(lambda connection: inspect(connection).has_table("feature_plugin_states"))
        assert await db.scalar(super_permissions) == before


async def test_downgrade_refuses_plugin_authorization_or_audit_history(migration_db, tmp_path):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0080")
    async with engine.begin() as db:
        await db.execute(Role.__table__.insert().values(name="委派角色", permissions=["plugins.read"]))
    with pytest.raises(RuntimeError, match="仍有角色"):
        await asyncio.to_thread(command.downgrade, cfg, "0079")
    async with engine.begin() as db:
        await db.execute(delete(Role).where(Role.name == "委派角色"))
    for action, target, summary, error in (
        ("role.update", "role", {"rbac_change": {"permissions_before": ["plugins.read"]}}, "授权审计"),
        ("task.cancel_request", "task", {"task_kind": "sample.render"}, "任务取消审计"),
        ("feature_plugin.enable", "feature_plugin", {}, "管理操作日志"),
    ):
        async with engine.begin() as db:
            await db.execute(
                AdminAuditLog.__table__.insert().values(
                    operation_id=str(uuid4()),
                    action=action,
                    target_type=target,
                    actor_username="管理员",
                    result="success",
                    summary=summary,
                )
            )
        with pytest.raises(RuntimeError, match=error):
            await asyncio.to_thread(command.downgrade, cfg, "0079")
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0080"
            assert await db.scalar(select(AdminAuditLog.action)) == action
            await db.execute(delete(AdminAuditLog))
    async with engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO feature_plugin_states (plugin_id,enabled,revision,updated_at) "
                "VALUES ('sample', false, 3, CURRENT_TIMESTAMP)"
            )
        )
    if engine.dialect.name == "sqlite":
        snapshot, restored = tmp_path / "snapshot", tmp_path / "restored.db"
        await asyncio.to_thread(
            snapshot_database, str(engine.url), snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, restored) == "0080"
        with closing(sqlite3.connect(restored)) as copy:
            assert copy.execute("SELECT plugin_id, enabled, revision FROM feature_plugin_states").fetchall() == [
                ("sample", 0, 3),
            ]
    with pytest.raises(RuntimeError, match="状态或修订"):
        await asyncio.to_thread(command.downgrade, cfg, "0079")
    async with engine.begin() as db:
        await db.execute(text("DELETE FROM feature_plugin_states"))
        uid = await db.scalar(text("INSERT INTO users(username,password_hash) VALUES ('tester','unused') RETURNING id"))
        await db.execute(
            Task.__table__.insert().values(user_id=uid, kind="sample.render", status="cancelled", input={})
        )
    with pytest.raises(RuntimeError, match="任务类型"):
        await asyncio.to_thread(command.downgrade, cfg, "0079")
