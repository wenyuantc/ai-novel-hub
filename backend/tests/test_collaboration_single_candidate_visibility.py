"""单项设定候选变化不能复用旧 AI 载荷或越过真实发送门禁。"""

import asyncio

import pytest
from sqlalchemy import event, select

from app.ai.providers import OpenAICompatProvider
from app.core.errors import ForbiddenError
from app.models import AgentMessage, Career, Chapter, Character, Organization, Project, WorldSetting
from app.schemas.career import CareerCreate, CareerUpdate
from app.schemas.organization import OrganizationCreate, OrganizationUpdate
from app.schemas.story import CharacterCreate, CharacterUpdate, WorldSettingCreate, WorldSettingUpdate
from app.services import ai_dispatch, career_service, organization_service, story_service
from app.services.ai_dispatch import VisibilityChangedError, guard_provider
from app.services.setting_ai_context import build_scope
from tests.helpers import create_project, register
from tests.test_agent_runtime import setup_agent
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_rewrite_identity import _http_clients
from tests.test_collaboration_story_access import _invite

KINDS = ("character", "world", "career", "organization")


def _create(kind, name):
    if kind == "character":
        return "/characters", {"name": name}
    if kind == "world":
        return "/world", {"title": name}
    if kind == "career":
        return "/careers", {"name": name, "stages": [{"name": "入门"}]}
    return "/organizations", {"name": name}


async def _setup(owner, editor):
    await register(owner, "single-candidate-owner")
    await register(editor, "single-candidate-editor")
    project = await create_project(owner)
    await _invite(owner, editor, project["id"], "single-candidate-editor", "editor")
    return project, f"/api/v1/projects/{project['id']}"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("competes", [True, False])
async def test_single_create_rechecks_old_scope_and_epoch(app, kind, competes):
    async with _http_clients(app) as (owner, editor):
        project, base = await _setup(owner, editor)
        original = await owner.post(f"{base}/characters", json={"name": "青鸿", "profile": "原唯一候选"})
        assert original.status_code == 201, original.text
        async with app.state.sessionmaker() as db:
            old = await build_scope(db, project["id"], texts=("青鸿",))
            assert old.includes("character", original.json()["id"])
        name = "青鸿" if competes else "远行"
        path, payload = _create(kind, name)
        result = await editor.post(base + path, json=payload)
        assert result.status_code == 201, result.text
        async with app.state.sessionmaker() as db:
            current = await build_scope(db, project["id"], texts=("青鸿",))
            row = await db.get(Project, project["id"])
            assert current.includes("character", original.json()["id"]) is not competes
            assert current.stamp.revision == old.stamp.revision + int(competes)
            assert row.ai_history_epoch == old.stamp.epoch + int(competes)
        async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
            headers.set()
            body.set()
            provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), old.stamp,
                                      app.state.sessionmaker)
            if competes:
                with pytest.raises(VisibilityChangedError):
                    await _collect(provider.generate_stream([{"role": "user", "content": "旧载荷"}], "test"))
                assert requests == []
            else:
                result = await _collect(provider.generate_stream([{"role": "user", "content": "旧载荷"}], "test"))
                assert result == ["ok"]
                assert len(requests) == 1


@pytest.mark.parametrize("case", [
    "character_name", "character_alias", "world_title", "world_alias", "career_name", "organization_name",
])
async def test_single_identity_update_invalidates_competing_scope(app, case):
    kind, field = case.split("_")
    async with _http_clients(app) as (owner, editor):
        project, base = await _setup(owner, editor)
        original = await owner.post(f"{base}/characters", json={"name": "青鸿", "profile": "原唯一候选"})
        path, payload = _create(kind, "远行")
        target = await owner.post(base + path, json=payload)
        assert original.status_code == target.status_code == 201
        async with app.state.sessionmaker() as db:
            old = await build_scope(db, project["id"], texts=("青鸿",))
            assert old.includes("character", original.json()["id"])
        update = {"aliases": ["青鸿"]} if field == "alias" else {field: "青鸿"}
        result = await editor.patch(f"{base}{path}/{target.json()['id']}", json=update)
        assert result.status_code == 200, result.text
        async with app.state.sessionmaker() as db:
            current = await build_scope(db, project["id"], texts=("青鸿",))
            assert not current.includes("character", original.json()["id"])
            assert current.stamp.revision == old.stamp.revision + 1
            assert current.stamp.epoch == old.stamp.epoch + 1


