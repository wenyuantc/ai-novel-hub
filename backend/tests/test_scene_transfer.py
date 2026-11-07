"""v7真实场景图与历史重映射、合法v6转换和非法夹带拒绝。"""

import json
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.models import Chapter, ChapterVersion, PlanningUnit, Scene, SettingProgression, SettingTimeline
from app.services.project_transfer import importer
from tests.test_project_transfer import _backup, _import, _preview_raw, _setup
from tests.test_scenes import setup_scene


async def test_v7_scenes_raw_planning_history_and_retired_ids_roundtrip(app, client):
    project, ids = await _setup(client, app)
    base = f"/api/v1/projects/{project['id']}"
    chapter_id = ids["chapters"][0]
    url = f"{base}/chapters/{chapter_id}"
    first = (await client.get(f"{url}/scenes")).json()["items"][0]
    second = await client.post(f"{url}/scenes", json={
        "expected_content_revision": 0, "content": "第二场景🙂", "summary": "作者保留摘要", "title": "夜访",
    })
    assert second.status_code == 201, second.text
    second = second.json()
    matrix = (await client.get(f"{base}/planning/matrix")).json()
    tag = await client.post(f"{base}/planning/tags", json={"expected_revision": matrix["revision"], "name": "场景标签"})
    assert tag.status_code == 201, tag.text
    planned = await client.patch(f"{base}/planning/units/batch", json={
        "expected_revision": tag.json()["revision"],
        "expected_content_revisions": [{"chapter_id": chapter_id, "content_revision": 1}],
        "targets": [{"kind": "scene", "id": second["id"]}],
        "pov": {"op": "set", "character_id": ids["characters"][0]},
        "tags": {"op": "replace", "ids": [tag.json()["item"]["id"]]},
        "references": [{"kind": "character", "op": "replace", "ids": [ids["characters"][2]]}],
    })
    assert planned.status_code == 200, planned.text
    # 无本地规划的空场景退场，也必须随核心备份保留真实身份。
    third = await client.post(f"{url}/scenes", json={"expected_content_revision": 2})
    assert third.status_code == 201, third.text
    removed = await client.delete(f"{url}/scenes/{third.json()['id']}?expected_content_revision=3")
    assert removed.status_code == 200, removed.text
    # Scene锚点使用原有角色时间线；不用正文历史回滚它。
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(select(SettingTimeline).where(
            SettingTimeline.project_id == project["id"], SettingTimeline.character_id == ids["characters"][0],
            SettingTimeline.subject_kind == "character",
        ))
        db.add(SettingProgression(timeline_id=timeline.id, field_key="current_state", operation="replace",
                                  value="夜访后", anchor_kind="scene", scene_id=second["id"], boundary="end"))
        await db.commit()
    data = await _backup(client, project["id"])
    assert (data["version"], data["app_schema"]) == (7, "0039")
    assert "content_revision" not in data["tables"]["chapters"][0]
    imported = await _import(client, data)
    new_project_id = imported["project"]["id"]
    result = await _backup(client, new_project_id)
    assert len(result["tables"]["scenes"]) == len(data["tables"]["scenes"])
    old_scenes = data["tables"]["scenes"]
    new_scenes = result["tables"]["scenes"]
    mapping = {old["id"]: new["id"] for old, new in zip(old_scenes, new_scenes, strict=True)}
    assert set(mapping).isdisjoint(mapping.values())
    assert [(row["title"], row["content"], row["summary"], row["sort"], row["active"]) for row in old_scenes] == [
        (row["title"], row["content"], row["summary"], row["sort"], row["active"]) for row in new_scenes
    ]
    assert result["tables"]["setting_progressions"][0]["scene_id"] == mapping[second["id"]]
    async with app.state.sessionmaker() as db:
        units = list(await db.scalars(select(PlanningUnit).where(PlanningUnit.project_id == new_project_id)))
        assert len(units) == 1 and units[0].scene_id == mapping[second["id"]]
        chapters = list(await db.scalars(select(Chapter).where(Chapter.project_id == new_project_id)))
        assert all(chapter.content_revision == 0 for chapter in chapters)
        versions = list(await db.scalars(select(ChapterVersion).where(
            ChapterVersion.chapter_id.in_([chapter.id for chapter in chapters]),
        )))
        snapshots = [version.scene_snapshot for version in versions if version.scene_snapshot]
        assert snapshots
        assert any(item["planning"] and item["planning"]["unit_id"] == units[0].id
                   for snapshot in snapshots for item in snapshot["scenes"])
        assert all(item["id"] in set(mapping.values()) for snapshot in snapshots for item in snapshot["scenes"])
        assert await db.get(Scene, mapping[first["id"]]) is not None


