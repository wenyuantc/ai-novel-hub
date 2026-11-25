"""排空设置迁移兼容旧覆盖，并保护新覆盖及审计。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import select, text

from app.models import AdminAuditLog, SystemSettings
from tests.test_task_worker_migration import migration_db as migration_db


async def test_drain_migration_preserves_legacy_data_and_guards_downgrade(migration_db):
    config, engine = migration_db
    old_settings = {"registration_enabled": False, "task_max_per_user": 3}
    settings_table = SystemSettings.__table__
    audit_table = AdminAuditLog.__table__
    await asyncio.to_thread(command.upgrade, config, "0069")
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO users (id,username,password_hash) VALUES (1,'旧账号','x')"))
        await db.execute(settings_table.insert().values(
            id=1, revision=1, overrides=old_settings, last_operation_id=str(uuid4()), updated_at=datetime.now(UTC),
        ))
        await db.execute(audit_table.insert().values(
            operation_id=str(uuid4()), actor_user_id=1, actor_username="旧账号", action="system_settings.update",
            result="success", target_type="system_settings", target_id=1,
            summary={"settings_changes": [{"key": "registration_enabled"}]},
        ))

    await asyncio.to_thread(command.upgrade, config, "0070")
    async with engine.begin() as db:
        assert await db.scalar(select(settings_table.c.overrides)) == old_settings
        assert await db.scalar(text("SELECT username FROM users")) == "旧账号"
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0070"
        # 显式 false 也是旧版不会识别的覆盖键。
        await db.execute(settings_table.update().values(overrides={**old_settings, "task_admission_closed": False}))
    with pytest.raises(RuntimeError, match="排空设置覆盖"):
        await asyncio.to_thread(command.downgrade, config, "0069")

    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0070"
        assert await db.scalar(select(settings_table.c.overrides)) == {**old_settings, "task_admission_closed": False}
        await db.execute(settings_table.update().values(overrides=old_settings))
        audit = await db.execute(audit_table.insert().values(
            operation_id=str(uuid4()), actor_user_id=1, actor_username="旧账号", action="system_settings.update",
            result="success", target_type="system_settings", target_id=1,
            summary={"settings_changes": [{"key": "task_admission_closed"}]},
        ).returning(audit_table.c.id))
        new_audit_id = audit.scalar_one()
    with pytest.raises(RuntimeError, match="排空设置审计"):
        await asyncio.to_thread(command.downgrade, config, "0069")

    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0070"
        assert await db.scalar(select(audit_table.c.summary).where(audit_table.c.id == new_audit_id)) == {
            "settings_changes": [{"key": "task_admission_closed"}],
        }
        await db.execute(audit_table.delete().where(audit_table.c.id == new_audit_id))
    await asyncio.to_thread(command.downgrade, config, "0069")
    async with engine.connect() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0069"
        assert await db.scalar(select(settings_table.c.overrides)) == old_settings
        assert await db.scalar(select(audit_table.c.summary)) == {"settings_changes": [{"key": "registration_enabled"}]}
