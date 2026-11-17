"""图片备份、卡片及效果在 PostgreSQL 外键和事务下的真实往返。"""

import asyncio
import base64
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.errors import ConflictError
from app.models import Character
from app.models.setting_images import SettingImageResource
from app.services import setting_image_service
from app.services.agent import effects
from app.services.agent.access import lock_agent_write
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import ToolCtx
from app.services.project_transfer import thumbnails
from tests import pg_support
from tests.helpers import create_project, register
from tests.test_series_postgresql import _wait_for_blocker
from tests.test_thumbnail_transfer_effects import (
    make_entry,
    png,
)
from tests.test_thumbnail_transfer_effects import (
    test_backup_aborts_if_actor_or_project_changes_during_staging as _staging,
)
from tests.test_thumbnail_transfer_effects import (
    test_backup_and_card_images_roundtrip_with_explicit_selection as _roundtrip,
)
from tests.test_thumbnail_transfer_effects import (
    test_backup_spool_rejects_size_and_image_version_changes as _spool,
)
from tests.test_thumbnail_transfer_effects import (
    test_effects_restore_images_and_reject_later_images as _effects,
)
from tests.test_thumbnail_transfer_effects import (
    test_shared_images_export_as_independent_book_resources as _shared,
)
from tests.test_thumbnail_transfer_effects import (
    test_thumbnail_undo_cannot_override_later_series_binding as _series_undo,
)
from tests.test_thumbnail_transfer_lifecycle import _exercise_recycled_then_restored


@pytest.fixture
async def pg_client(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="缩略图传输测试")
    async with pg_support.postgres_apps(pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=1) as apps:
        app = apps[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, app


@pytest.fixture
async def pg_race(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="缩略图并发测试")
    # 两个应用实例各自持有授权 runtime，避免单进程内互斥掩盖 PostgreSQL 实际锁等待。
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2, separate_settings=True,
    ) as apps:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[0]), base_url="http://testserver",
        ) as client:
            yield client, apps[0], apps[1]


async def test_postgresql_thumbnail_backup_and_cards(pg_client):
    client, _ = pg_client
    await _roundtrip(client)


async def test_postgresql_thumbnail_effects(pg_client):
    client, app = pg_client
    await _effects(client, app)


async def test_postgresql_shared_images_export(pg_client):
    client, app = pg_client
    await _shared(client, app)


async def test_postgresql_shared_images_undo(pg_client):
    client, app = pg_client
    await _series_undo(client, app)


async def test_postgresql_backup_version_and_size_conflicts(pg_client, monkeypatch):
    client, app = pg_client
    await _spool(client, app, monkeypatch)


@pytest.mark.parametrize("change", ["actor", "project"])
async def test_postgresql_backup_revoked_during_staging(pg_client, monkeypatch, change):
    client, app = pg_client
    await _staging(client, app, monkeypatch, change)


@pytest.mark.parametrize("phase", ["staging", "sending"])
async def test_postgresql_backup_does_not_resume_after_recycle_restore(pg_client, phase):
    client, app = pg_client
    await _exercise_recycled_then_restored(client, app, phase)


