"""真实场景边界、旧同号章合同及结构操作的完整历史重验。"""

from sqlalchemy import select

from app.models import Chapter, CharacterCareer, SettingTimeline
from app.models.scenes import Scene
from app.schemas.progression import StoryPosition
from app.services.setting_progression_resolver import ProgressionEvent, resolve_progressions
from tests.helpers import create_project
from tests.test_scene_planning import planning_batch, scene_setup


def test_scene_resolver_inserts_positions_between_unchanged_legacy_boundaries():
    events = [
        ProgressionEvent(id=1, field_key="content", operation="append", value="旧章初", anchor_kind="planned",
                         chapter_no=3),
        ProgressionEvent(id=2, field_key="content", operation="append", value="第一场末", anchor_kind="scene",
                         scene_id=20, resolved_chapter_id=7, resolved_chapter_no=3, resolved_scene_order=0,
                         boundary="end"),
        ProgressionEvent(id=3, field_key="content", operation="append", value="第二场初", anchor_kind="scene",
                         scene_id=21, resolved_chapter_id=7, resolved_chapter_no=3, resolved_scene_order=1),
        ProgressionEvent(id=4, field_key="content", operation="append", value="旧章末", anchor_kind="chapter",
                         chapter_id=99, resolved_chapter_no=3, boundary="end"),
        ProgressionEvent(id=5, field_key="content", operation="replace", value="不可复活", anchor_kind="scene",
                         scene_id=None),
    ]

    def at(**position):
        return resolve_progressions("world", {"content": "基础"}, {}, events,
                                    StoryPosition(chapter_no=3, **position))

    assert at(chapter_id=99).fields["content"] == "基础\n旧章初"
    first = at(chapter_id=7, scene_id=20, scene_order=0)
    assert first.fields["content"] == "基础\n旧章初" and first.orphaned_event_ids == (5,)
    assert at(chapter_id=7, scene_id=20, scene_order=0, boundary="end").fields["content"] == "基础\n旧章初\n第一场末"
    assert at(chapter_id=7, scene_id=21, scene_order=1).fields["content"] == "基础\n旧章初\n第一场末\n第二场初"
    assert at(chapter_id=99, boundary="end").fields["content"] == "基础\n旧章初\n第一场末\n第二场初\n旧章末"


async def _timeline(client, base, kind, field, identity):
    response = await client.get(f"{base}/setting-timelines", params={"subject_kind": kind, field: identity})
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    return response.json()["items"][0]


async def _scene_change(client, url, revision, scene_id, *, field="content", value="变化", boundary="end"):
    return await client.post(f"{url}/progressions", json={
        "expected_revision": revision, "field_key": field, "operation": "replace", "value": value,
        "anchor_kind": "scene", "scene_id": scene_id, "boundary": boundary,
    })


