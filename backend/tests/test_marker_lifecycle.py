"""失效和退场标记必须可显式处理，同时保留版本身份。"""

import asyncio
import hashlib
import json

from sqlalchemy import select

from app.models import SceneMarker
from tests.helpers import register
from tests.test_project_transfer import _backup, _import
from tests.test_rich_document_api import MARKER, save_payload
from tests.test_scenes import setup_scene


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def assert_scrubbed_tombstone(row):
    assert row["status"] == "deleted"
    assert (row["start_offset"], row["end_offset"], row["last_start"]) == (None, None, None)
    assert (row["excerpt"], row["excerpt_truncated"]) == ("", False)
    assert (row["draft_excerpt"], row["draft_excerpt_truncated"], row["draft_last_start"]) == (None, False, None)


async def create_stale_marker(client, endpoint):
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "stale", "start": None, "end": None,
        "color": "green", "excerpt": "旧片段哨兵", "last_start": 0,
        "draft_excerpt": "草稿哨兵", "draft_excerpt_truncated": False, "draft_last_start": 0,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text


async def test_delete_stale_scrubs_backup_but_version_remains_restorable(client):
    project, _chapter, url, scene = await setup_scene(client, "正文")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    await create_stale_marker(client, endpoint)
    marked = (await client.get(f"{url}/versions")).json()[0]
    deleted = await client.delete(f"/api/v1/projects/{project['id']}/markers/{scene['id']}/{MARKER}", params={
        "expected_content_revision": 1,
    })
    assert deleted.status_code == 200, deleted.text
    backup = await _backup(client, project["id"], versions=0)
    assert not backup["tables"].get("chapter_versions")
    assert_scrubbed_tombstone(backup["tables"]["scene_markers"][0])
    assert "旧片段哨兵" not in json.dumps(backup, ensure_ascii=False)
    assert "草稿哨兵" not in json.dumps(backup, ensure_ascii=False)
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    marker = (await client.get(endpoint)).json()["document"]["markers"][0]
    assert marker["excerpt"] == "旧片段哨兵" and marker["draft_excerpt"] == "草稿哨兵"


async def test_history_restore_scrubs_removed_marker_and_can_restore_later_version(client):
    project, _chapter, url, scene = await setup_scene(client, "正文")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    await create_stale_marker(client, endpoint)
    versions = (await client.get(f"{url}/versions")).json()
    marked, baseline = versions[0], versions[-1]
    assert marked["id"] != baseline["id"]
    restored = await client.post(f"{url}/versions/{baseline['id']}/restore", json={
        "expected_content_revision": 1, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    backup = await _backup(client, project["id"], versions=0)
    assert_scrubbed_tombstone(backup["tables"]["scene_markers"][0])
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    marker = (await client.get(endpoint)).json()["document"]["markers"][0]
    assert marker["excerpt"] == "旧片段哨兵" and marker["draft_excerpt"] == "草稿哨兵"


async def test_cross_scene_relink_scrubs_source_and_preserves_historical_restore(client):
    project, _chapter, url, source = await setup_scene(client, "正文")
    target = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "目标"})
    assert target.status_code == 201, target.text
    target = target.json()
    endpoint = f"{url}/scenes/{source['id']}/document"
    await create_stale_marker(client, endpoint)
    marked = (await client.get(f"{url}/versions")).json()[0]
    moved = await client.post(f"/api/v1/projects/{project['id']}/markers/{source['id']}/{MARKER}/relink", json={
        "expected_source_revision": 2, "target_chapter_id": source["chapter_id"],
        "target_scene_id": target["id"], "expected_target_revision": 2,
        "start": 0, "end": 2, "selected_hash": digest("目标"),
    })
    assert moved.status_code == 200, moved.text
    backup = await _backup(client, project["id"], versions=0)
    old = next(row for row in backup["tables"]["scene_markers"] if row["scene_id"] == source["id"])
    new = next(row for row in backup["tables"]["scene_markers"] if row["scene_id"] == target["id"])
    assert_scrubbed_tombstone(old)
    assert new["origin_marker_id"] == old["id"] and new["status"] == "active"
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 3, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    marker = (await client.get(endpoint)).json()["document"]["markers"][0]
    assert marker["excerpt"] == "旧片段哨兵" and marker["draft_excerpt"] == "草稿哨兵"


