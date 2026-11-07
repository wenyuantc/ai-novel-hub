"""场景继承、逐项覆盖、真实范围和稳定规划来源。"""

import asyncio
import json

from sqlalchemy import event, func, select

from app.models import Chapter, ChapterCharacter, PlanningUnit
from app.models.scenes import Scene
from app.services.planning_scene_service import resolve_scene_plannings
from tests.helpers import create_project, register


async def scene_setup(client, *, content="第一段", count=2):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = await client.post(f"{base}/chapters", json={"title": "相逢", "chapter_no": 1, "content": content})
    assert chapter.status_code == 201, chapter.text
    chapter = chapter.json()
    scenes_url = f"{base}/chapters/{chapter['id']}/scenes"
    for index in range(1, count):
        page = (await client.get(scenes_url)).json()
        response = await client.post(scenes_url, json={
            "expected_content_revision": page["content_revision"], "title": f"场景{index + 1}", "content": "第二段",
        })
        assert response.status_code == 201, response.text
    scenes = (await client.get(scenes_url)).json()["items"]
    return project, base, chapter, scenes_url, scenes


async def planning_batch(client, base, scenes_url, targets, **fields):
    matrix = (await client.get(f"{base}/planning/matrix")).json()
    body = {"expected_revision": matrix["revision"], "targets": targets, **fields}
    if any(target["kind"] == "scene" for target in targets):
        page = (await client.get(scenes_url)).json()
        body["expected_content_revisions"] = [{
            "chapter_id": page["items"][0]["chapter_id"], "content_revision": page["content_revision"],
        }]
    return await client.patch(f"{base}/planning/units/batch", json=body)


async def test_scene_defaults_override_empty_and_restore_inherit_do_not_change_chapter_cast(client, app):
    _, base, chapter, scenes_url, scenes = await scene_setup(client)
    alice = (await client.post(f"{base}/characters", json={"name": "甲角色"})).json()
    bob = (await client.post(f"{base}/characters", json={"name": "乙角色"})).json()
    chapter_source = [{"kind": "chapter", "id": chapter["id"]}]
    saved = await planning_batch(client, base, scenes_url, chapter_source,
                                 pov={"op": "set", "character_id": alice["id"]},
                                 references=[{"kind": "character", "op": "replace", "ids": [alice["id"]]}])
    assert saved.status_code == 200, saved.text
    page = (await client.get(scenes_url)).json()
    inherited = (await client.get(f"{scenes_url}/{scenes[0]['id']}")).json()["planning"]
    assert inherited["unit_id"] is None
    assert inherited["raw"]["modes"]["character"] == "inherit"
    assert inherited["effective"]["pov_character_id"] == alice["id"]
    source = [{"kind": "scene", "id": scenes[0]["id"]}]
    changed = await planning_batch(client, base, scenes_url, source, pov={"op": "clear"},
                                   references=[{"kind": "character", "op": "replace", "ids": [bob["id"]]}])
    assert changed.status_code == 200, changed.text
    assert changed.json()["content_revisions"][0]["content_revision"] == page["content_revision"] + 1
    first = changed.json()["rows"][0]["scene_planning"]
    assert first["raw"]["modes"]["pov"] == "override" and first["effective"]["pov_character_id"] is None
    assert first["effective"]["references"] == [{"kind": "character", "id": bob["id"]}]
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(ChapterCharacter.character_id).where(
            ChapterCharacter.chapter_id == chapter["id"]
        ))) == [alice["id"]]
    parent = await planning_batch(client, base, scenes_url, chapter_source,
                                  pov={"op": "set", "character_id": bob["id"]})
    assert parent.status_code == 200, parent.text
    local = (await client.get(f"{scenes_url}/{scenes[0]['id']}")).json()["planning"]["effective"]
    sibling = (await client.get(f"{scenes_url}/{scenes[1]['id']}")).json()["planning"]["effective"]
    assert local["pov_character_id"] is None and sibling["pov_character_id"] == bob["id"]
    restored = await planning_batch(client, base, scenes_url, source, pov={"op": "inherit"},
                                    references=[{"kind": "character", "op": "inherit"}])
    assert restored.status_code == 200, restored.text
    view = restored.json()["rows"][0]["scene_planning"]
    assert view["raw"]["pov_character_id"] is None and not view["raw"]["references"]
    assert view["effective"]["pov_character_id"] == bob["id"]
    assert view["effective"]["references"] == [{"kind": "character", "id": alice["id"]}]
    assert set(view["effective"]["sources"].values()) == {"chapter"}


