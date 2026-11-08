"""富格式／区块／标记是作品备份核心资料，不允许错景引用或伪造旧格式。"""

import json
from contextlib import aclosing
from copy import deepcopy

import pytest
from sqlalchemy import event, update

from app.core.errors import ConflictError
from app.models import Chapter, SceneDocumentMeta, SceneMarker, SceneSection
from app.schemas.transfer import BackupOptions
from app.services import version_service
from app.services.project_transfer import exporter, importer
from app.services.project_transfer.registry import RULES_BY_TABLE
from tests.test_project_transfer import _backup, _import, _preview_raw
from tests.test_scenes import setup_scene

SECTION_KEY = "645d44c0-cc16-4949-973b-542022da8421"
MARKER_KEY = "d4baf738-d1ca-4d51-a9cc-22fe47213105"


async def _seed_rich(app, client):
    project, chapter, url, first = await setup_scene(client, "甲😀乙\r\n尾")
    second = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "别景"})
    assert second.status_code == 201
    async with app.state.sessionmaker() as db:
        db.add(SceneDocumentMeta(scene_id=first["id"], formatting={
            "version": 1, "inline": [{"kind": "bold", "start": 0, "end": 1}],
        }))
        block = SceneSection(scene_id=first["id"], logical_key=SECTION_KEY, anchor=1, sort=0,
                             title="备选", content="仅作者可见😀", include_in_ai=False)
        db.add(block)
        await db.flush()
        db.add(SceneMarker(scene_id=first["id"], logical_key=MARKER_KEY, section_id=block.id,
                           status="active", color="yellow", start_offset=0, end_offset=2))
        await db.commit()
    return project, chapter, first, second.json()


async def test_rich_backup_roundtrip_rebinds_scene_and_section_ids(app, client):
    project, _chapter, _first, _second = await _seed_rich(app, client)
    data = await _backup(client, project["id"])
    assert (data["version"], data["app_schema"]) == (9, "0041")
    imported = await _import(client, data)
    result = await _backup(client, imported["project"]["id"])
    old_block, new_block = data["tables"]["scene_sections"][0], result["tables"]["scene_sections"][0]
    new_mark = result["tables"]["scene_markers"][0]
    assert new_block["id"] != old_block["id"] and new_block["scene_id"] != old_block["scene_id"]
    assert new_block["logical_key"] == SECTION_KEY and new_block["content"] == "仅作者可见😀"
    assert new_block["include_in_ai"] is False and new_mark["logical_key"] == MARKER_KEY
    assert new_mark["section_id"] == new_block["id"] and new_mark["scene_id"] == new_block["scene_id"]
    assert result["tables"]["scene_documents"][0]["formatting"]["inline"][0]["kind"] == "bold"


async def test_v8_marker_without_truncation_flag_defaults_to_full_excerpt(app, client):
    project, _chapter, _first, _second = await _seed_rich(app, client)
    backup = await _backup(client, project["id"], versions=1)
    for row in backup["tables"]["scene_markers"]:
        row.pop("excerpt_truncated", None)
    for version in backup["tables"]["chapter_versions"]:
        snapshot = version.get("scene_snapshot") or {}
        if snapshot.get("schema_version") == 2:
            for scene in snapshot["scenes"]:
                for marker in scene["document"]["markers"]:
                    marker.pop("excerpt_truncated", None)
    imported = await _import(client, backup)
    after = await _backup(client, imported["project"]["id"], versions=1)
    assert after["tables"]["scene_markers"][0]["excerpt_truncated"] is False


@pytest.mark.parametrize("variant", [
    "cross_scene", "invalid_format", "duplicate_document", "bad_anchor", "missing_table",
])
async def test_rich_backup_invalid_core_is_rejected_during_preview(app, client, variant):
    project, _chapter, _first, second = await _seed_rich(app, client)
    broken = deepcopy(await _backup(client, project["id"]))
    if variant == "cross_scene":
        broken["tables"]["scene_markers"][0]["scene_id"] = second["id"]
    elif variant == "invalid_format":
        broken["tables"]["scene_documents"][0]["formatting"]["inline"][0]["end"] = 999
    elif variant == "duplicate_document":
        broken["tables"]["scene_documents"].append(deepcopy(broken["tables"]["scene_documents"][0]))
    elif variant == "bad_anchor":
        broken["tables"]["scene_sections"][0]["anchor"] = 4  # 位于 CRLF 内部。
    else:
        broken["tables"].pop("scene_sections")
    response = await _preview_raw(client, json.dumps(broken).encode())
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("variant", ["current", "history"])
async def test_backup_rejects_rich_anchor_inside_cross_scene_crlf(client, variant):
    from tests.test_rich_document_api import add_section, save_payload

    project, _chapter, url, scene = await setup_scene(client, "甲\r\n乙")
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert split.status_code == 200, split.text
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["sections"][0]["anchor"] = 0
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    backup = await _backup(client, project["id"], versions=1)
    assert (await _preview_raw(client, json.dumps(backup).encode())).status_code == 200
    if variant == "current":
        backup["tables"]["scene_sections"][0]["anchor"] = 2
    else:
        snapshot = next(version["scene_snapshot"] for version in backup["tables"]["chapter_versions"]
                        if version["scene_snapshot"]["schema_version"] == 2)
        snapshot["scenes"][0]["document"]["sections"][0]["anchor"] = 2
    rejected = await _preview_raw(client, json.dumps(backup).encode())
    assert rejected.status_code == 400, rejected.text