async def setup_retired(client):
    project, _, url, first = await setup_scene(client, "正文🙂目标")
    initial = (await client.get(f"{url}/versions")).json()[0]
    created = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "旧景🙂"})
    assert created.status_code == 201, created.text
    second = created.json()["id"]
    endpoint = f"{url}/scenes/{second}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 2,
        "color": "blue", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    assert (await client.put(endpoint, json=body)).status_code == 200
    marked = (await client.get(f"{url}/versions")).json()[0]
    restored = await client.post(f"{url}/versions/{initial['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    path = f"/api/v1/projects/{project['id']}/markers"
    return project, url, first, second, marked, path


async def test_relink_retired_marker_preserves_scene_history(client):
    _, url, first, second, marked, path = await setup_retired(client)
    result = await client.post(f"{path}/{second}/{MARKER}/relink", json={
        "expected_source_revision": 3, "target_chapter_id": first["chapter_id"],
        "target_scene_id": first["id"], "expected_target_revision": 3,
        "start": 2, "end": 3, "selected_hash": digest("🙂"),
    })
    assert result.status_code == 200, result.text
    assert result.json()["source_revision"] == 4 == result.json()["target_revision"]
    markers = (await client.get(path)).json()["items"]
    assert len(markers) == 1
    assert (markers[0]["scene_id"], markers[0]["key"], markers[0]["status"], markers[0]["excerpt"]) == (
        first["id"], MARKER, "active", "🙂",
    )
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 4, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    markers = (await client.get(path)).json()["items"]
    assert len(markers) == 1 and markers[0]["scene_id"] == second and markers[0]["status"] == "active"


async def test_delete_retired_marker_and_restore_version(client):
    _, url, _first, second, marked, path = await setup_retired(client)
    result = await client.delete(f"{path}/{second}/{MARKER}", params={"expected_content_revision": 3})
    assert result.status_code == 200, result.text
    assert (await client.get(path)).json()["items"] == []
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 4, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert (await client.get(path)).json()["items"][0]["scene_id"] == second


async def test_relink_requires_revision_selection_and_ownership(client, make_client):
    project, _url, first, second, _marked, path = await setup_retired(client)
    data = {"expected_source_revision": 3, "target_chapter_id": first["chapter_id"],
            "target_scene_id": first["id"], "expected_target_revision": 3,
            "start": 0, "end": 2, "selected_hash": digest("错误")}
    assert (await client.post(f"{path}/{second}/{MARKER}/relink", json=data)).status_code == 409
    data["selected_hash"] = digest("正文")
    data["expected_source_revision"] = 2
    assert (await client.post(f"{path}/{second}/{MARKER}/relink", json=data)).status_code == 409
    data["expected_source_revision"] = 3
    stranger = make_client()
    await register(stranger, "marker_other")
    assert (await stranger.post(f"{path}/{second}/{MARKER}/relink", json=data)).status_code == 404
    denied = await stranger.delete(f"{path}/{second}/{MARKER}", params={"expected_content_revision": 3})
    assert denied.status_code == 404
    assert (await client.get(path)).json()["items"][0]["scene_active"] is False
    assert project["id"] > 0


async def test_cross_chapter_relink_checks_both_versions_and_crlf(client):
    project, _url, first, second, _marked, path = await setup_retired(client)
    created = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "二", "content": "甲\r\n🙂乙",
    })
    assert created.status_code == 201, created.text
    chapter2 = created.json()["id"]
    url2 = f"/api/v1/projects/{project['id']}/chapters/{chapter2}"
    scene2 = (await client.get(f"{url2}/scenes")).json()["items"][0]
    data = {"expected_source_revision": 3, "target_chapter_id": chapter2,
            "target_scene_id": scene2["id"], "expected_target_revision": 1,
            "start": 3, "end": 4, "selected_hash": digest("🙂")}
    assert (await client.post(f"{path}/{second}/{MARKER}/relink", json=data)).status_code == 409
    data["expected_target_revision"] = 0
    data.update(start=1, end=2, selected_hash=digest("\r"))
    assert (await client.post(f"{path}/{second}/{MARKER}/relink", json=data)).status_code == 422
    data.update(start=3, end=4, selected_hash=digest("🙂"))
    result = await client.post(f"{path}/{second}/{MARKER}/relink", json=data)
    assert result.status_code == 200, result.text
    assert result.json()["source_chapter_id"] == first["chapter_id"]
    assert result.json()["source_revision"] == 4 and result.json()["target_revision"] == 1
    markers = (await client.get(path)).json()["items"]
    assert len(markers) == 1 and markers[0]["chapter_id"] == chapter2 and markers[0]["excerpt"] == "🙂"