@pytest.mark.parametrize("kind", ["character", "world"])
async def test_removed_alias_invalidates_old_unique_match(app, kind):
    async with _http_clients(app) as (owner, editor):
        project, base = await _setup(owner, editor)
        path, payload = _create(kind, "远行")
        payload["aliases"] = ["青鸿"]
        target = await owner.post(base + path, json=payload)
        assert target.status_code == 201, target.text
        async with app.state.sessionmaker() as db:
            old = await build_scope(db, project["id"], texts=("青鸿",))
            assert old.includes(kind, target.json()["id"])
        result = await editor.patch(f"{base}{path}/{target.json()['id']}", json={"aliases": []})
        assert result.status_code == 200, result.text
        async with app.state.sessionmaker() as db:
            current = await build_scope(db, project["id"], texts=("青鸿",))
            assert not current.includes(kind, target.json()["id"])
            assert current.stamp.revision == old.stamp.revision + 1
            assert current.stamp.epoch == old.stamp.epoch + 1


@pytest.mark.parametrize("kind", ["character", "world"])
async def test_noncompeting_alias_addition_and_equal_update_keep_epoch(app, kind):
    async with _http_clients(app) as (owner, editor):
        project, base = await _setup(owner, editor)
        path, payload = _create(kind, "青鸿")
        target = await owner.post(base + path, json=payload)
        assert target.status_code == 201, target.text
        target_path = f"{base}{path}/{target.json()['id']}"
        async with app.state.sessionmaker() as db:
            old = (await build_scope(db, project["id"])).stamp
        name_field = "name" if kind == "character" else "title"
        result = await editor.patch(target_path, json={name_field: "青鸿", "aliases": ["远行"]})
        assert result.status_code == 200, result.text
        async with app.state.sessionmaker() as db:
            assert (await build_scope(db, project["id"])).stamp == old
        result = await editor.patch(target_path, json={name_field: "青鸿", "aliases": ["远行"]})
        assert result.status_code == 200, result.text
        async with app.state.sessionmaker() as db:
            assert (await build_scope(db, project["id"])).stamp == old


async def test_other_project_candidate_does_not_change_scope_or_epoch(app, client):
    await register(client, "candidate-project-owner")
    local, foreign = await create_project(client), await create_project(client, title="异书")
    local_base = f"/api/v1/projects/{local['id']}"
    original = await client.post(f"{local_base}/characters", json={"name": "青鸿"})
    assert original.status_code == 201, original.text
    async with app.state.sessionmaker() as db:
        old = await build_scope(db, local["id"], texts=("青鸿",))
        assert old.includes("character", original.json()["id"])
    foreign_base = f"/api/v1/projects/{foreign['id']}"
    result = await client.post(f"{foreign_base}/world", json={"title": "青鸿"})
    assert result.status_code == 201, result.text
    async with app.state.sessionmaker() as db:
        current = await build_scope(db, local["id"], texts=("青鸿",))
        assert current.includes("character", original.json()["id"])
        assert current.stamp == old.stamp


async def test_competing_single_create_seals_existing_agent_history(app, client, fake_llm):
    project, _agent_base, conversation_url = await setup_agent(client)
    base = f"/api/v1/projects/{project['id']}"
    original = await client.post(f"{base}/characters", json={"name": "青鸿", "profile": "先前唯一命中"})
    assert original.status_code == 201, original.text
    conversation_id = int(conversation_url.rsplit("/", 1)[1])
    async with app.state.sessionmaker() as db:
        db.add(AgentMessage(conversation_id=conversation_id, role="assistant", content="旧会话已有结果"))
        await db.commit()
    before = await client.get(conversation_url)
    assert before.status_code == 200 and not before.json()["history_sealed"], before.text
    added = await client.post(f"{base}/world", json={"title": "青鸿"})
    assert added.status_code == 201, added.text
    after = await client.get(conversation_url)
    assert after.status_code == 200 and after.json()["history_sealed"], after.text
    rejected = await client.post(conversation_url + "/messages", json={"content": "继续使用旧资料"})
    assert rejected.status_code == 409 and rejected.json()["code"] == "ai_history_sealed", rejected.text
    assert not fake_llm.tool_calls


