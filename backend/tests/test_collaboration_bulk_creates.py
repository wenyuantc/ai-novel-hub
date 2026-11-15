"""协作者手工组合写入的权限、归属、事务和发送屏障。"""

import asyncio

import pytest
from sqlalchemy import event, func, select

from app.ai.providers import OpenAICompatProvider
from app.core.errors import BadRequestError, ForbiddenError, NotFoundError
from app.models import (
    Career,
    Chapter,
    Character,
    CharacterCareer,
    Organization,
    Project,
    Relationship,
    RelationshipType,
)
from app.schemas.career import CareerBatchCreate
from app.schemas.organization import OrganizationWithMembers
from app.schemas.relationship import CharacterWithRelations, RelationshipBatchCreate
from app.services import ai_dispatch, career_service, organization_service, relationship_service
from app.services.ai_dispatch import guard_provider, read_visibility_stamp
from app.services.setting_ai_context import build_scope
from tests.helpers import create_project, register
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_story_access import _invite


def _case(kind, tag, ids, career_id):
    if kind == "career":
        return "/careers/batch", {"careers": [{"name": f"{tag}职业", "stages": [{"name": "入门"}]}]}
    if kind == "organization":
        return "/organizations/with-members", {
            "organization": {"name": f"{tag}组织"}, "members": [{"character_id": ids[0]}],
        }
    if kind == "relationship":
        return "/relationships/batch", {
            "items": [{"a_id": ids[0], "b_id": ids[1], "role_a": "朋友"}],
        }
    return "/characters/with-relations", {
        "character": {"name": f"{tag}人物", "always_in_context": True},
        "relations": [{"target_id": ids[0], "role_a": "朋友"}],
        "careers": [{"career_id": career_id}],
    }


def _service(kind, payload):
    return {
        "career": (career_service.create_careers, CareerBatchCreate),
        "organization": (organization_service.create_with_members, OrganizationWithMembers),
        "relationship": (relationship_service.create_relationships, RelationshipBatchCreate),
        "character": (relationship_service.create_character_with_relations, CharacterWithRelations),
    }[kind][0], {
        "career": CareerBatchCreate, "organization": OrganizationWithMembers,
        "relationship": RelationshipBatchCreate, "character": CharacterWithRelations,
    }[kind](**payload)


@pytest.mark.parametrize("kind", ["career", "organization", "relationship", "character"])
async def test_bulk_create_manual_role_matrix_and_strict_default(
    app, client, make_client, system_admin, fake_llm, kind,
):
    await register(client, "bulk-owner")
    editor, viewer, stranger = make_client(), make_client(), make_client()
    editor_user = await register(editor, "bulk-editor")
    await register(viewer, "bulk-viewer")
    await register(stranger, "bulk-stranger")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for i in range(8):
        created = await client.post(f"{base}/characters", json={"name": f"角色{i}"})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    career = await client.post(f"{base}/careers", json={"name": "剑修", "stages": [{"name": "初阶"}]})
    assert career.status_code == 201, career.text
    await _invite(client, editor, project["id"], "bulk-editor", "editor")
    await _invite(client, viewer, project["id"], "bulk-viewer", "viewer")

    path, payload = _case(kind, "拒绝", chars[:2], career.json()["id"])
    assert (await viewer.post(base + path, json=payload)).status_code == 403
    assert (await stranger.post(base + path, json=payload)).status_code == 404
    for i, (actor, tag) in enumerate(((client, "作者"), (editor, "编辑"), (system_admin, "管理员"))):
        path, payload = _case(kind, tag, chars[2 * i:2 * i + 2], career.json()["id"])
        response = await actor.post(base + path, json=payload)
        assert response.status_code == 201, response.text
        if kind == "character":
            assert response.json()["character"]["always_in_context"] is True
            assert response.json()["relationships"][0]["other_id"] == chars[2 * i]
            assert response.json()["careers"][0]["career_id"] == career.json()["id"]
            assert "origin" not in response.json()["character"]

    # 管理员即使另有 viewer 身份，也保留 projects.edit 的并集权限。
    await _invite(client, system_admin, project["id"], "settings_admin", "viewer")
    path, payload = _case(kind, "管理员访客", chars[6:8], career.json()["id"])
    response = await system_admin.post(base + path, json=payload)
    assert response.status_code == 201, response.text
    service, data = _service(kind, payload)
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await service(db, editor_user["id"], project["id"], data)
    assert fake_llm.calls == [] and fake_llm.embed_calls == []