async def test_same_scene_stale_relink_and_explicit_active_delete(client):
    project, _chapter, url, scene = await setup_scene(client, "新🙂正文")
    doc = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(doc)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "stale", "start": None, "end": None,
        "color": "green", "excerpt": "旧文", "last_start": 0,
    })
    body["add_marker_keys"].append(MARKER)
    assert (await client.put(doc, json=body)).status_code == 200
    path = f"/api/v1/projects/{project['id']}/markers/{scene['id']}/{MARKER}"
    result = await client.post(f"{path}/relink", json={
        "expected_source_revision": 1, "target_chapter_id": scene["chapter_id"],
        "target_scene_id": scene["id"], "expected_target_revision": 1,
        "start": 1, "end": 2, "selected_hash": digest("🙂"),
    })
    assert result.status_code == 200, result.text
    assert (await client.get(doc)).json()["document"]["markers"][0]["status"] == "active"
    deleted = await client.delete(path, params={"expected_content_revision": 2})
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(doc)).json()["document"]["markers"] == []

async def test_cannot_restore_source_tombstone_after_real_cross_scene_relink(client, make_client):
    project, _chapter, url, source = await setup_scene(client, "原文")
    target = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "目标🙂"})
    assert target.status_code == 201, target.text
    target = target.json()
    source_doc = f"{url}/scenes/{source['id']}/document"
    body = save_payload((await client.get(source_doc)).json())
    marker = {"key": MARKER, "section_key": None, "status": "stale", "start": None, "end": None,
              "color": "green", "excerpt": "原文", "excerpt_truncated": False, "last_start": 0}
    body["document"]["markers"] = [marker]
    body["add_marker_keys"] = [MARKER]
    saved = await client.put(source_doc, json=body)
    assert saved.status_code == 200
    path = f"/api/v1/projects/{project['id']}/markers/{source['id']}/{MARKER}/relink"
    moved = await client.post(path, json={
        "expected_source_revision": saved.json()["content_revision"], "target_chapter_id": source["chapter_id"],
        "target_scene_id": target["id"], "expected_target_revision": saved.json()["content_revision"],
        "start": 0, "end": 2, "selected_hash": digest("目标"),
    })
    assert moved.status_code == 200, moved.text
    body = save_payload((await client.get(source_doc)).json())
    body["document"]["markers"] = [marker]
    body["restore_marker_keys"] = [MARKER]
    stranger = make_client()
    await register(stranger, "restore_marker_other")
    assert (await stranger.put(source_doc, json=body)).status_code == 404
    blocked = await client.put(source_doc, json=body)
    assert blocked.status_code == 409 and blocked.json()["code"] == "marker_identity_conflict"
    assert (await client.get(source_doc)).json()["document"]["markers"] == []
    other_doc = f"{url}/scenes/{target['id']}/document"
    assert (await client.get(other_doc)).json()["document"]["markers"][0]["status"] == "active"


async def test_cross_chapter_relink_then_restore_source_history(client):
    """跨章迁移后恢复原章历史不能制造两处活动身份。"""
    project, _chapter, url, source = await setup_scene(client, "原文")
    source_doc = f"{url}/scenes/{source['id']}/document"
    body = save_payload((await client.get(source_doc)).json())
    body["document"]["markers"] = [{
        "key": MARKER, "section_key": None, "status": "stale", "start": None, "end": None,
        "color": "green", "excerpt": "原文", "last_start": 0,
    }]
    body["add_marker_keys"] = [MARKER]
    saved = await client.put(source_doc, json=body)
    assert saved.status_code == 200, saved.text
    marked = (await client.get(f"{url}/versions")).json()[0]
    target_chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "第二章", "content": "目标文本",
    })
    assert target_chapter.status_code == 201, target_chapter.text
    target_url = f"/api/v1/projects/{project['id']}/chapters/{target_chapter.json()['id']}"
    target = (await client.get(f"{target_url}/scenes")).json()["items"][0]
    moved = await client.post(f"/api/v1/projects/{project['id']}/markers/{source['id']}/{MARKER}/relink", json={
        "expected_source_revision": 1, "target_chapter_id": target_chapter.json()["id"],
        "target_scene_id": target["id"], "expected_target_revision": 0,
        "start": 0, "end": 2, "selected_hash": digest("目标"),
    })
    assert moved.status_code == 200, moved.text
    preview = await client.get(f"{url}/versions/{marked['id']}/restore-preview", params={
        "expected_content_revision": 2,
    })
    assert preview.status_code == 200, preview.text
    assert any("其他活动章节" in item["label"] for item in preview.json()["blockers"])
    before = await _backup(client, project["id"], versions=0)
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 409, restored.text
    assert any("其他活动章节" in item["label"] for item in restored.json()["blockers"])
    after = await _backup(client, project["id"], versions=0)
    assert before["tables"]["scene_markers"] == after["tables"]["scene_markers"]
    assert before["tables"]["scenes"] == after["tables"]["scenes"]
    deleted = await client.delete(f"/api/v1/projects/{project['id']}/markers/{target['id']}/{MARKER}", params={
        "expected_content_revision": 1,
    })
    assert deleted.status_code == 200, deleted.text
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert (await client.get(source_doc)).json()["document"]["markers"][0]["excerpt"] == "原文"