async def test_scene_all_reference_types_clear_and_inherit_independently(client):
    _, base, chapter, scenes_url, scenes = await scene_setup(client)
    alice = (await client.post(f"{base}/characters", json={"name": "甲"})).json()
    bob = (await client.post(f"{base}/characters", json={"name": "乙"})).json()
    worlds = [(await client.post(f"{base}/world", json={"title": f"设定{category}", "category": category})).json()
              for category in ("place", "faction", "item", "rule", "history", "other")]
    organization = (await client.post(f"{base}/organizations", json={"name": "山门"})).json()
    career = (await client.post(f"{base}/careers", json={"name": "游侠", "stages": [{"name": "初阶"}]})).json()
    types = (await client.get(f"{base}/relationship-types")).json()
    relation = await client.post(f"{base}/relationships", json={
        "a_id": alice["id"], "b_id": bob["id"], "type_id": types[0]["id"],
    })
    assert relation.status_code == 201, relation.text
    relationship = (await client.get(f"{base}/planning/entries", params={"kind": "relationship"})).json()["items"][0]
    revision = (await client.get(f"{base}/planning/matrix")).json()["revision"]
    tag = await client.post(f"{base}/planning/tags", json={"expected_revision": revision, "name": "铺垫"})
    assert tag.status_code == 201, tag.text
    operations = [
        {"kind": "character", "op": "replace", "ids": [alice["id"]]},
        {"kind": "world", "op": "replace", "ids": [world["id"] for world in worlds]},
        {"kind": "organization", "op": "replace", "ids": [organization["id"]]},
        {"kind": "career", "op": "replace", "ids": [career["id"]]},
        {"kind": "relationship", "op": "replace", "timeline_ids": [relationship["reference"]["timeline_id"]]},
    ]
    saved = await planning_batch(client, base, scenes_url, [{"kind": "chapter", "id": chapter["id"]}],
                                 references=operations, tags={"op": "replace", "ids": [tag.json()["item"]["id"]]})
    assert saved.status_code == 200, saved.text
    source = [{"kind": "scene", "id": scenes[0]["id"]}]
    for kind in ("character", "world", "organization", "career", "relationship"):
        cleared = await planning_batch(client, base, scenes_url, source, references=[{"kind": kind, "op": "clear"}])
        assert cleared.status_code == 200, cleared.text
        view = cleared.json()["rows"][0]["scene_planning"]
        assert view["raw"]["modes"][kind] == "override"
        assert not [ref for ref in view["effective"]["references"] if ref["kind"] == kind]
        restored = await planning_batch(client, base, scenes_url, source,
                                        references=[{"kind": kind, "op": "inherit"}])
        assert restored.status_code == 200, restored.text
        refs = restored.json()["rows"][0]["references"]
        assert {ref["kind"] for ref in refs} == {"character", "world", "organization", "career", "relationship"}
    cleared = await planning_batch(client, base, scenes_url, source, tags={"op": "clear"})
    assert cleared.status_code == 200 and not cleared.json()["rows"][0]["tags"]
    restored = await planning_batch(client, base, scenes_url, source, tags={"op": "inherit"})
    assert restored.status_code == 200 and restored.json()["rows"][0]["tags"][0]["name"] == "铺垫"