@pytest.mark.parametrize("kind", ["career", "organization", "relationship", "character"])
async def test_denial_does_not_autoflush_pending_changes(app, client, make_client, kind):
    await register(client, "bulk-flush-owner")
    viewer = make_client()
    user = await register(viewer, "bulk-flush-viewer")
    project = await create_project(client)
    await _invite(client, viewer, project["id"], "bulk-flush-viewer", "viewer")
    _, payload = _case(kind, "拒绝", [123, 456], 789)
    service, data = _service(kind, payload)
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="授权前不得写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(ForbiddenError):
            await service(db, user["id"], project["id"], data, manual=True)
        assert flushes == [] and pending.id is None and pending in db.new
        await db.rollback()


@pytest.mark.parametrize("kind", ["career", "organization", "character"])
async def test_combined_create_holds_real_send_visibility_gate(app, client, make_client, monkeypatch, kind):
    await register(client, "bulk-gate-owner")
    editor = make_client()
    await register(editor, "bulk-gate-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, project["id"], "bulk-gate-editor", "editor")
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        sending = asyncio.create_task(_collect(provider.generate_stream([{"role": "user", "content": "设定"}], "test")))
        try:
            await asyncio.wait_for(received.wait(), 5)
            requests_for_kind = {
                "career": ("/careers/batch", {"careers": [{"name": "组合职业", "stages": [{"name": "初阶"}]}]}),
                "organization": ("/organizations/with-members", {"organization": {"name": "组合组织"}}),
                "character": ("/characters/with-relations", {
                    "character": {"name": "组合人物", "always_in_context": True},
                }),
            }
            path, request = requests_for_kind[kind]
            blocked = await asyncio.wait_for(editor.post(base + path, json=request), 2)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            assert (await editor.get(f"{base}/characters")).json() == []
            headers.set()
            body.set()
            assert await asyncio.wait_for(sending, 5) == ["ok"]
            saved = await editor.post(base + path, json=request)
            assert saved.status_code == 201, saved.text
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


async def _rows(app):
    from app.models import CareerHistory, OrganizationMember, RelationshipHistory, SettingProgression, SettingTimeline

    models = (Career, Character, CharacterCareer, Organization, OrganizationMember, Relationship,
              RelationshipType, CareerHistory, RelationshipHistory, SettingProgression, SettingTimeline)
    async with app.state.sessionmaker() as db:
        return tuple([await db.scalar(select(func.count()).select_from(model)) for model in models])


@pytest.mark.parametrize("bad_reference", [
    "career_stage", "org_parent", "org_member", "org_chapter", "org_scene", "relationship_character",
    "relationship_type", "relationship_chapter", "relationship_scene", "character_target", "character_type",
    "character_career", "character_stage",
])
async def test_foreign_references_and_later_failures_rollback_everything(app, client, make_client, bad_reference):
    owner = await register(client, "bulk-scope-owner")
    editor = make_client()
    await register(editor, "bulk-scope-editor")
    project = await create_project(client)
    foreign = await create_project(client, title="另一部作品")
    base, foreign_base = (f"/api/v1/projects/{p['id']}" for p in (project, foreign))

    async def post(path, body):
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
        return response.json()

    local_a = await post(f"{base}/characters", {"name": "甲"})
    local_b = await post(f"{base}/characters", {"name": "乙"})
    local_c = await post(f"{base}/characters", {"name": "丙"})
    foreign_a = await post(f"{foreign_base}/characters", {"name": "外书角色"})
    foreign_parent = await post(f"{foreign_base}/organizations", {"name": "外书组织"})
    foreign_chapter = await post(f"{foreign_base}/chapters", {"title": "外书章节", "chapter_no": 1})
    scenes = await client.get(f"{foreign_base}/chapters/{foreign_chapter['id']}/scenes")
    assert scenes.status_code == 200, scenes.text
    foreign_scene = scenes.json()["items"][0]["id"]
    local_main = await post(f"{base}/careers", {"name": "主修", "stages": [{"name": "初阶"}]})
    local_sub = await post(f"{base}/careers", {"name": "副修", "kind": "sub", "stages": [{"name": "初阶"}]})
    foreign_career = await post(f"{foreign_base}/careers", {"name": "外书职业", "stages": [{"name": "初阶"}]})
    foreign_type = await post(f"{foreign_base}/relationship-types", {"role_a": "异书甲", "role_b": "异书乙"})
    await _invite(client, editor, project["id"], "bulk-scope-editor", "editor")

    if bad_reference == "career_stage":
        kind, path = "career", "/careers/batch"
        payload = {"careers": [
            {"name": "先写职业", "stages": [{"name": "初阶"}]},
            {"name": "后错职业", "stages": [{"id": foreign_career['stages'][0]['id'], "name": "外阶"}]},
        ]}
    elif bad_reference.startswith("org_"):
        kind, path = "organization", "/organizations/with-members"
        bad = {"character_id": local_b["id"]}
        payload = {"organization": {"name": "不得留存组织"}, "members": [{"character_id": local_a["id"]}, bad]}
        if bad_reference == "org_parent":
            payload["organization"]["parent_id"] = foreign_parent["id"]
        elif bad_reference == "org_member":
            bad["character_id"] = foreign_a["id"]
        elif bad_reference == "org_chapter":
            bad.update(anchor_kind="chapter", chapter_id=foreign_chapter["id"])
        else:
            bad.update(anchor_kind="scene", scene_id=foreign_scene)
    elif bad_reference.startswith("relationship_"):
        kind, path = "relationship", "/relationships/batch"
        bad = {"a_id": local_b["id"], "b_id": local_c["id"], "role_a": "朋友"}
        payload = {"items": [
            {"a_id": local_a["id"], "b_id": local_b["id"], "role_a": "临时自定义关系"}, bad,
        ]}
        if bad_reference == "relationship_character":
            bad["b_id"] = foreign_a["id"]
        elif bad_reference == "relationship_type":
            bad["type_id"] = foreign_type["id"]
        elif bad_reference == "relationship_chapter":
            bad.update(anchor_kind="chapter", chapter_id=foreign_chapter["id"])
        else:
            bad.update(anchor_kind="scene", scene_id=foreign_scene)
    else:
        kind, path = "character", "/characters/with-relations"
        payload = {
            "character": {"name": "不得留存人物", "always_in_context": True},
            "relations": [{"target_id": local_a["id"], "role_a": "临时自定义关系"}],
            "careers": [{"career_id": local_main["id"]}],
        }
        if bad_reference == "character_target":
            payload["relations"].append({"target_id": foreign_a["id"], "role_a": "朋友"})
        elif bad_reference == "character_type":
            payload["relations"].append({"target_id": local_b["id"], "type_id": foreign_type["id"]})
        elif bad_reference == "character_career":
            payload["careers"].append({"career_id": foreign_career["id"]})
        else:
            # 第一项已完成 assign_new 内的 SAVEPOINT，后一项非法必须仍撤销整组。
            payload["careers"].append({"career_id": local_sub["id"], "stage_id": foreign_career['stages'][0]['id']})

    before = await _rows(app)
    result = await editor.post(base + path, json=payload)
    assert result.status_code == 400, result.text
    assert await _rows(app) == before
    # 即使内部调用者捕获异常后再次提交，也不能提交任何已经 flush 的前项。
    service, data = _service(kind, payload)
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError):
            await service(db, owner["id"], project["id"], data, manual=True)
        await db.commit()
    assert await _rows(app) == before