@pytest.mark.parametrize("kind", KINDS)
async def test_single_delete_invalidates_old_scope_without_links(app, kind):
    async with _http_clients(app) as (owner, editor):
        project, base = await _setup(owner, editor)
        path, payload = _create(kind, "青鸿")
        target = await owner.post(base + path, json=payload)
        assert target.status_code == 201, target.text
        async with app.state.sessionmaker() as db:
            old = await build_scope(db, project["id"], texts=("青鸿",))
            assert old.includes(kind, target.json()["id"])
        result = await editor.delete(f"{base}{path}/{target.json()['id']}")
        assert result.status_code == 204, result.text
        async with app.state.sessionmaker() as db:
            current = await build_scope(db, project["id"], texts=("青鸿",))
            assert not current.includes(kind, target.json()["id"])
            assert current.stamp.revision == old.stamp.revision + 1
            assert current.stamp.epoch == old.stamp.epoch + 1


@pytest.mark.parametrize("operation", ["character", "world", "career", "organization"])
async def test_single_create_respects_real_send_gate(app, monkeypatch, operation):
    async with _http_clients(app) as (owner, editor):
        project, base = await _setup(owner, editor)
        path, payload = _create(operation, "门禁候选")
        async with app.state.sessionmaker() as db:
            stamp = (await build_scope(db, project["id"])).stamp
        monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
        async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
            provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp,
                                      app.state.sessionmaker)
            sending = asyncio.create_task(
                _collect(provider.generate_stream([{"role": "user", "content": "设定"}], "test"))
            )
            try:
                await asyncio.wait_for(received.wait(), 5)
                blocked = await asyncio.wait_for(editor.post(base + path, json=payload), 2)
                assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
                headers.set()
                body.set()
                assert await asyncio.wait_for(sending, 5) == ["ok"]
                saved = await editor.post(base + path, json=payload)
                assert saved.status_code == 201, saved.text
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                if not sending.done():
                    sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.parametrize("operation", ["character", "world", "career", "organization"])
async def test_single_identity_update_respects_real_send_gate(app, monkeypatch, operation):
    async with _http_clients(app) as (owner, editor):
        project, base = await _setup(owner, editor)
        path, payload = _create(operation, "旧名称")
        target = await owner.post(base + path, json=payload)
        assert target.status_code == 201, target.text
        target_path = f"{base}{path}/{target.json()['id']}"
        field = "title" if operation == "world" else "name"
        async with app.state.sessionmaker() as db:
            stamp = (await build_scope(db, project["id"])).stamp
        monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
        async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
            provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp,
                                      app.state.sessionmaker)
            sending = asyncio.create_task(
                _collect(provider.generate_stream([{"role": "user", "content": "设定"}], "test"))
            )
            try:
                await asyncio.wait_for(received.wait(), 5)
                blocked = await asyncio.wait_for(editor.patch(target_path, json={field: "新名称"}), 2)
                assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
                headers.set()
                body.set()
                assert await asyncio.wait_for(sending, 5) == ["ok"]
                saved = await editor.patch(target_path, json={field: "新名称"})
                assert saved.status_code == 200, saved.text
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                if not sending.done():
                    sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("update", [False, True])
