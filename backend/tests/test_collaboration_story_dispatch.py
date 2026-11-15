"""人工设定修改与真实 AI 出站请求的 SQLite 发送门禁。"""

import asyncio

import pytest
from sqlalchemy import event, text, update

from app.ai.providers import OpenAICompatProvider
from app.models import Character, Project
from app.services import ai_dispatch, project_access
from app.services.ai_dispatch import VisibilityBusyError, VisibilityChangedError, guard_provider, read_visibility_stamp
from tests.helpers import create_project, register
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_story_access import _invite


@pytest.mark.parametrize(
    "operation",
    ["character_create", "character_pin", "character_delete", "world_category", "world_delete",
     "career_delete", "organization_delete"],
)
async def test_editor_setting_visibility_write_respects_real_send_timeout(
    app, client, make_client, monkeypatch, operation,
):
    await register(client, "dispatch-story-owner")
    editor = make_client()
    await register(editor, "dispatch-story-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "角色"})).json()
    world = (await client.post(f"{base}/world", json={"title": "世界"})).json()
    career = (await client.post(f"{base}/careers", json={
        "name": "职业", "stages": [{"name": "入门"}],
    })).json()
    org = (await client.post(f"{base}/organizations", json={"name": "组织"})).json()
    await _invite(client, editor, project["id"], "dispatch-story-editor", "editor")
    async with app.state.sessionmaker() as db:
        (await db.get(Character, character["id"])).ai_mode = None
        await db.commit()
        stamp = await read_visibility_stamp(db, project["id"])
    methods = {
        "character_create": ("POST", f"{base}/characters", {"name": "新角色"}, 201),
        "character_pin": ("PATCH", f"{base}/characters/{character['id']}", {"always_in_context": True}, 200),
        "character_delete": ("DELETE", f"{base}/characters/{character['id']}", None, 204),
        "world_category": ("PATCH", f"{base}/world/{world['id']}", {"category": "faction"}, 200),
        "world_delete": ("DELETE", f"{base}/world/{world['id']}", None, 204),
        "career_delete": ("DELETE", f"{base}/careers/{career['id']}", None, 204),
        "organization_delete": ("DELETE", f"{base}/organizations/{org['id']}", None, 204),
    }
    method, path, payload, success = methods[operation]
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        sending = asyncio.create_task(_collect(provider.generate_stream([{"role": "user", "content": "设定"}], "test")))
        writing = None
        try:
            await asyncio.wait_for(received.wait(), 5)
            writing = asyncio.create_task(editor.request(method, path, json=payload))
            blocked = await asyncio.wait_for(asyncio.shield(writing), 1)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            assert len((await editor.get(f"{base}/characters")).json()) == 1
            assert (await editor.get(f"{base}/characters/{character['id']}")).json()["always_in_context"] is False
            assert (await editor.get(f"{base}/world/{world['id']}")).json()["category"] == "other"
            assert (await editor.get(f"{base}/careers/{career['id']}")).status_code == 200
            assert (await editor.get(f"{base}/organizations/{org['id']}")).status_code == 200
            headers.set()
            body.set()
            assert await asyncio.wait_for(sending, 5) == ["ok"]
            saved = await editor.request(method, path, json=payload)
            assert saved.status_code == success, saved.text
            if operation == "character_pin":
                with pytest.raises(VisibilityChangedError):
                    await _collect(provider.generate_stream([{"role": "user", "content": "设定"}], "test"))
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            if writing is not None and not writing.done():
                await asyncio.gather(writing, return_exceptions=True)
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


async def test_visibility_lock_requires_project_write_lock(app, client):
    owner = await register(client, "dispatch-invalid-lock-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ValueError, match="可见性写门禁必须同时锁定项目"):
            await project_access.resolve_access(db, owner["id"], project["id"], visibility_write=True)


async def test_visibility_busy_restores_timeout_before_connection_returns_to_pool(app, client, monkeypatch):
    owner = await register(client, "dispatch-timeout-owner")
    project = await create_project(client)
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.1)
    observed = []
    engine = app.state.engine.sync_engine
    async with app.state.sessionmaker() as contender, app.state.sessionmaker() as holder:
        await contender.execute(text("PRAGMA busy_timeout = 4321"))
        connection = await contender.connection()
        raw = await connection.get_raw_connection()
        target = raw.dbapi_connection

        def checkin(dbapi_connection, _):
            if dbapi_connection is target:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA busy_timeout")
                observed.append(cursor.fetchone()[0])
                cursor.close()

        event.listen(engine, "checkin", checkin)
        try:
            await holder.execute(update(Project).where(Project.id == project["id"]).values(title=Project.title))
            with pytest.raises(VisibilityBusyError):
                await project_access.resolve_access(
                    contender, owner["id"], project["id"], for_update=True, visibility_write=True,
                )
            assert observed == [4321]
        finally:
            event.remove(engine, "checkin", checkin)
            await holder.rollback()
