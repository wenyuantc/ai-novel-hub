"""空正文也可能含有无法从纯文本恢复的作者资料。"""

import pytest
from sqlalchemy import event, select

from app.core.errors import ConflictError
from app.models import Chapter, ChapterVersion, SceneDocumentMeta, SceneMarker, SceneSection
from app.services import rich_document_store, version_service
from tests.test_project_transfer import _backup, _import
from tests.test_rich_document_api import KEY, add_section, save_payload
from tests.test_scenes import setup_scene

SECTION_KEY = "645d44c0-cc16-4949-973b-542022da8421"
MARKER_KEY = "d4baf738-d1ca-4d51-a9cc-22fe47213105"


async def test_first_section_in_empty_chapter_gets_a_recoverable_version(app, client):
    _project, chapter, _url, scene = await setup_scene(client, "  \r\n")
    async with app.state.sessionmaker() as db:
        parent = await db.get(Chapter, chapter["id"])
        assert await version_service.get_latest(db, parent.id) is None
        db.add(SceneSection(scene_id=scene["id"], logical_key=SECTION_KEY, anchor=0, sort=0,
                            title="作者笔记", content="保留🙂\r\n内容", include_in_ai=False))
        await db.flush()
        version = await version_service.record_manual(db, parent)
        assert version is not None and version.source == "manual"
        snapshot = version.scene_snapshot
        assert snapshot["schema_version"] == 2
        document = snapshot["scenes"][0]["document"]
        assert document["sections"][0]["key"] == SECTION_KEY
        assert document["sections"][0]["content"] == "保留🙂\r\n内容"
        assert document["sections"][0]["include_in_ai"] is False
        assert version.content == "  \r\n"


async def test_empty_chapter_baseline_preserves_deleted_section_and_stale_marker(app, client):
    _project, chapter, _url, scene = await setup_scene(client, "")
    async with app.state.sessionmaker() as db:
        parent = await db.get(Chapter, chapter["id"])
        section = SceneSection(scene_id=scene["id"], logical_key=SECTION_KEY, anchor=0, sort=0,
                               title="作者笔记", content="删除前", include_in_ai=False)
        db.add(section)
        await db.flush()
        db.add(SceneMarker(scene_id=scene["id"], logical_key=MARKER_KEY, section_id=section.id,
                           status="stale", color="yellow", last_start=0, excerpt="曾经文字"))
        await db.flush()
        await version_service.ensure_baseline(db, parent)
        baseline = await version_service.get_latest(db, parent.id)
        assert baseline is not None and baseline.scene_snapshot["schema_version"] == 2
        assert baseline.scene_snapshot["scenes"][0]["document"]["markers"][0]["excerpt"] == "曾经文字"
        section.active = False
        await db.flush()
        changed = await version_service.record_manual(db, parent)
        assert changed is not None and changed.id != baseline.id
        doc = changed.scene_snapshot["scenes"][0]["document"]
        assert doc["sections"] == [] and doc["retired_section_keys"] == [SECTION_KEY]
        assert doc["markers"][0]["section_key"] == SECTION_KEY
        assert len(list(await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == parent.id)))) == 2


async def test_pure_whitespace_without_author_material_still_skips_initial_version(app, client):
    _project, chapter, _url, _scene = await setup_scene(client, " \r\n")
    async with app.state.sessionmaker() as db:
        parent = await db.get(Chapter, chapter["id"])
        await version_service.ensure_baseline(db, parent)
        assert await version_service.record_manual(db, parent) is None
        assert await version_service.get_latest(db, parent.id) is None


async def test_pending_first_section_cannot_produce_a_silent_empty_snapshot(app, client):
    project, chapter, url, scene = await setup_scene(client, "")
    async with app.state.sessionmaker() as db:
        parent = await db.get(Chapter, chapter["id"])
        db.add(SceneSection(scene_id=scene["id"], logical_key=SECTION_KEY, anchor=0, sort=0,
                            title="笔记", content="首次创建", include_in_ai=False))
        with pytest.raises(ConflictError) as exc:
            await version_service.record(db, parent, "manual")
        assert exc.value.extra["code"] == "rich_document_pending"
        await db.flush()
        version = await version_service.record(db, parent, "manual")
        version_id = version.id
        await db.commit()
    backup = await _backup(client, project["id"], versions=1)
    imported = await _import(client, backup)
    restored_backup = await _backup(client, imported["project"]["id"], versions=1)
    assert restored_backup["tables"]["chapter_versions"][0]["scene_snapshot"]["scenes"][0][
        "document"]["sections"][0]["content"] == "首次创建"
    response = await client.post(f"{url}/versions/{version_id}/restore", json={"expected_content_revision": 0})
    assert response.status_code == 200, response.text
    document = (await client.get(f"{url}/scenes/{scene['id']}/document")).json()["document"]
    assert document["sections"][0]["content"] == "首次创建"