async def test_denied_single_candidate_write_does_not_autoflush(app, client, make_client, kind, update):
    await register(client, "candidate-denial-owner")
    viewer = make_client()
    user = await register(viewer, "candidate-denial-viewer")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    path, payload = _create(kind, "原名称")
    target = await client.post(base + path, json=payload)
    assert target.status_code == 201, target.text
    await _invite(client, viewer, project["id"], "candidate-denial-viewer", "viewer")
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="未授权前不得写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(ForbiddenError):
            if kind == "character":
                if update:
                    await story_service.update_owned(db, story_service.CHARACTERS, user["id"], project["id"],
                                                     target.json()["id"], CharacterUpdate(name="新"), manage=True)
                else:
                    await story_service.create_owned(db, story_service.CHARACTERS, user["id"], project["id"],
                                                     CharacterCreate(name="新"), manage=True)
            elif kind == "world":
                if update:
                    await story_service.update_owned(db, story_service.WORLD, user["id"], project["id"],
                                                     target.json()["id"], WorldSettingUpdate(title="新"), manage=True)
                else:
                    await story_service.create_owned(db, story_service.WORLD, user["id"], project["id"],
                                                     WorldSettingCreate(title="新"), manage=True)
            elif kind == "career":
                if update:
                    await career_service.update_career(db, user["id"], project["id"], target.json()["id"],
                                                       CareerUpdate(name="新"), manual=True)
                else:
                    await career_service.create_career(db, user["id"], project["id"],
                                                       CareerCreate(**_create(kind, "新")[1]), manual=True)
            elif update:
                await organization_service.update_org(db, user["id"], project["id"], target.json()["id"],
                                                      OrganizationUpdate(name="新"), manual=True)
            else:
                await organization_service.create_org(db, user["id"], project["id"],
                                                      OrganizationCreate(name="新"), manual=True)
        assert flushes == [] and pending.id is None and pending in db.new
        await db.rollback()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("update", [False, True])
async def test_failed_candidate_revision_cannot_be_committed_later(app, client, monkeypatch, kind, update):
    owner = await register(client, "candidate-rollback-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    path, payload = _create(kind, "旧名称")
    target = await client.post(base + path, json=payload)
    assert target.status_code == 201, target.text

    async def fail(*_args, **_kwargs):
        raise RuntimeError("候选代次更新失败")

    if update:
        monkeypatch.setattr(story_service.setting_ai_visibility, "bump_visibility_revision", fail)
    else:
        monkeypatch.setattr(story_service, "_bump_new_candidate_competition", fail)
    ident = target.json()["id"]
    models = {"character": Character, "world": WorldSetting, "career": Career, "organization": Organization}
    model = models[kind]
    column = model.title if kind == "world" else model.name
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError, match="候选代次更新失败"):
            if kind == "character":
                if update:
                    await story_service.update_owned(db, story_service.CHARACTERS, owner["id"], project["id"], ident,
                                                     CharacterUpdate(name="新名称"))
                else:
                    await story_service.create_owned(db, story_service.CHARACTERS, owner["id"], project["id"],
                                                     CharacterCreate(name="不得留存"))
            elif kind == "world":
                if update:
                    await story_service.update_owned(db, story_service.WORLD, owner["id"], project["id"], ident,
                                                     WorldSettingUpdate(title="新名称"))
                else:
                    await story_service.create_owned(db, story_service.WORLD, owner["id"], project["id"],
                                                     WorldSettingCreate(title="不得留存"))
            elif kind == "career":
                if update:
                    await career_service.update_career(db, owner["id"], project["id"], ident,
                                                       CareerUpdate(name="新名称"))
                else:
                    await career_service.create_career(db, owner["id"], project["id"],
                                                       CareerCreate(**_create(kind, "不得留存")[1]))
            elif update:
                await organization_service.update_org(db, owner["id"], project["id"], ident,
                                                      OrganizationUpdate(name="新名称"))
            else:
                await organization_service.create_org(db, owner["id"], project["id"],
                                                      OrganizationCreate(name="不得留存"))
        await db.commit()  # 内部调用方捕获异常后继续提交，也不能留下无代次的改动。
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(column).where(model.id == ident)) == "旧名称"
        assert await db.scalar(select(model.id).where(model.project_id == project["id"], column == "不得留存")) is None