async def test_history_restore_allows_unrelated_same_uuid_in_another_chapter(client):
    project, _chapter, url, source = await setup_scene(client, "原文")
    source_doc = f"{url}/scenes/{source['id']}/document"
    await create_stale_marker(client, source_doc)
    marked = (await client.get(f"{url}/versions")).json()[0]
    deleted = await client.delete(f"/api/v1/projects/{project['id']}/markers/{source['id']}/{MARKER}", params={
        "expected_content_revision": 1,
    })
    assert deleted.status_code == 200, deleted.text
    other = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "另一章", "content": "独立文本",
    })
    assert other.status_code == 201, other.text
    other_url = f"/api/v1/projects/{project['id']}/chapters/{other.json()['id']}"
    other_scene = (await client.get(f"{other_url}/scenes")).json()["items"][0]
    other_doc = f"{other_url}/scenes/{other_scene['id']}/document"
    payload = save_payload((await client.get(other_doc)).json())
    payload["document"]["markers"] = [{
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 2,
        "color": "blue", "excerpt": "", "last_start": None,
    }]
    payload["add_marker_keys"] = [MARKER]
    created = await client.put(other_doc, json=payload)
    assert created.status_code == 200, created.text
    restored = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert len((await client.get(source_doc)).json()["document"]["markers"]) == 1
    assert len((await client.get(other_doc)).json()["document"]["markers"]) == 1


async def test_existing_cross_chapter_double_live_backup_roundtrip(app, client):
    """旧版迁移后恢复历史可能产生双活；导入不能丢弃任何一侧。"""
    project, _chapter, url, source = await setup_scene(client, "原文")
    source_doc = f"{url}/scenes/{source['id']}/document"
    await create_stale_marker(client, source_doc)
    other = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "另一章", "content": "目标文本",
    })
    assert other.status_code == 201, other.text
    other_url = f"/api/v1/projects/{project['id']}/chapters/{other.json()['id']}"
    target = (await client.get(f"{other_url}/scenes")).json()["items"][0]
    moved = await client.post(f"/api/v1/projects/{project['id']}/markers/{source['id']}/{MARKER}/relink", json={
        "expected_source_revision": 1, "target_chapter_id": other.json()["id"],
        "target_scene_id": target["id"], "expected_target_revision": 0,
        "start": 0, "end": 2, "selected_hash": digest("目标"),
    })
    assert moved.status_code == 200, moved.text
    # 构造旧版历史恢复后的真实持久状态；新版本不能再通过历史恢复制造这一状态。
    async with app.state.sessionmaker() as db:
        old = await db.scalar(select(SceneMarker).where(SceneMarker.scene_id == source["id"]))
        old.status, old.excerpt, old.last_start = "stale", "旧片段哨兵", 0
        old.draft_excerpt, old.draft_last_start = "草稿哨兵", 0
        await db.commit()
    backup = await _backup(client, project["id"], versions=0)
    imported = await _import(client, backup)  # 内含真实预览。
    result = await _backup(client, imported["project"]["id"], versions=0)
    rows = result["tables"]["scene_markers"]
    old = next(row for row in rows if row["status"] == "stale")
    new = next(row for row in rows if row["status"] == "active")
    assert new["origin_marker_id"] == old["id"]
    assert (old["excerpt"], old["draft_excerpt"]) == ("旧片段哨兵", "草稿哨兵")
    assert (new["start_offset"], new["end_offset"]) == (0, 2)