async def test_scene_http_boundaries_effective_and_previews_use_same_position(client):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, count=3)
    world = (await client.post(f"{base}/world", json={"title": "山门", "category": "place", "content": "完好"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    url = f"{base}/setting-timelines/{timeline['id']}"
    changed = await _scene_change(client, url, timeline["revision"], scenes[1]["id"], value="山门被毁")
    assert changed.status_code == 201, changed.text
    assert changed.json()["item"]["scene_id"] == scenes[1]["id"]
    assert changed.json()["item"]["resolved_scene_order"] == 1
    assert not changed.json()["item"]["orphaned"]
    for scene, expected in ((scenes[0], "完好"), (scenes[1], "完好"), (scenes[2], "山门被毁")):
        response = await client.get(f"{url}/effective", params={"scene_id": scene["id"]})
        assert response.status_code == 200, response.text
        assert response.json()["fields"]["content"] == expected
        assert response.json()["position"]["scene_id"] == scene["id"]
    old_start = await client.get(f"{url}/effective", params={"chapter_id": chapter["id"]})
    old_end = await client.get(f"{url}/effective", params={"chapter_id": chapter["id"], "boundary": "end"})
    assert old_start.json()["fields"]["content"] == "完好"
    assert old_end.json()["fields"]["content"] == "山门被毁"
    selected = await planning_batch(client, base, scenes_url, [{"kind": "scene", "id": scenes[0]["id"]}],
                                    references=[{"kind": "world", "op": "replace", "ids": [world["id"]]}])
    assert selected.status_code == 200, selected.text
    preview = await client.get(f"{base}/planning/units/context-preview",
                               params={"source_kind": "scene", "source_id": scenes[0]["id"]})
    assert preview.status_code == 200, preview.text
    assert "完好" in preview.json()["text"] and "山门被毁" not in preview.json()["text"]
    setting = await client.get(f"{scenes_url}/{scenes[0]['id']}/setting-context")
    assert setting.status_code == 200, setting.text
    assert setting.json()["position"]["scene_id"] == scenes[0]["id"]
    assert "完好" in setting.json()["text"] and "山门被毁" not in setting.json()["text"]


async def test_scene_reorder_changes_effective_values_but_keeps_same_number_legacy_window(client, app):
    _, base, chapter, scenes_url, scenes = await scene_setup(client)
    other = (await client.post(f"{base}/chapters", json={"title": "同号章", "chapter_no": 2})).json()
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, other["id"])
        row.chapter_no = 1
        await db.commit()
    world = (await client.post(f"{base}/world", json={"title": "状态", "content": "原状"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    url = f"{base}/setting-timelines/{timeline['id']}"
    changed = await _scene_change(client, url, timeline["revision"], scenes[0]["id"], value="已变化")
    assert changed.status_code == 201, changed.text
    same_start = (await client.get(f"{url}/effective", params={"chapter_id": other["id"]})).json()
    assert same_start["fields"]["content"] == "原状"
    other_scene = (await client.get(f"{base}/chapters/{other['id']}/scenes")).json()["items"][0]
    exact = (await client.get(f"{url}/effective", params={"scene_id": other_scene["id"]})).json()
    assert exact["fields"]["content"] == "已变化"
    before = (await client.get(f"{url}/effective", params={"scene_id": scenes[1]["id"]})).json()
    assert before["fields"]["content"] == "已变化"
    page = (await client.get(scenes_url)).json()
    reorder = await client.post(f"{scenes_url}/reorder", json={
        "expected_content_revision": page["content_revision"], "scene_ids": [scene["id"] for scene in reversed(scenes)],
    })
    assert reorder.status_code == 200, reorder.text
    after = (await client.get(f"{url}/effective", params={"scene_id": scenes[1]["id"]})).json()
    assert after["fields"]["content"] == "原状"
    legacy = (await client.get(f"{url}/effective", params={"chapter_id": chapter["id"]})).json()
    assert legacy["fields"]["content"] == "原状"


async def test_scene_reorder_revalidates_intermediate_career_limit_and_rolls_back_everything(client, app):
    project, base, chapter, scenes_url, scenes = await scene_setup(client)
    character = (await client.post(f"{base}/characters", json={"name": "主角"})).json()
    careers = [(await client.post(f"{base}/careers", json={
        "name": name, "kind": "main", "stages": [{"name": "初阶"}],
    })).json() for name in ("旧职业", "新职业")]
    timeline_ids = []
    async with app.state.sessionmaker() as db:
        for career in careers:
            timeline = SettingTimeline(project_id=project["id"], subject_kind="career", character_id=character["id"],
                                       career_id=career["id"], baseline={"exists": False})
            db.add(timeline)
            await db.flush()
            timeline_ids.append(timeline.id)
        await db.commit()
    first_url, second_url = [f"{base}/setting-timelines/{identity}" for identity in timeline_ids]

    def presence(career):
        return {"exists": True, "state": {"role": "main", "stage_id": career["stages"][0]["id"], "note": ""}}

    entered = await _scene_change(client, first_url, 1, scenes[0]["id"], field="presence",
                                  value=presence(careers[0]), boundary="start")
    assert entered.status_code == 201, entered.text
    left = await _scene_change(client, first_url, entered.json()["timeline_revision"], scenes[0]["id"],
                               field="presence", value={"exists": False})
    assert left.status_code == 201, left.text
    entered_new = await _scene_change(client, second_url, 1, scenes[1]["id"], field="presence",
                                      value=presence(careers[1]), boundary="start")
    assert entered_new.status_code == 201, entered_new.text
    page = (await client.get(scenes_url)).json()
    denied = await client.post(f"{scenes_url}/reorder", json={
        "expected_content_revision": page["content_revision"], "scene_ids": [scene["id"] for scene in reversed(scenes)],
    })
    assert denied.status_code == 409, denied.text
    fresh = (await client.get(scenes_url)).json()
    assert fresh["content_revision"] == page["content_revision"]
    assert [scene["id"] for scene in fresh["items"]] == [scene["id"] for scene in scenes]
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(Scene.id).where(Scene.chapter_id == chapter["id"])
                                     .order_by(Scene.sort))) == [scene["id"] for scene in scenes]
        current = list(await db.scalars(select(CharacterCareer).where(CharacterCareer.character_id == character["id"])))
        assert len(current) == 1 and current[0].career_id == careers[1]["id"]


async def test_scene_progressions_reject_cross_project_mixed_identity_and_protect_inactive_events(client):
    _, base, _, scenes_url, scenes = await scene_setup(client)
    world = (await client.post(f"{base}/world", json={"title": "山门", "content": "完好"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    url = f"{base}/setting-timelines/{timeline['id']}"
    other = await create_project(client, title="其他作品")
    foreign_base = f"/api/v1/projects/{other['id']}"
    foreign_chapter = (await client.post(f"{foreign_base}/chapters", json={"chapter_no": 1})).json()
    foreign_scene = (await client.get(f"{foreign_base}/chapters/{foreign_chapter['id']}/scenes")).json()["items"][0]
    foreign = await _scene_change(client, url, timeline["revision"], foreign_scene["id"])
    assert foreign.status_code == 400 and "不属于" in foreign.json()["detail"]
    mixed = await client.post(f"{url}/progressions", json={
        "expected_revision": 1, "field_key": "content", "operation": "replace", "value": "错误",
        "anchor_kind": "scene", "scene_id": scenes[0]["id"], "chapter_id": scenes[0]["chapter_id"],
    })
    assert mixed.status_code == 422
    changed = await _scene_change(client, url, timeline["revision"], scenes[0]["id"])
    assert changed.status_code == 201, changed.text
    removed = await client.delete(f"{url}/progressions/{changed.json()['item']['id']}",
                                   params={"expected_revision": changed.json()["timeline_revision"]})
    assert removed.status_code == 200, removed.text
    page = (await client.get(scenes_url)).json()
    blocked = await client.delete(f"{scenes_url}/{scenes[0]['id']}",
                                  params={"expected_content_revision": page["content_revision"]})
    assert blocked.status_code == 409 and blocked.json()["code"] == "scene_reference_conflict"


async def test_deleted_chapter_orphans_scene_event_without_same_number_resurrection(client):
    _, base, chapter, _, scenes = await scene_setup(client)
    world = (await client.post(f"{base}/world", json={"title": "旧锚", "content": "起点"})).json()
    timeline = await _timeline(client, base, "world", "world_setting_id", world["id"])
    url = f"{base}/setting-timelines/{timeline['id']}"
    changed = await _scene_change(client, url, timeline["revision"], scenes[0]["id"], value="已变")
    assert changed.status_code == 201, changed.text
    deleted = await client.delete(f"{base}/chapters/{chapter['id']}")
    assert deleted.status_code == 204, deleted.text
    replacement = await client.post(f"{base}/chapters", json={"chapter_no": 1, "title": "同号新章"})
    assert replacement.status_code == 201, replacement.text
    new_scene = (await client.get(f"{base}/chapters/{replacement.json()['id']}/scenes")).json()["items"][0]
    assert new_scene["id"] not in {scene["id"] for scene in scenes}
    detail = (await client.get(f"{url}/progressions/{changed.json()['item']['id']}")).json()["item"]
    assert detail["scene_id"] is None and detail["orphaned"]
    effective = await client.get(f"{url}/effective", params={"scene_id": new_scene["id"]})
    assert effective.status_code == 200 and effective.json()["fields"]["content"] == "起点"


async def test_legacy_relationship_member_and_career_entries_accept_explicit_scene_anchors(client):
    _, base, _, _, scenes = await scene_setup(client)
    alice = (await client.post(f"{base}/characters", json={"name": "甲角色"})).json()
    bob = (await client.post(f"{base}/characters", json={"name": "乙角色"})).json()
    organization = (await client.post(f"{base}/organizations", json={"name": "山门"})).json()
    career = (await client.post(f"{base}/careers", json={"name": "游侠", "stages": [{"name": "初阶"}]})).json()
    types = (await client.get(f"{base}/relationship-types")).json()
    position = {"anchor_kind": "scene", "scene_id": scenes[0]["id"], "boundary": "end"}
    relation = await client.post(f"{base}/relationships", json={
        "a_id": alice["id"], "b_id": bob["id"], "type_id": types[0]["id"], **position,
    })
    assert relation.status_code == 201, relation.text
    member = await client.post(f"{base}/organizations/{organization['id']}/members", json={
        "character_id": alice["id"], "expected_revision": 0, **position,
    })
    assert member.status_code == 201, member.text
    assignment = await client.post(f"{base}/characters/{alice['id']}/careers", json={
        "career_id": career["id"], "expected_revision": 0, **position,
    })
    assert assignment.status_code == 201, assignment.text
    for kind, extra in (("relationship", {}), ("membership", {"organization_id": organization["id"]}),
                        ("career", {"career_id": career["id"]})):
        timeline = await client.get(f"{base}/setting-timelines", params={
            "subject_kind": kind, "character_id": alice["id"], **extra,
        })
        assert timeline.status_code == 200 and timeline.json()["total"] == 1, timeline.text
        url = f"{base}/setting-timelines/{timeline.json()['items'][0]['id']}/effective"
        early = await client.get(url, params={"scene_id": scenes[0]["id"]})
        later = await client.get(url, params={"scene_id": scenes[1]["id"]})
        assert early.status_code == later.status_code == 200, (early.text, later.text)
        assert early.json()["fields"]["exists"] is False and later.json()["fields"]["exists"] is True
