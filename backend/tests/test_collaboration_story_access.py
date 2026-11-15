"""作品协作者的人工设定能力及系列身份边界。"""

import pytest
from sqlalchemy import select

from app.core.errors import NotFoundError
from app.models import Character, Project, User
from app.schemas import setting_categories, setting_fields
from app.schemas.progression import ProgressionCreate
from app.services import (
    career_service,
    foreshadow_service,
    organization_service,
    relationship_service,
    setting_catalog_service,
    setting_category_service,
    setting_field_service,
    setting_progression_service,
    story_service,
)
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def _invite(owner, guest, project_id, username, role):
    response = await owner.post(
        f"/api/v1/projects/{project_id}/collaborators", json={"username": username, "role": role}
    )
    assert response.status_code == 201, response.text
    invitation = response.json()
    response = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert response.status_code == 200, response.text


async def test_story_manual_access_and_strict_agent_helper(app, client, make_client):
    await register(client, "story-owner")
    viewer, editor, outsider = make_client(), make_client(), make_client()
    viewer_user = await register(viewer, "story-viewer")
    await register(editor, "story-editor")
    await register(outsider, "story-outsider")
    project = await create_project(client)
    foreign = await create_project(client, title="另一部作品")
    base = f"/api/v1/projects/{project['id']}"
    foreign_base = f"/api/v1/projects/{foreign['id']}"
    character = await client.post(f"{base}/characters", json={"name": "陆青"})
    assert character.status_code == 201, character.text
    cid = character.json()["id"]
    other = await client.post(f"{foreign_base}/characters", json={"name": "不能跨作品读取"})
    assert other.status_code == 201, other.text
    await _invite(client, viewer, project["id"], "story-viewer", "viewer")
    await _invite(client, editor, project["id"], "story-editor", "editor")

    read = await viewer.get(f"{base}/characters")
    assert read.status_code == 200 and [c["name"] for c in read.json()] == ["陆青"]
    assert (await viewer.get(f"{base}/characters/{cid}")).status_code == 200
    assert (await outsider.get(f"{base}/characters/{cid}")).status_code == 404
    assert (await viewer.get(f"{base}/characters/{other.json()['id']}")).status_code == 404
    assert (await viewer.patch(f"{base}/characters/{cid}", json={"name": "不能写"})).status_code == 403
    assert (await viewer.patch(f"{base}/characters/{cid}", json={"always_in_context": True})).status_code == 403
    assert (await viewer.delete(f"{base}/characters/{cid}")).status_code == 403
    assert (await editor.patch(f"{base}/characters/{cid}", json={"name": "编辑后的角色"})).status_code == 200
    created = await editor.post(f"{base}/world", json={"title": "世界", "content": "设定"})
    assert created.status_code == 201, created.text
    assert (await viewer.get(f"{base}/world/{created.json()['id']}")).status_code == 200

    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await story_service.get_owned(db, story_service.CHARACTERS, viewer_user["id"], project["id"], cid)
        assert await db.scalar(select(Character.name).where(Character.id == cid)) == "编辑后的角色"
        legacy = await db.get(Character, cid)
        legacy.ai_mode = None
        before_revision = (await db.get(Project, project["id"])).ai_visibility_revision
        await db.commit()
    assert (await editor.patch(f"{base}/characters/{cid}", json={"always_in_context": True})).status_code == 200
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).ai_visibility_revision == before_revision + 1


