"""向导替换词典时，不能继续发送旧 AI 可见性快照。"""

import asyncio

import pytest

from app.ai.providers import OpenAICompatProvider
from app.models import SettingAiLink, SettingReferenceHandle
from app.services import ai_dispatch, wizard_service
from app.services.ai_dispatch import VisibilityBusyError, VisibilityChangedError, guard_provider, read_visibility_stamp
from tests.helpers import create_project, register
from tests.test_ai_visibility_dispatch import _collect, _server, _sse


@pytest.mark.parametrize("kind", ["characters", "world", "careers"])
async def test_wizard_replacement_invalidates_prior_visibility(app, client, kind):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    path, payload, raw = {
        "characters": ("characters", {"name": "原人物"}, '[{"name":"新人物"}]'),
        "world": ("world", {"title": "原地点", "category": "place"}, '[{"title":"新地点"}]'),
        "careers": ("careers", {"name": "原职业", "stages": [{"name": "起步"}]},
                    '[{"name":"新职业","stages":[{"name":"起步"}]}]'),
    }[kind]
    assert (await client.post(f"{base}/{path}", json=payload)).status_code == 201
    async with app.state.sessionmaker() as db:
        before = await read_visibility_stamp(db, project["id"])
    async with app.state.sessionmaker() as db:
        await wizard_service.save_wizard_result(db, project["id"], kind, raw)
    async with app.state.sessionmaker() as db:
        after = await read_visibility_stamp(db, project["id"])
    assert after.revision == before.revision + 1
    assert after.epoch == before.epoch + 1


async def test_new_candidate_competition_invalidates_old_context(app, client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    assert (await client.post(f"{base}/characters", json={"name": "青云"})).status_code == 201
    async with app.state.sessionmaker() as db:
        before = await read_visibility_stamp(db, project["id"])
    async with app.state.sessionmaker() as db:
        await wizard_service.save_wizard_result(
            db, project["id"], "world", '[{"title":"青云山","category":"place"}]',
        )
    async with app.state.sessionmaker() as db:
        after = await read_visibility_stamp(db, project["id"])
    assert after.epoch == before.epoch + 1


async def test_new_noncompeting_candidate_does_not_bump_visibility(app, client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    assert (await client.post(f"{base}/characters", json={"name": "青云"})).status_code == 201
    async with app.state.sessionmaker() as db:
        before = await read_visibility_stamp(db, project["id"])
    async with app.state.sessionmaker() as db:
        await wizard_service.save_wizard_result(
            db, project["id"], "world", '[{"title":"碧海","category":"place"}]',
        )
    async with app.state.sessionmaker() as db:
        after = await read_visibility_stamp(db, project["id"])
    assert after == before


async def test_replacement_with_deleted_link_bumps_epoch_only_once(app, client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = await client.post(f"{base}/characters", json={"name": "旧角色"})
    world = await client.post(f"{base}/world", json={"title": "旧世界"})
    assert character.status_code == world.status_code == 201
    async with app.state.sessionmaker() as db:
        source = SettingReferenceHandle(project_id=project["id"], kind="character", character_id=character.json()["id"])
        target = SettingReferenceHandle(project_id=project["id"], kind="world", world_setting_id=world.json()["id"])
        db.add_all([source, target])
        await db.flush()
        db.add(SettingAiLink(project_id=project["id"], source_handle_id=source.id, target_handle_id=target.id))
        await db.commit()
    async with app.state.sessionmaker() as db:
        before = await read_visibility_stamp(db, project["id"])
    async with app.state.sessionmaker() as db:
        await wizard_service.save_wizard_result(db, project["id"], "characters", '[{"name":"新角色"}]')
    async with app.state.sessionmaker() as db:
        after = await read_visibility_stamp(db, project["id"])
    assert after.revision == before.revision + 1
    assert after.epoch == before.epoch + 1


async def test_real_http_send_with_old_snapshot_is_rejected_after_wizard_replace(app, client):
    await register(client)
    project = await create_project(client)
    created = await client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": "旧角色"})
    assert created.status_code == 201
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        async with app.state.sessionmaker() as db:
            await wizard_service.save_wizard_result(db, project["id"], "characters", '[{"name":"新角色"}]')
        with pytest.raises(VisibilityChangedError):
            await _collect(provider.generate_stream([{"role": "user", "content": "旧上下文"}], "test-model"))
        assert requests == []


async def test_real_http_send_holds_visibility_until_headers_before_wizard_replace(app, client, monkeypatch):
    await register(client)
    project = await create_project(client)
    created = await client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": "旧角色"})
    assert created.status_code == 201
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.15)
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        sending = asyncio.create_task(_collect(provider.generate_stream(
            [{"role": "user", "content": "旧上下文"}], "test-model",
        )))
        try:
            await asyncio.wait_for(received.wait(), 5)
            async with app.state.sessionmaker() as db:
                with pytest.raises(VisibilityBusyError):
                    await wizard_service.save_wizard_result(db, project["id"], "characters", '[{"name":"新角色"}]')
            headers.set()
            async with app.state.sessionmaker() as db:
                await wizard_service.save_wizard_result(db, project["id"], "characters", '[{"name":"新角色"}]')
            assert not sending.done() and len(requests) == 1
            body.set()
            assert await asyncio.wait_for(sending, 5) == ["ok"]
        finally:
            headers.set()
            body.set()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