async def _agent_change(app, owner_id, project_id, tool_name, args, gate=None):
    """经过实际 Agent 写门禁、工具和 effects 捕获，事务由调用方路径提交。"""
    async with app.state.sessionmaker() as db:
        project = await lock_agent_write(db, owner_id, project_id, grant=None)
        before = await effects.snapshot(db, project_id)
        ctx = ToolCtx(db, project, owner_id, app.state.task_engine.ctx, visibility_locked=True)
        result = await get_tool(tool_name).execute(ctx, args)
        assert "error" not in result, result
        changes = effects.diff(before, await effects.snapshot(db, project_id))
        if gate is not None:
            gate["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            gate["ready"].set()
            await gate["release"].wait()
        await db.commit()
        return result, changes


async def _agent_undo(app, owner_id, project_id, changes, gate=None):
    async with app.state.sessionmaker() as db:
        await lock_agent_write(db, owner_id, project_id, grant=None)
        await effects.undo(db, project_id, changes)
        if gate is not None:
            gate["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            gate["ready"].set()
            await gate["release"].wait()
        await db.commit()


def _gate():
    return {"ready": asyncio.Event(), "release": asyncio.Event()}


async def _stop(*tasks):
    for task in tasks:
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


def _hold_uploaded_binding(monkeypatch, gate):
    original = setting_image_service.set_book_image_without_commit

    async def held(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        gate["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        gate["ready"].set()
        await gate["release"].wait()
        return result

    monkeypatch.setattr(setting_image_service, "set_book_image_without_commit", held)


async def _replace(client, route, version):
    return await client.put(route, files={"file": ("blue.png", png("blue"), "image/png")},
                            data={"expected_version": version})


@pytest.mark.parametrize("winner", ["image", "agent"])
async def test_postgresql_replace_and_agent_delete_are_serialized(pg_race, monkeypatch, winner):
    client, app, agent_app = pg_race
    owner = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    char, route, old = await make_entry(client, base, "character", "characters", {"name": "甲"})
    old_bytes = (await client.get(route)).content
    gate = _gate()
    upload_task = delete_task = None
    try:
        if winner == "image":
            _hold_uploaded_binding(monkeypatch, gate)
            upload_task = asyncio.create_task(_replace(client, route, old["version"]))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            delete_task = asyncio.create_task(_agent_change(
                agent_app, owner["id"], project["id"], "delete_character", {"id": char["id"]},
            ))
        else:
            delete_task = asyncio.create_task(_agent_change(
                agent_app, owner["id"], project["id"], "delete_character", {"id": char["id"]}, gate,
            ))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            upload_task = asyncio.create_task(_replace(client, route, old["version"]))
        await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, gate["pid"]), 15)
        gate["release"].set()
        upload = await asyncio.wait_for(upload_task, 15)
        _, changes = await asyncio.wait_for(delete_task, 15)
        assert upload.status_code == (200 if winner == "image" else 404), upload.text
        expected_version = upload.json()["version"] if winner == "image" else old["version"]
        image_effect = next(row for row in changes if row["table"] == "setting_thumbnail_bindings")
        assert image_effect["before"]["image_resource_id"] == expected_version
        assert (await client.get(route)).status_code == 404
        await _agent_undo(agent_app, owner["id"], project["id"], changes)
        restored = await client.get(route)
        assert restored.status_code == 200 and restored.headers["etag"] == f'"{expected_version}"'
        assert (restored.content == old_bytes) is (winner == "agent")
    finally:
        gate["release"].set()
        await _stop(upload_task, delete_task)


@pytest.mark.parametrize("winner", ["image", "agent"])
async def test_postgresql_replace_and_agent_undo_create_are_serialized(pg_race, monkeypatch, winner):
    client, app, agent_app = pg_race
    owner = await register(client)
    project = await create_project(client)
    result, changes = await _agent_change(
        agent_app, owner["id"], project["id"], "create_character", {"name": "新角色"},
    )
    char_id = result["id"]
    route = f"/api/v1/projects/{project['id']}/settings/character/{char_id}/thumbnail"
    gate = _gate()
    upload_task = undo_task = None
    try:
        if winner == "image":
            _hold_uploaded_binding(monkeypatch, gate)
            upload_task = asyncio.create_task(_replace(client, route, ""))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            undo_task = asyncio.create_task(_agent_undo(agent_app, owner["id"], project["id"], changes))
        else:
            undo_task = asyncio.create_task(_agent_undo(agent_app, owner["id"], project["id"], changes, gate))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            upload_task = asyncio.create_task(_replace(client, route, ""))
        await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, gate["pid"]), 15)
        gate["release"].set()
        upload = await asyncio.wait_for(upload_task, 15)
        if winner == "image":
            assert upload.status_code == 200, upload.text
            with pytest.raises(ConflictError) as exc:
                await asyncio.wait_for(undo_task, 15)
            assert exc.value.extra["code"] == "undo_conflict"
            current = await client.get(route)
            assert current.status_code == 200 and current.headers["etag"] == f'"{upload.json()["version"]}"'
        else:
            await asyncio.wait_for(undo_task, 15)
            assert upload.status_code == 404, upload.text
            assert (await client.get(route)).status_code == 404
        async with app.state.sessionmaker() as db:
            exists = await db.scalar(select(Character.id).where(Character.id == char_id))
            count = await db.scalar(select(func.count()).select_from(SettingImageResource).where(
                SettingImageResource.project_id == project["id"],
            ))
            assert (exists is not None) is (winner == "image")
            assert count == (1 if winner == "image" else 0)
    finally:
        gate["release"].set()
        await _stop(upload_task, undo_task)


