"""场景权威正文、稳定身份、轻量只读目录和结构并发的 HTTP 回归。"""

import hashlib

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import Chapter, Scene
from app.services.scene_service import list_scenes
from tests.helpers import create_project, register


async def setup_scene(client, content="甲🙂\n\n乙\t 尾  "):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/chapters", json={"title": "第一章", "content": content})
    assert response.status_code == 201, response.text
    chapter = response.json()
    url = f"{base}/chapters/{chapter['id']}"
    page = (await client.get(f"{url}/scenes")).json()
    return project, chapter, url, page["items"][0]


async def test_initial_scene_and_legacy_single_scene_write(client):
    _, chapter, url, scene = await setup_scene(client)
    assert chapter["content_revision"] == 0
    detail = (await client.get(f"{url}/scenes/{scene['id']}")).json()
    assert detail["content"] == chapter["content"]
    assert detail["separator_before"] == "" and detail["summary"] == ""
    assert detail["planning"]["raw"]["modes"]["pov"] == "inherit"
    saved = await client.patch(url, json={"content": "新\t稿 🙂", "expected_content_revision": 0})
    assert saved.status_code == 200, saved.text
    assert saved.json()["content_revision"] == 1
    assert (await client.get(f"{url}/scenes/{scene['id']}")).json()["content"] == "新\t稿 🙂"
    stale = await client.patch(f"{url}/scenes/{scene['id']}", json={
        "expected_content_revision": 0, "content": "不能覆盖",
    })
    assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision"
    assert stale.json()["current_revision"] == 1 and "不能覆盖" not in stale.text


async def test_unicode_split_merge_preserves_every_character_and_identity(app, client):
    original = "\t甲👩‍🚀\r\n\n乙\u3000尾  "
    _, chapter, url, first = await setup_scene(client, original)
    split = await client.post(f"{url}/scenes/{first['id']}/split", json={
        "expected_content_revision": 0, "offset": 3, "right_title": "后半",
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    assert left["id"] == first["id"] and right["id"] != left["id"]
    assert left["content"] == original[:3] and right["content"] == original[3:]
    assert right["separator_before"] == ""
    assert (await client.get(url)).json()["content"] == original
    rejected = await client.patch(url, json={"content": "无目标旧整段"})
    assert rejected.status_code == 409 and rejected.json()["code"] == "scene_target_required"
    preview = await client.post(f"{url}/scenes/merge-preview", json={
        "expected_content_revision": 1, "scene_ids": [left["id"], right["id"]],
    })
    assert preview.status_code == 200, preview.text
    info = preview.json()
    merged = await client.post(f"{url}/scenes/merge", json={
        "expected_content_revision": 1, "scene_ids": info["scene_ids"],
        "expected_planning_revision": info["planning_revision"], "signature": info["signature"],
        "metadata_source": "left",
    })
    assert merged.status_code == 200, merged.text
    assert merged.json()["items"][0]["content"] == original
    assert (await client.get(f"{url}/scenes/{right['id']}")).status_code == 404
    async with app.state.sessionmaker() as db:
        retired = await db.get(Scene, right["id"])
        assert retired is not None and retired.active is False
        assert (await db.get(Chapter, chapter["id"])).content == original
    new = await client.post(f"{url}/scenes", json={"expected_content_revision": 2, "content": "新"})
    assert new.status_code == 201 and new.json()["id"] > right["id"]


async def test_pagination_reorder_and_position_resolution(client):
    project, _, url, first = await setup_scene(client, "甲🙂")
    second = await client.post(f"{url}/scenes", json={
        "expected_content_revision": 0, "title": "二", "content": "乙", "summary": "作者摘要",
    })
    assert second.status_code == 201, second.text
    third = await client.post(f"{url}/scenes", json={"expected_content_revision": 1, "content": "丙"})
    assert third.status_code == 201, third.text
    page = (await client.get(f"{url}/scenes?limit=1&offset=1")).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert page["items"][0]["summary"] == "作者摘要"
    assert "content" not in page["items"][0] and "planning" in page["items"][0]
    directory = (await client.get(f"/api/v1/projects/{project['id']}/scenes?limit=2")).json()
    assert directory["content_revision"] is None and directory["total"] == 3
    ordered = [third.json()["id"], first["id"], second.json()["id"]]
    result = await client.post(f"{url}/scenes/reorder", json={
        "expected_content_revision": 2, "scene_ids": ordered,
    })
    assert result.status_code == 200, result.text
    content = (await client.get(url)).json()["content"]
    assert content == "\n\n丙甲🙂\n\n乙"  # 首项分隔也不能悄悄被丢掉。
    fingerprint = hashlib.sha256(content.encode()).hexdigest()
    located = await client.get(f"{url}/scenes/resolve-position", params={
        "fingerprint": fingerprint, "start": 3, "end": 5,
    })
    assert located.status_code == 200 and located.json()["scene_id"] == first["id"]
    cross = (await client.get(f"{url}/scenes/resolve-position", params={
        "fingerprint": fingerprint, "start": 2, "end": len(content),
    })).json()
    assert cross["scene_id"] is None and cross["reason"] == "cross_scene"
    stale = (await client.get(f"{url}/scenes/resolve-position", params={
        "fingerprint": "0" * 64, "start": 0, "end": 1,
    })).json()
    assert stale["reason"] == "stale"
    wrong = await client.post(f"{url}/scenes/reorder", json={
        "expected_content_revision": 3, "scene_ids": [first["id"]],
    })
    assert wrong.status_code == 400
    duplicate = await client.post(f"{url}/scenes/reorder", json={
        "expected_content_revision": 3, "scene_ids": [first["id"], first["id"]],
    })
    assert duplicate.status_code == 422


async def test_scene_reads_are_no_autoflush_and_do_not_select_body(app, client):
    project, chapter, _, _ = await setup_scene(client)
    statements = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower())

    def reject_flush(_session, _context, _instances):
        raise AssertionError("只读场景目录触发了 flush")

    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, chapter["id"])
        row.title = "未提交草稿"
        event.listen(Session, "before_flush", reject_flush)
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe)
        try:
            page = await list_scenes(db, 1, project["id"], chapter["id"])
            assert page.total == 1
        finally:
            event.remove(Session, "before_flush", reject_flush)
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe)
    assert not any("scenes.content" in statement for statement in statements)