async def test_real_v6_upgrade_creates_one_exact_scene_and_keeps_old_history(client):
    project, _, _, _ = await setup_scene(client, "\t旧🙂稿\r\n\n尾  ")
    data = await _backup(client, project["id"])
    data["version"] = 6
    tables = data["tables"]
    tables.pop("scenes")
    tables.pop("planning_unit_characters")
    for row in tables["chapter_versions"]:
        row.pop("scene_snapshot")
    parsed = importer.parse_backup(json.dumps(data).encode())
    assert parsed.tables["scenes"][0]["content"] == "\t旧🙂稿\r\n\n尾  "
    assert parsed.tables["scenes"][0]["separator_before"] == ""
    imported = await _import(client, data)
    result = await _backup(client, imported["project"]["id"])
    assert result["tables"]["chapter_versions"][0]["scene_snapshot"] is None
    assert len(result["tables"]["scenes"]) == 1


@pytest.mark.parametrize("variant", ["missing_scenes", "wrong_chapter", "projection", "missing_history_identity",
                                     "bad_snapshot", "old_smuggled", "word_count"])
async def test_v7_bad_core_graph_is_rejected_before_import(client, variant):
    project, _, _, _ = await setup_scene(client, "甲🙂乙")
    data = await _backup(client, project["id"])
    broken = deepcopy(data)
    if variant == "missing_scenes":
        broken["tables"].pop("scenes")
    elif variant == "wrong_chapter":
        broken["tables"]["scenes"][0]["chapter_id"] = 99999
    elif variant == "projection":
        broken["tables"]["chapters"][0]["content"] += "矛盾"
    elif variant == "missing_history_identity":
        broken["tables"]["chapter_versions"][0]["scene_snapshot"]["scenes"][0]["id"] = 99999
    elif variant == "bad_snapshot":
        broken["tables"]["chapter_versions"][0]["scene_snapshot"]["schema_version"] = 99
    elif variant == "old_smuggled":
        broken["version"] = 6
    else:
        broken["tables"]["scenes"][0]["word_count"] = 0
    response = await _preview_raw(client, json.dumps(broken).encode())
    assert response.status_code == 400, response.text
    if variant == "old_smuggled":
        assert "旧版备份不支持独立场景" in response.json()["detail"]
    else:
        assert response.json()["errors"]


async def test_scene_inherit_cannot_smuggle_local_values(client):
    project, chapter, url, scene = await setup_scene(client, "正文")
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "角色"})).json()
    saved = await client.patch(f"{base}/planning/units/batch", json={
        "expected_revision": (await client.get(f"{url}/scenes")).json()["planning_revision"],
        "expected_content_revisions": [{"chapter_id": chapter["id"], "content_revision": 0}],
        "targets": [{"kind": "scene", "id": scene["id"]}],
        "pov": {"op": "set", "character_id": character["id"]},
        "references": [{"kind": "character", "op": "replace", "ids": [character["id"]]}],
    })
    assert saved.status_code == 200, saved.text
    data = await _backup(client, project["id"])
    for field in ("pov", "character"):
        broken = deepcopy(data)
        broken["tables"]["planning_units"][0][f"{field}_mode"] = "inherit"
        rejected = await _preview_raw(client, json.dumps(broken).encode())
        assert rejected.status_code == 400 and "继承" in rejected.json()["detail"], rejected.text