async def test_document_capture_batches_scene_ids(app, client, monkeypatch):
    _project, chapter, url, first = await setup_scene(client, "甲")
    second = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "乙"})
    third = await client.post(f"{url}/scenes", json={"expected_content_revision": 1, "content": "丙"})
    assert second.status_code == third.status_code == 201
    scene_ids = [first["id"], second.json()["id"], third.json()["id"]]
    statements = []

    def observe(_conn, _cursor, statement, parameters, _context, _many):
        if "from scene_documents" in statement.lower():
            statements.append(parameters)

    monkeypatch.setattr(rich_document_store, "CAPTURE_BATCH", 2)
    async with app.state.sessionmaker() as db:
        db.add_all([SceneDocumentMeta(scene_id=scene_id, formatting={}) for scene_id in scene_ids])
        await db.flush()
        parent = await db.get(Chapter, chapter["id"])
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe)
        try:
            await version_service.record(db, parent, "manual")
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe)
    assert len(statements) == 2 and all(len(params) <= 2 for params in statements)


@pytest.mark.parametrize("siblings", [False, True])
async def test_history_blocks_cross_chapter_section_lineage_and_preserves_data(app, client, siblings):
    project, _chapter, url, source = await setup_scene(client, "原文")
    endpoint = f"{url}/scenes/{source['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    marked = (await client.get(f"{url}/versions")).json()[0]
    deletion = save_payload(saved.json())
    deletion["document"]["sections"] = []
    deletion["document"]["retired_section_keys"] = [KEY]
    deletion["delete_section_keys"] = [KEY]
    deleted = await client.put(endpoint, json=deletion)
    assert deleted.status_code == 200, deleted.text
    other = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "另一章", "content": "目标文本",
    })
    assert other.status_code == 201, other.text
    other_url = f"/api/v1/projects/{project['id']}/chapters/{other.json()['id']}"
    target = (await client.get(f"{other_url}/scenes")).json()["items"][0]
    target_doc = f"{other_url}/scenes/{target['id']}/document"
    body = save_payload((await client.get(target_doc)).json())
    add_section(body)
    body["document"]["sections"][0]["content"] = "目标章作者材料"
    assert (await client.put(target_doc, json=body)).status_code == 200
    third_scene_id = None
    if siblings:
        third = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
            "title": "来源章", "content": "来源",
        })
        assert third.status_code == 201, third.text
        third_url = f"/api/v1/projects/{project['id']}/chapters/{third.json()['id']}"
        third_scene_id = (await client.get(f"{third_url}/scenes")).json()["items"][0]["id"]
    async with app.state.sessionmaker() as db:
        old = await db.scalar(select(SceneSection).where(SceneSection.scene_id == source["id"]))
        new = await db.scalar(select(SceneSection).where(SceneSection.scene_id == target["id"]))
        if siblings:
            root = SceneSection(scene_id=third_scene_id, logical_key=KEY, anchor=0, sort=0,
                                title="历史来源", content="", active=False)
            db.add(root)
            await db.flush()
            old.origin_section_id = root.id
            new.origin_section_id = root.id
        else:
            new.origin_section_id = old.id
        await db.commit()
    preview = await client.get(f"{url}/versions/{marked['id']}/restore-preview", params={
        "expected_content_revision": 2,
    })
    assert preview.status_code == 200, preview.text
    assert any("区块" in blocker["label"] and "其他活动章节" in blocker["label"]
               for blocker in preview.json()["blockers"])
    blocked = await client.post(f"{url}/versions/{marked['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert blocked.status_code == 409, blocked.text
    assert (await client.get(endpoint)).json()["document"]["sections"] == []
    assert (await client.get(target_doc)).json()["document"]["sections"][0]["content"] == "目标章作者材料"