async def test_split_and_merge_scrub_migrated_marker_tombstones(app, client):
    project, _chapter, url, scene = await setup_scene(client, "甲乙丙丁戊己")
    async with app.state.sessionmaker() as db:
        db.add(SceneMarker(scene_id=scene["id"], logical_key=MARKER, status="active", color="blue",
                           start_offset=4, end_offset=5, excerpt="拆景前哨兵"))
        await db.commit()
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 3,
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    backup = await _backup(client, project["id"], versions=0)
    old = next(row for row in backup["tables"]["scene_markers"] if row["scene_id"] == left["id"])
    assert_scrubbed_tombstone(old)
    async with app.state.sessionmaker() as db:
        moved = await db.scalar(select(SceneMarker).where(SceneMarker.scene_id == right["id"]))
        moved.excerpt = "合景前哨兵"
        await db.commit()
    preview = await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 1, "scene_ids": [left["id"], right["id"]],
    })
    assert preview.status_code == 200 and not preview.json()["blockers"], preview.text
    info = preview.json()
    merged = await client.post(f"{url}/scenes/merge", json={
        "expected_content_revision": 1, "scene_ids": info["scene_ids"],
        "expected_planning_revision": info["planning_revision"], "signature": info["signature"],
        "metadata_source": "left",
    })
    assert merged.status_code == 200, merged.text
    backup = await _backup(client, project["id"], versions=0)
    moved = next(row for row in backup["tables"]["scene_markers"] if row["scene_id"] == right["id"])
    assert_scrubbed_tombstone(moved)
    assert "拆景前哨兵" not in json.dumps(backup, ensure_ascii=False)
    active = next(row for row in backup["tables"]["scene_markers"] if row["scene_id"] == left["id"])
    assert active["excerpt"] == "合景前哨兵"


async def test_split_merge_preserves_active_marker_metadata_and_history(client):
    project, _chapter, url, scene = await setup_scene(client, "甲乙丙丁戊己")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"] = [{
        "key": MARKER, "section_key": None, "status": "active", "start": 4, "end": 5,
        "color": "purple", "excerpt": "仍须保留的合法元数据", "excerpt_truncated": True, "last_start": 3,
    }]
    body["add_marker_keys"] = [MARKER]
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    original = saved.json()["document"]["markers"][0]
    original_version = (await client.get(f"{url}/versions")).json()[0]
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 1, "offset": 3,
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    target = f"{url}/scenes/{right['id']}/document"
    moved = (await client.get(target)).json()["document"]["markers"][0]
    assert (moved["start"], moved["end"]) == (1, 2)
    assert (moved["excerpt"], moved["excerpt_truncated"], moved["last_start"]) == (
        original["excerpt"], original["excerpt_truncated"], original["last_start"],
    )
    backup = await _backup(client, project["id"])
    imported = await _import(client, backup)
    roundtrip = await _backup(client, imported["project"]["id"])
    active = next(row for row in roundtrip["tables"]["scene_markers"] if row["status"] == "active")
    assert (active["excerpt"], active["excerpt_truncated"], active["last_start"]) == (
        original["excerpt"], original["excerpt_truncated"], original["last_start"],
    )
    preview = await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 2, "scene_ids": [left["id"], right["id"]],
    })
    assert preview.status_code == 200 and not preview.json()["blockers"], preview.text
    info = preview.json()
    merged = await client.post(f"{url}/scenes/merge", json={
        "expected_content_revision": 2, "scene_ids": info["scene_ids"],
        "expected_planning_revision": info["planning_revision"], "signature": info["signature"],
        "metadata_source": "left",
    })
    assert merged.status_code == 200, merged.text
    assert (await client.get(endpoint)).json()["document"]["markers"][0] == original
    restored = await client.post(f"{url}/versions/{original_version['id']}/restore", json={
        "expected_content_revision": 3, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert (await client.get(endpoint)).json()["document"]["markers"][0] == original


async def test_cross_chapter_relink_and_history_restore_are_serialized(client):
    project, _chapter, url, source = await setup_scene(client, "原文")
    endpoint = f"{url}/scenes/{source['id']}/document"
    await create_stale_marker(client, endpoint)
    marked = (await client.get(f"{url}/versions")).json()[0]
    other = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "另一章", "content": "目标文本",
    })
    assert other.status_code == 201, other.text
    other_url = f"/api/v1/projects/{project['id']}/chapters/{other.json()['id']}"
    target = (await client.get(f"{other_url}/scenes")).json()["items"][0]
    path = f"/api/v1/projects/{project['id']}/markers"
    moved, restored = await asyncio.gather(
        client.post(f"{path}/{source['id']}/{MARKER}/relink", json={
            "expected_source_revision": 1, "target_chapter_id": other.json()["id"],
            "target_scene_id": target["id"], "expected_target_revision": 0,
            "start": 0, "end": 2, "selected_hash": digest("目标"),
        }),
        client.post(f"{url}/versions/{marked['id']}/restore", json={
            "expected_content_revision": 1, "confirm_rich_document_loss": True,
        }),
    )
    assert sorted([moved.status_code, restored.status_code]) == [200, 409], (moved.text, restored.text)
    markers = (await client.get(path)).json()["items"]
    assert len(markers) == 1 and markers[0]["key"] == MARKER
