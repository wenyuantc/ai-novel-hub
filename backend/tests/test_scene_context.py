"""统一有效规划到实际上下文的边界；不会将被覆盖的章 cast 或未来状态混回。"""

from app.models import Chapter, Project
from app.services import context_builder, planning_context_service
from tests.test_scene_planning import planning_batch, scene_setup
from tests.test_scene_progressions import _scene_change, _timeline
from tests.test_setting_categories import _create, _dropdown
from tests.test_setting_fields import _fields, _versions


async def _policy(client, base, kind, identity, **changes):
    url = f"{base}/setting-entries/{kind}/{identity}/ai-settings"
    current = (await client.get(url)).json()
    response = await client.patch(url, json={
        "expected_ai_visibility_revision": current["ai_visibility_revision"], **changes,
    })
    assert response.status_code == 200, response.text


async def test_scene_context_manual_override_never_and_hidden_fields_share_one_projection(client, app):
    project, base, chapter, scenes_url, scenes = await scene_setup(client, content="无姓名的场景。")
    old = (await client.post(f"{base}/characters", json={
        "name": "章默认旧人物", "profile": "不可混回的旧角色秘密",
    })).json()
    new = (await client.post(f"{base}/characters", json={"name": "场景新人", "profile": "字段隐藏秘密",
                                                       "current_state": "还是学徒"})).json()
    forbidden = (await client.post(f"{base}/characters", json={
        "name": "绝不可出站视角", "profile": "永不发送资料",
    })).json()
    await _policy(client, base, "character", old["id"], ai_mode="manual_only")
    await _policy(client, base, "character", new["id"], ai_mode="manual_only", ai_hidden_builtin_fields=["profile"])
    await _policy(client, base, "character", forbidden["id"], ai_mode="never")
    parent = await planning_batch(client, base, scenes_url, [{"kind": "chapter", "id": chapter["id"]}],
                                  pov={"op": "set", "character_id": old["id"]},
                                  references=[{"kind": "character", "op": "replace", "ids": [old["id"]]}])
    assert parent.status_code == 200, parent.text
    child = await planning_batch(client, base, scenes_url, [{"kind": "scene", "id": scenes[0]["id"]}],
                                 pov={"op": "set", "character_id": forbidden["id"]},
                                 references=[{"kind": "character", "op": "replace", "ids": [new["id"]]}])
    assert child.status_code == 200, child.text
    timeline = await _timeline(client, base, "character", "character_id", new["id"])
    changed = await _scene_change(client, f"{base}/setting-timelines/{timeline['id']}", timeline["revision"],
                                  scenes[1]["id"], field="current_state", value="未来圣者")
    assert changed.status_code == 201, changed.text
    async with app.state.sessionmaker() as db:
        stored_project = await db.get(Project, project["id"])
        stored_chapter = await db.get(Chapter, chapter["id"])
        planning = await planning_context_service.load_planning_context(
            db, stored_project, "scene", scenes[0]["id"],
        )
        assert planning.manual_character_ids == [new["id"]]
        assert planning.setting_scope.scope.includes("character", new["id"])
        assert not planning.setting_scope.scope.includes("character", old["id"])
        assert not planning.setting_scope.scope.includes("character", forbidden["id"])
        ordinary = await context_builder.build_setting_context(db, stored_project, stored_chapter, planning=planning)
        combined = planning.text + ordinary
        assert "场景新人" in combined and "还是学徒" in combined
        assert all(secret not in combined for secret in (
            "章默认旧人物", "不可混回的旧角色秘密", "绝不可出站视角", "永不发送资料", "字段隐藏秘密", "未来圣者",
        ))


async def test_scene_field_and_category_queries_project_at_exact_scene_not_chapter_window(client):
    _, base, _, _, scenes = await scene_setup(client, count=3)
    character = (await client.post(f"{base}/characters", json={"name": "林岚"})).json()
    definition_id, option_id = await _dropdown(client, base)
    fields = await _fields(client, base, character)
    changed = await client.post(
        f"{base}/setting-entries/character/{character['id']}/fields/{definition_id}/progressions",
        json={**_versions(fields), "field_type": "dropdown", "value": {"option_id": option_id},
              "operation": "replace", "anchor_kind": "scene", "scene_id": scenes[1]["id"], "boundary": "end"},
    )
    assert changed.status_code == 201, changed.text
    for scene, expected in ((scenes[0], None), (scenes[1], None), (scenes[2], {"option_id": option_id})):
        projected = await _fields(client, base, character, scene_id=scene["id"])
        assert projected["position"]["scene_id"] == scene["id"]
        assert projected["fields"][0]["value"] == expected
    category = await _create(client, base, rules=[{
        "kind": "dropdown", "definition_id": definition_id, "option_id": option_id,
    }])
    url = f"{base}/setting-categories/{category['item']['id']}/entries"
    early = await client.get(url, params={"scene_id": scenes[0]["id"]})
    later = await client.get(url, params={"scene_id": scenes[2]["id"]})
    assert early.status_code == later.status_code == 200, (early.text, later.text)
    assert early.json()["total"] == 0 and later.json()["total"] == 1
    assert later.json()["position"]["scene_id"] == scenes[2]["id"]
    invalid = await client.get(url, params={"scene_id": scenes[0]["id"], "chapter_no": 1})
    assert invalid.status_code == 422