async def test_v7_backup_upgrade_and_reject_future_rich_data(client):
    project, _chapter, _url, _scene = await setup_scene(client, "旧🙂稿\r\n尾")
    data = await _backup(client, project["id"])
    old = deepcopy(data)
    old["version"] = 7
    for table in ("scene_documents", "scene_sections", "scene_markers"):
        old["tables"].pop(table)
    parsed = importer.parse_backup(json.dumps(old).encode())
    assert parsed.tables["scenes"][0]["content"] == "旧🙂稿\r\n尾"
    assert all(parsed.tables[table] == [] for table in ("scene_documents", "scene_sections", "scene_markers"))
    bad = deepcopy(old)
    bad["tables"]["scene_sections"] = [{"id": 123}]
    response = await _preview_raw(client, json.dumps(bad).encode())
    assert response.status_code == 400


async def test_rich_metadata_exports_in_bounded_scene_key_pages(app, client):
    project, _chapter, url, first = await setup_scene(client, "一")
    second = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "二"})
    third = await client.post(f"{url}/scenes", json={"expected_content_revision": 1, "content": "三"})
    assert second.status_code == third.status_code == 201
    ids = [first["id"], second.json()["id"], third.json()["id"]]
    statements = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if "scene_documents" in statement.lower():
            statements.append(statement.lower())

    async with app.state.sessionmaker() as db:
        db.add_all([SceneDocumentMeta(scene_id=scene_id, formatting={}) for scene_id in reversed(ids)])
        await db.commit()
        plan = await exporter.prepare_export(db, 1, project["id"], BackupOptions())
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe)
        try:
            rows = [row async for row in exporter._iter_rows(db, RULES_BY_TABLE["scene_documents"], plan)]
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe)
    assert [row["scene_id"] for row in rows] == ids
    assert len(statements) >= 3
    assert all("limit" in statement and "scene_documents.scene_id >" in statement for statement in statements)


async def test_rich_export_does_not_finish_after_document_revision_changes(app, client):
    project, chapter, _first, _second = await _seed_rich(app, client)
    async with app.state.sessionmaker() as db:
        plan = await exporter.prepare_export(db, 1, project["id"], BackupOptions())
    async with aclosing(exporter.stream_export(app.state.sessionmaker, plan)) as stream:
        async for chunk in stream:
            if chunk.endswith(b'"scene_documents":['):
                break
        else:
            pytest.fail("富格式核心表未参与导出")
        async with app.state.sessionmaker() as db:
            await db.execute(update(Chapter).where(Chapter.id == chapter["id"]).values(
                content_revision=Chapter.content_revision + 1,
            ))
            await db.commit()
        with pytest.raises(ConflictError) as stale:
            async for chunk in stream:
                assert chunk != b"}}"
        assert stale.value.extra["code"] == "scene_context_changed"


async def _backup_with_rich_history(app, client):
    project, chapter, _first, _second = await _seed_rich(app, client)
    async with app.state.sessionmaker() as db:
        parent = await db.get(Chapter, chapter["id"])
        await version_service.record(db, parent, "manual")
        await db.commit()
    data = await _backup(client, project["id"], versions=1)
    snapshot = next(row["scene_snapshot"] for row in data["tables"]["chapter_versions"]
                    if row["scene_snapshot"]["schema_version"] == 2)
    return project, data, snapshot


async def test_rich_history_backup_rebinds_scene_preserving_section_and_marker_keys(app, client):
    _project, data, snapshot = await _backup_with_rich_history(app, client)
    imported = await _import(client, data)
    after = await _backup(client, imported["project"]["id"], versions=1)
    restored = next(row["scene_snapshot"] for row in after["tables"]["chapter_versions"]
                    if row["scene_snapshot"]["schema_version"] == 2)
    before_scene, after_scene = snapshot["scenes"][0], restored["scenes"][0]
    assert after_scene["id"] != before_scene["id"]
    assert after_scene["document"] == before_scene["document"]
    assert after_scene["document"]["sections"][0]["key"] == SECTION_KEY
    assert after_scene["document"]["markers"][0]["key"] == MARKER_KEY


@pytest.mark.parametrize("identity", ["section", "marker"])
async def test_rich_history_rejects_snapshot_identity_missing_from_same_scene(app, client, identity):
    _project, data, snapshot = await _backup_with_rich_history(app, client)
    document = snapshot["scenes"][0]["document"]
    element = document["sections"][0] if identity == "section" else document["markers"][0]
    element["key"] = "8c6d7ad3-f24e-4957-96ee-c9d5390f4b38"
    if identity == "section":
        document["markers"][0]["section_key"] = element["key"]
    response = await _preview_raw(client, json.dumps(data).encode())
    assert response.status_code == 400, response.text

@pytest.mark.parametrize("changes", [
    {"draft_excerpt": 123, "draft_last_start": 0},
    {"draft_excerpt": "片段", "draft_last_start": "无效"},
    {"draft_excerpt": None, "draft_excerpt_truncated": True},
    {"draft_excerpt": "x" * 5001, "draft_last_start": 0},
])
async def test_rich_backup_rejects_invalid_unverified_draft_excerpt(app, client, changes):
    project, _chapter, _first, _second = await _seed_rich(app, client)
    backup = await _backup(client, project["id"])
    row = backup["tables"]["scene_markers"][0]
    # 退场行不会进入普通文档投影，但仍需校验备份中保留的身份资料。
    row["status"] = "deleted"
    row.update(changes)
    rejected = await _preview_raw(client, json.dumps(backup).encode())
    assert rejected.status_code == 400, rejected.text