async def test_batch_relationship_foreign_pair_has_same_error_as_missing_pair(client, make_client):
    await register(client, "bulk-pair-owner")
    editor = make_client()
    await register(editor, "bulk-pair-editor")
    project = await create_project(client)
    foreign = await create_project(client, title="外书")
    base, other = (f"/api/v1/projects/{p['id']}" for p in (project, foreign))
    ids = []
    for name in ("甲", "乙"):
        response = await client.post(f"{other}/characters", json={"name": name})
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    made = await client.post(f"{other}/relationships", json={"a_id": ids[0], "b_id": ids[1], "role_a": "朋友"})
    assert made.status_code == 201, made.text
    local = []
    for name in ("本书甲", "本书乙"):
        response = await client.post(f"{base}/characters", json={"name": name})
        assert response.status_code == 201, response.text
        local.append(response.json()["id"])
    made = await client.post(
        f"{base}/relationships", json={"a_id": local[0], "b_id": local[1], "role_a": "朋友"},
    )
    assert made.status_code == 201, made.text
    await _invite(client, editor, project["id"], "bulk-pair-editor", "editor")

    def body(a, b):
        return {"items": [
            {"a_id": local[0], "b_id": local[1], "role_a": "朋友"},
            {"a_id": a, "b_id": b, "role_a": "朋友"},
        ]}

    existing = await editor.post(f"{base}/relationships/batch", json=body(*ids))
    missing = await editor.post(f"{base}/relationships/batch", json=body(2147483646, 2147483647))
    assert existing.status_code == missing.status_code == 400
    assert existing.json() == missing.json()