async def test_scene_batch_content_cas_and_same_chapter_multiple_targets_advance_once(client):
    _, base, _, scenes_url, scenes = await scene_setup(client)
    revision = (await client.get(f"{base}/planning/matrix")).json()["revision"]
    page = (await client.get(scenes_url)).json()
    body = {"expected_revision": revision, "targets": [{"kind": "scene", "id": scene["id"]} for scene in scenes],
            "expected_content_revisions": [{"chapter_id": scenes[0]["chapter_id"],
                                            "content_revision": page["content_revision"]}], "pov": {"op": "clear"}}
    responses = await asyncio.gather(*(client.patch(f"{base}/planning/units/batch", json=body) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert (await client.get(scenes_url)).json()["content_revision"] == page["content_revision"] + 1
    fresh = (await client.get(f"{base}/planning/matrix")).json()["revision"]
    stale = await client.patch(f"{base}/planning/units/batch", json={**body, "expected_revision": fresh})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision"


async def test_scene_matrix_mentions_and_weave_are_separate_axis(client):
    _, base, chapter, scenes_url, scenes = await scene_setup(client, content="林远林远", count=4)
    character = (await client.post(f"{base}/characters", json={"name": "林远"})).json()
    target = [{"kind": "scene", "id": scenes[0]["id"]}]
    changed = await planning_batch(client, base, scenes_url, target,
                                   references=[{"kind": "character", "op": "replace", "ids": [character["id"]]}])
    assert changed.status_code == 200, changed.text
    columns = [{"kind": "setting", "reference": {"kind": "character", "id": character["id"]}}]
    matrix = await client.get(f"{base}/planning/matrix", params={"granularity": "scene",
                                                             "column_refs": json.dumps(columns)})
    assert matrix.status_code == 200, matrix.text
    assert matrix.json()["total"] == 4
    counts = {cell["row_key"]: cell for cell in matrix.json()["cells"]}
    assert counts[f"scene:{scenes[0]['id']}"]["mention_count"] == 2
    assert counts[f"scene:{scenes[1]['id']}"]["mention_count"] == 0
    assert counts[f"scene:{scenes[0]['id']}"]["manual_source"] == "scene"
    subplot = await client.post(f"{base}/planning/subplots", json={
        "expected_revision": matrix.json()["revision"], "name": "寻亲",
    })
    assert subplot.status_code == 201, subplot.text
    revision = subplot.json()["revision"]
    for scene in (scenes[0], scenes[3]):
        node = await client.put(f"{base}/planning/subplots/{subplot.json()['item']['id']}/nodes/scene/{scene['id']}",
                                json={"expected_revision": revision, "content": "推进"})
        assert node.status_code == 200, node.text
        revision = node.json()["revision"]
    other_subplot = await client.post(f"{base}/planning/subplots", json={"expected_revision": revision, "name": "破局"})
    assert other_subplot.status_code == 201, other_subplot.text
    crossing = await client.post(f"{base}/planning/crossings", json={
        "expected_revision": other_subplot.json()["revision"], "source": {"kind": "scene", "id": scenes[3]["id"]},
        "title": "场景交汇", "description": "共同推进", "subplot_ids": [
            subplot.json()["item"]["id"], other_subplot.json()["item"]["id"],
        ],
    })
    assert crossing.status_code == 201, crossing.text
    crossing_list = await client.get(f"{base}/planning/crossings", params={
        "source_kind": "scene", "source_id": scenes[3]["id"],
    })
    assert crossing_list.status_code == 200 and crossing_list.json()["total"] == 1, crossing_list.text
    weave = await client.get(f"{base}/planning/weave", params={"axis": "scene", "page_size": 1, "gap_threshold": 2})
    assert weave.status_code == 200, weave.text
    assert weave.json()["total"] == 4 and len(weave.json()["rows"]) == 1
    assert weave.json()["tracks"][0]["gaps"][0]["gap_units"] == 2
    assert "场景 4" in weave.json()["tracks"][0]["gaps"][0]["before_label"]
    chapters = (await client.get(f"{base}/planning/weave")).json()
    assert chapters["total"] == 1 and all(track["unplanned"] for track in chapters["tracks"])
    assert chapters["rows"][0]["chapter_id"] == chapter["id"]


async def test_scene_planning_read_does_not_materialize_flush_or_select_body(client, app):
    project, _, chapter, _, scenes = await scene_setup(client)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    async with app.state.sessionmaker() as db:
        obj = await db.get(Chapter, chapter["id"])
        obj.title = "未提交草稿"

        def reject_flush(_session, _flush_context, _instances):
            raise AssertionError("只读场景规划触发了 flush")

        event.listen(db.sync_session, "before_flush", reject_flush)
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
        try:
            result = await resolve_scene_plannings(db, project["id"], [scene["id"] for scene in scenes])
            assert all(planning.unit_id is None for planning in result.values())
        finally:
            event.remove(db.sync_session, "before_flush", reject_flush)
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
        assert await db.scalar(select(func.count()).select_from(PlanningUnit)) == 0
        await db.rollback()
    assert not any("scenes.content" in statement or "chapters.content," in statement for statement in statements)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Scene)) == 2


async def test_scene_local_and_recoverable_references_block_true_setting_delete(client):
    _, base, _, scenes_url, scenes = await scene_setup(client)
    character = (await client.post(f"{base}/characters", json={"name": "保留角色"})).json()
    target = [{"kind": "scene", "id": scenes[0]["id"]}]
    changed = await planning_batch(client, base, scenes_url, target, pov={"op": "set", "character_id": character["id"]})
    assert changed.status_code == 200, changed.text
    denied = await client.delete(f"{base}/characters/{character['id']}")
    assert denied.status_code == 409 and denied.json()["code"] == "setting_target_in_use"
    cleared = await planning_batch(client, base, scenes_url, target, pov={"op": "inherit"})
    assert cleared.status_code == 200, cleared.text
    denied_history = await client.delete(f"{base}/characters/{character['id']}")
    assert denied_history.status_code == 409 and denied_history.json()["code"] == "setting_target_in_use"
    assert any(source["kind"] == "scene_history" for source in denied_history.json()["sources"])