async def test_scene_scope_permissions_and_last_scene_guard(client, make_client):
    _, _, url, scene = await setup_scene(client)
    other = make_client()
    await register(other, "another")
    checks = [
        await other.get(f"{url}/scenes"),
        await other.get(f"{url}/scenes/{scene['id']}"),
        await other.post(f"{url}/scenes", json={"expected_content_revision": 0}),
        await other.patch(f"{url}/scenes/{scene['id']}", json={"expected_content_revision": 0, "summary": "x"}),
        await other.post(f"{url}/scenes/reorder", json={"expected_content_revision": 0, "scene_ids": [scene['id']]}),
        await other.delete(f"{url}/scenes/{scene['id']}?expected_content_revision=0"),
    ]
    assert all(response.status_code == 404 for response in checks)
    last = await client.delete(f"{url}/scenes/{scene['id']}?expected_content_revision=0")
    assert last.status_code == 409 and last.json()["code"] == "scene_last_required"


@pytest.mark.parametrize("offset", [0, 2, 3])
async def test_split_rejects_empty_or_outside_parts(client, offset):
    _, _, url, scene = await setup_scene(client, "甲乙")
    response = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": offset,
    })
    assert response.status_code == 422
    assert (await client.get(url)).json()["content"] == "甲乙"


async def test_insert_first_uses_default_separator_without_losing_old_text(client):
    _, _, url, first = await setup_scene(client, "原\n稿")
    created = await client.post(f"{url}/scenes", json={
        "expected_content_revision": 0, "after_scene_id": None, "content": "新稿",
    })
    assert created.status_code == 201, created.text
    assert (await client.get(url)).json()["content"] == "新稿\n\n原\n稿"
    assert (await client.get(f"{url}/scenes/{first['id']}")).json()["content"] == "原\n稿"


async def test_missing_scene_is_reported_without_read_repair(app, client):
    project, _, _, _ = await setup_scene(client)
    async with app.state.sessionmaker() as db:
        broken = Chapter(project_id=project["id"], title="故障种子", content="不得自动修复", chapter_no=2)
        db.add(broken)
        await db.commit()
        broken_id = broken.id
    response = await client.get(f"/api/v1/projects/{project['id']}/chapters/{broken_id}/scenes")
    assert response.status_code == 409 and response.json()["code"] == "scene_invariant_error"
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(Scene).where(Scene.chapter_id == broken_id))) == []


async def test_management_scene_write_keeps_version_and_safe_audit_atomic(app, client, system_admin):
    _, _, url, scene = await setup_scene(client, "甲乙")
    response = await system_admin.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 1,
    })
    assert response.status_code == 200, response.text
    from app.models import AdminAuditLog

    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.action == "chapter.update", AdminAuditLog.result == "success",
        )))
        assert len(rows) == 1 and rows[0].summary["changed_fields"] == ["content"]
        assert rows[0].version_ref["version_id"] > 0
        assert "甲乙" not in str(rows[0].summary)