async def test_combined_character_does_not_expose_series_identity(client, make_client):
    await register(client, "bulk-series-owner")
    editor = make_client()
    await register(editor, "bulk-series-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post("/api/v1/series", json={"name": "不属于编辑者的系列"})
    assert created.status_code == 201, created.text
    series = created.json()
    series_base = f"/api/v1/series/{series['id']}"
    attached = await client.post(f"{series_base}/books", json={
        "project_id": project["id"], "expected_revision": series["revision"],
    })
    assert attached.status_code == 200, attached.text
    series = attached.json()
    entry = await client.post(f"{series_base}/entries", json={
        "kind": "character", "data": {"name": "系列角色"}, "expected_revision": series["revision"],
    })
    assert entry.status_code == 201, entry.text
    entry = entry.json()
    use = await client.post(f"{series_base}/entries/{entry['item']['id']}/uses", json={
        "project_id": project["id"], "expected_revision": entry["revision"],
    })
    assert use.status_code == 200, use.text
    await _invite(client, editor, project["id"], "bulk-series-editor", "editor")
    assert (await editor.get(series_base)).status_code == 404
    created = await editor.post(f"{base}/characters/with-relations", json={
        "character": {"name": "新人物"},
        "relations": [{"target_id": use.json()["ref"]["id"], "role_a": "朋友"}],
    })
    assert created.status_code == 201, created.text
    assert created.json()["relationships"][0]["other_name"] == "系列角色"
    assert "series_id" not in created.text and "series_entry_id" not in created.text
    assert "不属于编辑者的系列" not in created.text


@pytest.mark.parametrize("kind", ["career", "organization", "character"])
@pytest.mark.parametrize("competes", [True, False])
async def test_bulk_candidates_invalidate_visibility_only_on_competition(app, client, make_client, kind, competes):
    await register(client, "bulk-candidate-owner")
    editor = make_client()
    await register(editor, "bulk-candidate-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    existing = await client.post(f"{base}/characters", json={"name": "青鸿", "profile": "旧唯一候选"})
    assert existing.status_code == 201, existing.text
    await _invite(client, editor, project["id"], "bulk-candidate-editor", "editor")
    async with app.state.sessionmaker() as db:
        old = await build_scope(db, project["id"], texts=("青鸿",))
        assert old.includes("character", existing.json()["id"])
    name = "青鸿" if competes else "远行"
    if kind == "career":
        path, payload = "/careers/batch", {"careers": [{"name": name, "stages": [{"name": "初阶"}]}]}
    elif kind == "organization":
        path, payload = "/organizations/with-members", {"organization": {"name": name}}
    else:
        path, payload = "/characters/with-relations", {"character": {"name": "新角色", "aliases": [name]}}
    result = await editor.post(base + path, json=payload)
    assert result.status_code == 201, result.text
    async with app.state.sessionmaker() as db:
        current = await build_scope(db, project["id"], texts=("青鸿",))
        project_row = await db.get(Project, project["id"])
        assert current.includes("character", existing.json()["id"]) is not competes
        assert current.stamp.revision == old.stamp.revision + int(competes)
        assert project_row.ai_history_epoch == old.stamp.epoch + int(competes)