async def test_single_book_collaborator_does_not_see_series_identity(client, make_client):
    await register(client, "series-story-owner")
    guest = make_client()
    guest_user = await register(guest, "series-story-guest")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post("/api/v1/series", json={"name": "隐藏系列"})
    series = response.json()
    series_url = f"/api/v1/series/{series['id']}"
    response = await client.post(f"{series_url}/books", json={
        "project_id": project["id"], "expected_revision": series["revision"],
    })
    series = response.json()
    response = await client.post(f"{series_url}/entries", json={
        "kind": "character", "data": {"name": "共享角色"}, "expected_revision": series["revision"],
    })
    entry = response.json()
    response = await client.post(f"{series_url}/entries/{entry['item']['id']}/uses", json={
        "project_id": project["id"], "expected_revision": entry["revision"],
    })
    assert response.status_code == 200, response.text
    local_id = response.json()["ref"]["id"]
    await _invite(client, guest, project["id"], "series-story-guest", "editor")

    response = await guest.get(f"{base}/characters/{local_id}")
    assert response.status_code == 200, response.text
    assert response.json()["origin"] == {"scope": "book", "series_id": None, "series_entry_id": None}
    assert (await guest.get(series_url)).status_code == 404
    timeline_response = await client.get(f"{base}/setting-timelines", params={"character_id": local_id})
    timeline = timeline_response.json()["items"][0]
    denied = await guest.patch(f"{base}/setting-timelines/{timeline['id']}/baseline", json={
        "expected_revision": timeline["revision"], "changes": {"profile": "不应覆盖"},
    })
    assert denied.status_code == 409, denied.text
    assert "系列" not in denied.text and "series_setting_managed" not in denied.text
    for method, body in (("PATCH", {"name": "不应覆盖"}), ("PATCH", {"profile": "不应覆盖"}), ("DELETE", None)):
        denied = await guest.request(method, f"{base}/characters/{local_id}", json=body)
        assert denied.status_code == 409
        assert "系列" not in denied.text and "series_setting_managed" not in denied.text
        assert str(series["id"]) not in denied.text

    invite = await client.post(f"{series_url}/collaborators", json={"username": "series-story-guest"})
    assert invite.status_code == 201, invite.text
    invitation = invite.json()
    accept = await guest.post(
        f"/api/v1/collaboration/invitations/series/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accept.status_code == 200, accept.text
    response = await guest.get(f"{base}/characters/{local_id}")
    assert response.status_code == 200, response.text
    assert response.json()["origin"] == {
        "scope": "series", "series_id": series["id"], "series_entry_id": entry["item"]["id"],
    }
    denied = await guest.patch(f"{base}/characters/{local_id}", json={"name": "仍不可直接修改"})
    assert denied.status_code == 409 and denied.json()["code"] == "series_setting_managed"

    revoked = await client.delete(
        f"{base}/collaborators/{guest_user['id']}", params={"expected_revision": 2},
    )
    assert revoked.status_code == 204, revoked.text
    # 系列身份保留；失去单书权限后不能读取本书或在系列详情中看到它。
    series_detail = await guest.get(series_url)
    assert series_detail.status_code == 200 and series_detail.json()["books"] == []
    series_entry = await guest.get(f"{series_url}/entries/{entry['item']['id']}")
    assert series_entry.status_code == 200
    assert series_entry.json()["uses"] == [] and series_entry.json()["has_uses"] is True
    assert (await guest.get(f"{base}/characters/{local_id}")).status_code == 404
    denied = await guest.patch(f"{base}/characters/{local_id}", json={"name": "系列身份不能修改单书"})
    assert denied.status_code == 404


async def test_timeline_manual_access_and_strict_agent_service(app, client, make_client):
    await register(client, "timeline-owner")
    viewer, editor, outsider = make_client(), make_client(), make_client()
    viewer_user = await register(viewer, "timeline-viewer")
    editor_user = await register(editor, "timeline-editor")
    await register(outsider, "timeline-outsider")
    project = await create_project(client)
    foreign = await create_project(client, title="其他作品")
    base = f"/api/v1/projects/{project['id']}"
    foreign_base = f"/api/v1/projects/{foreign['id']}"
    world = (await client.post(f"{base}/world", json={"title": "星河", "content": "初始"})).json()
    other = (await client.post(f"{foreign_base}/world", json={"title": "山海"})).json()
    timeline = (await client.get(
        f"{base}/setting-timelines", params={"world_setting_id": world['id']},
    )).json()["items"][0]
    foreign_timeline = (await client.get(
        f"{foreign_base}/setting-timelines", params={"world_setting_id": other['id']},
    )).json()["items"][0]
    await _invite(client, viewer, project['id'], "timeline-viewer", "viewer")
    await _invite(client, editor, project['id'], "timeline-editor", "editor")
    url = f"{base}/setting-timelines/{timeline['id']}"
    assert (await viewer.get(f"{base}/setting-timelines")).status_code == 200
    assert (await viewer.get(f"{url}/baseline")).status_code == 200
    assert (await outsider.get(f"{url}/baseline")).status_code == 404
    assert (await viewer.get(f"{base}/setting-timelines/{foreign_timeline['id']}/baseline")).status_code == 404
    body = {"expected_revision": timeline['revision'], "changes": {"content": "新的基础"}}
    assert (await viewer.patch(f"{url}/baseline", json=body)).status_code == 403
    saved = await editor.patch(f"{url}/baseline", json=body)
    assert saved.status_code == 200, saved.text
    event = {"expected_revision": saved.json()['timeline_revision'], "field_key": "content", "operation": "replace",
             "value": "新的剧情", "anchor_kind": "planned", "chapter_no": 3, "boundary": "start"}
    assert (await viewer.post(f"{url}/progressions", json=event)).status_code == 403
    created = await editor.post(f"{url}/progressions", json=event)
    assert created.status_code == 201, created.text
    assert (await viewer.get(f"{url}/progressions")).json()["total"] == 1

    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await setting_progression_service.list_timelines(db, viewer_user['id'], project['id'])
        with pytest.raises(NotFoundError):
            await setting_progression_service.create_progression(
                db, editor_user['id'], project['id'], timeline['id'],
                ProgressionCreate(**event),
            )


async def test_setting_catalog_and_categories_use_explicit_human_permissions(app, client, make_client):
    await register(client, "settings-owner")
    viewer, editor = make_client(), make_client()
    viewer_user = await register(viewer, "settings-viewer")
    editor_user = await register(editor, "settings-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post(f"{base}/characters", json={"name": "主角"})
    assert created.status_code == 201
    await _invite(client, viewer, project["id"], "settings-viewer", "viewer")
    await _invite(client, editor, project["id"], "settings-editor", "editor")

    assert (await viewer.get(f"{base}/setting-entries")).status_code == 200
    assert (await viewer.get(f"{base}/setting-field-definitions")).status_code == 200
    assert (await viewer.get(f"{base}/setting-categories")).status_code == 200
    definition = {"expected_fields_revision": 0, "label": "境界", "field_type": "line",
                  "applicable_types": ["character"]}
    assert (await viewer.post(f"{base}/setting-field-definitions", json=definition)).status_code == 403
    created_field = await editor.post(f"{base}/setting-field-definitions", json=definition)
    assert created_field.status_code == 201, created_field.text
    category = {"expected_categories_revision": 0, "expected_fields_revision": 1, "name": "同盟",
                "applicable_types": ["character"], "match_mode": "all", "rules": [{"kind": "tag", "tag": "友"}]}
    assert (await viewer.post(f"{base}/setting-categories", json=category)).status_code == 403
    made = await editor.post(f"{base}/setting-categories", json=category)
    assert made.status_code == 201, made.text
    assert (await viewer.get(f"{base}/setting-categories/{made.json()['item']['id']}")).status_code == 200

    # 被 Agent 等内部调用的默认服务入口仍使用旧边界。
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await setting_catalog_service.list_entries(db, viewer_user["id"], project["id"])
        with pytest.raises(NotFoundError):
            await setting_field_service.list_definitions(db, viewer_user["id"], project["id"])
        with pytest.raises(NotFoundError):
            await setting_field_service.save_definition(
                db, editor_user["id"], project["id"], setting_fields.DefinitionCreate.model_validate(definition),
            )
        with pytest.raises(NotFoundError):
            await setting_category_service.save_category(
                db, editor_user["id"], project["id"], setting_categories.CategoryCreate.model_validate(category),
            )
        user = await db.get(User, viewer_user["id"])
        actor = setting_category_service.AuthenticatedActor(user.id, user.token_version)
    with pytest.raises(NotFoundError):
        await setting_category_service.list_categories(app.state.sessionmaker, actor, project["id"])


async def test_foreshadow_manual_routes_and_cross_project_ids(app, client, make_client):
    await register(client, "foreshadow-owner")
    viewer, editor, outsider = make_client(), make_client(), make_client()
    viewer_user = await register(viewer, "foreshadow-viewer")
    await register(editor, "foreshadow-editor")
    await register(outsider, "foreshadow-outsider")
    project = await create_project(client)
    foreign = await create_project(client, title="另一部作品")
    base = f"/api/v1/projects/{project['id']}"
    foreign_base = f"/api/v1/projects/{foreign['id']}"
    item = (await client.post(f"{base}/foreshadows", json={"title": "暗线"})).json()
    other = (await client.post(f"{foreign_base}/foreshadows", json={"title": "别书暗线"})).json()
    chapter = (await client.post(f"{base}/chapters", json={"chapter_no": 1, "title": "开场"})).json()
    other_chapter = (await client.post(f"{foreign_base}/chapters", json={"chapter_no": 1, "title": "别章"})).json()
    character = (await client.post(f"{base}/characters", json={"name": "出场者"})).json()
    other_character = (await client.post(f"{foreign_base}/characters", json={"name": "别书角色"})).json()
    await _invite(client, viewer, project['id'], "foreshadow-viewer", "viewer")
    await _invite(client, editor, project['id'], "foreshadow-editor", "editor")
    url = f"{base}/foreshadows/{item['id']}"
    assert (await viewer.get(f"{base}/foreshadows")).status_code == 200
    assert (await viewer.get(f"{base}/foreshadows", params={"character_id": other_character['id']})).status_code == 404
    assert (await viewer.get(f"{url}/history")).status_code == 200
    assert (await viewer.get(f"{base}/chapters/{chapter['id']}/foreshadow-reminders")).status_code == 200
    assert (await outsider.get(url)).status_code == 404
    assert (await viewer.get(f"{base}/foreshadows/{other['id']}")).status_code == 404
    assert (await editor.patch(f"{base}/foreshadows/{other['id']}", json={"title": "跨书"})).status_code == 404
    assert (await viewer.patch(url, json={"title": "禁止写"})).status_code == 403
    saved = await editor.patch(url, json={"title": "编辑后的伏笔"})
    assert saved.status_code == 200, saved.text
    assert (await editor.post(f"{url}/plant", json={"chapter_no": 1})).status_code == 200
    cast_url = f"{base}/chapters/{chapter['id']}/cast"
    assert (await viewer.get(cast_url)).json()["character_ids"] == []
    assert (await viewer.put(cast_url, json={"character_ids": [character['id']]})).status_code == 403
    assert (await editor.put(cast_url, json={"character_ids": [other_character['id']]})).status_code in (400, 404)
    assert (await editor.put(cast_url, json={"character_ids": [character['id']]})).status_code == 200
    assert (await viewer.get(cast_url)).json()["character_ids"] == [character['id']]
    assert (await viewer.get(f"{base}/chapters/{other_chapter['id']}/cast")).status_code == 404
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await foreshadow_service.list_foreshadows(db, viewer_user['id'], project['id'])


async def test_manual_entity_families_enforce_role_and_project_boundaries(app, client, make_client):
    await register(client, "entities-owner")
    viewer, editor, outsider = make_client(), make_client(), make_client()
    viewer_user = await register(viewer, "entities-viewer")
    await register(editor, "entities-editor")
    await register(outsider, "entities-outsider")
    project, foreign = await create_project(client), await create_project(client, title="另一部作品")
    base, foreign_base = (f"/api/v1/projects/{p['id']}" for p in (project, foreign))
    a = (await client.post(f"{base}/characters", json={"name": "甲"})).json()
    b = (await client.post(f"{base}/characters", json={"name": "乙"})).json()
    other = (await client.post(f"{foreign_base}/characters", json={"name": "别书角色"})).json()
    career_body = {"name": "医者", "stages": [{"name": "学徒"}]}
    career = (await client.post(f"{base}/careers", json=career_body)).json()
    foreign_career = (await client.post(f"{foreign_base}/careers", json=career_body)).json()
    org = (await client.post(f"{base}/organizations", json={"name": "组织"})).json()
    foreign_org = (await client.post(f"{foreign_base}/organizations", json={"name": "别组织"})).json()
    await _invite(client, viewer, project['id'], "entities-viewer", "viewer")
    await _invite(client, editor, project['id'], "entities-editor", "editor")
    for path in ("careers", "organizations", "relationships", "relationship-types", "relationship-graph"):
        assert (await viewer.get(f"{base}/{path}")).status_code == 200, path
        assert (await outsider.get(f"{base}/{path}")).status_code == 404, path
    for resource, item, foreign_item in (("careers", career, foreign_career), ("organizations", org, foreign_org)):
        url = f"{base}/{resource}/{item['id']}"
        assert (await viewer.get(url)).status_code == 200
        assert (await viewer.patch(url, json={"name": "禁止写"})).status_code == 403
        saved = await editor.patch(url, json={"name": "编辑后"})
        assert saved.status_code == 200, saved.text
        assert (await editor.patch(f"{base}/{resource}/{foreign_item['id']}", json={"name": "跨书"})).status_code == 404
    assign_url = f"{base}/characters/{a['id']}/careers"
    assert (await viewer.post(assign_url, json={"career_id": career['id']})).status_code == 403
    assert (await editor.post(assign_url, json={"career_id": foreign_career['id']})).status_code in (400, 404)
    assigned = await editor.post(assign_url, json={"career_id": career['id']})
    assert assigned.status_code == 201, assigned.text
    assert (await viewer.get(assign_url)).status_code == 200
    members_url = f"{base}/organizations/{org['id']}/members"
    assert (await editor.post(members_url, json={"character_id": other['id']})).status_code in (400, 404)
    member = await editor.post(members_url, json={"character_id": a['id']})
    assert member.status_code == 201, member.text
    assert (await viewer.get(members_url)).status_code == 200
    relation = {"a_id": a['id'], "b_id": b['id'], "role_a": "师父", "role_b": "徒弟"}
    assert (await viewer.post(f"{base}/relationships", json=relation)).status_code == 403
    created = await editor.post(f"{base}/relationships", json=relation)
    assert created.status_code == 201, created.text
    assert (await viewer.get(f"{base}/relationships/{created.json()['id']}/history")).status_code == 200
    async with app.state.sessionmaker() as db:
        for service in (
            career_service.list_careers, organization_service.list_orgs, relationship_service.list_relationships,
        ):
            with pytest.raises(NotFoundError):
                await service(db, viewer_user['id'], project['id'])


async def test_admin_viewer_union_preserves_anonymous_series_projection(client, make_client, system_admin):
    await register(client, "union-owner")
    guest = make_client()
    user = await register(guest, "union-viewer")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    series_url = f"/api/v1/series/{series['id']}"
    series = (await client.post(f"{series_url}/books", json={
        "project_id": project['id'], "expected_revision": series['revision'],
    })).json()
    entry = (await client.post(f"{series_url}/entries", json={
        "kind": "character", "data": {"name": "共享角色"}, "expected_revision": series['revision'],
    })).json()
    local = (await client.post(f"{series_url}/entries/{entry['item']['id']}/uses", json={
        "project_id": project['id'], "expected_revision": entry['revision'],
    })).json()['ref']['id']
    await _invite(client, guest, project['id'], "union-viewer", "viewer")
    target = f"{base}/characters/{local}"
    assert (await guest.get(target)).json()['origin']['scope'] == "book"
    assert (await guest.post(f"{base}/organizations", json={"name": "不允许"})).status_code == 403
    role = await create_role(system_admin, "作品内容管理员", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, user['id'], [role['id']])
    assert (await guest.post(f"{base}/organizations", json={"name": "管理员可写"})).status_code == 201
    assert (await guest.get(target)).json()['origin'] == {
        "scope": "series", "series_id": None, "series_entry_id": None,
    }
    assert (await guest.get(series_url)).status_code == 404
    denied = await guest.patch(target, json={"name": "不能覆盖"})
    assert denied.status_code == 409 and denied.json()['code'] == "series_setting_managed"
    await assign_roles(system_admin, user['id'], [])
    assert (await guest.get(target)).json()['origin']['scope'] == "book"
    assert (await guest.post(f"{base}/organizations", json={"name": "重新只读"})).status_code == 403