@pytest.mark.parametrize("winner", ["image", "agent"])
async def test_postgresql_replace_and_agent_undo_update_are_serialized(pg_race, monkeypatch, winner):
    client, app, agent_app = pg_race
    owner = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    char, route, old = await make_entry(client, base, "character", "characters", {"name": "甲"})
    _, changes = await _agent_change(agent_app, owner["id"], project["id"], "update_character", {
        "id": char["id"], "profile": "由智能体修改",
    })
    assert not any(row["table"] == "setting_thumbnail_bindings" for row in changes)
    gate = _gate()
    upload_task = undo_task = None
    try:
        if winner == "image":
            _hold_uploaded_binding(monkeypatch, gate)
            upload_task = asyncio.create_task(_replace(client, route, old["version"]))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            undo_task = asyncio.create_task(_agent_undo(agent_app, owner["id"], project["id"], changes))
        else:
            undo_task = asyncio.create_task(_agent_undo(agent_app, owner["id"], project["id"], changes, gate))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            upload_task = asyncio.create_task(_replace(client, route, old["version"]))
        await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, gate["pid"]), 15)
        gate["release"].set()
        upload = await asyncio.wait_for(upload_task, 15)
        await asyncio.wait_for(undo_task, 15)
        assert upload.status_code == 200, upload.text
        image = await client.get(route)
        assert image.status_code == 200 and image.headers["etag"] == f'"{upload.json()["version"]}"'
        restored = await client.get(f"{base}/characters/{char['id']}")
        assert restored.status_code == 200 and restored.json()["profile"] == ""
    finally:
        gate["release"].set()
        await _stop(upload_task, undo_task)


@pytest.mark.parametrize("winner", ["backup", "image"])
async def test_postgresql_backup_and_replace_observe_one_image_version(pg_race, monkeypatch, winner):
    client, app, _ = pg_race
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    _, route, old = await make_entry(client, base, "character", "characters", {"name": "甲"})
    old_bytes = (await client.get(route)).content
    gate = _gate()
    backup_task = upload_task = None
    try:
        if winner == "backup":
            _hold_uploaded_binding(monkeypatch, gate)
            upload_task = asyncio.create_task(_replace(client, route, old["version"]))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            backup_task = asyncio.create_task(client.get(f"{base}/backup"))
            backup = await asyncio.wait_for(backup_task, 15)
            assert backup.status_code == 200, backup.text
            assert base64.b64decode(backup.json()["thumbnails"][0]["data"]) == old_bytes
            gate["release"].set()
            upload = await asyncio.wait_for(upload_task, 15)
            assert upload.status_code == 200, upload.text
            assert (await client.get(route)).content != old_bytes
        else:
            original = thumbnails.iter_serialized

            async def held(*args, **kwargs):
                async for item in original(*args, **kwargs):
                    yield item
                    gate["ready"].set()
                    await gate["release"].wait()

            monkeypatch.setattr(thumbnails, "iter_serialized", held)
            backup_task = asyncio.create_task(client.get(f"{base}/backup"))
            await asyncio.wait_for(gate["ready"].wait(), 15)
            upload_task = asyncio.create_task(_replace(client, route, old["version"]))
            upload = await asyncio.wait_for(upload_task, 15)
            assert upload.status_code == 200, upload.text
            gate["release"].set()
            backup = await asyncio.wait_for(backup_task, 15)
            assert backup.status_code == 409 and backup.json()["code"] == "thumbnail_changed", backup.text
    finally:
        gate["release"].set()
        await _stop(upload_task, backup_task)
