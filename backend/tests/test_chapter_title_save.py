"""章标题的条件保存和在客户端重试后仍到达服务端的旧请求。"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import AdminAuditLog
from app.services import auth_service, authorization_service, chapter_service
from tests.helpers import create_project, register


async def _create_chapter(client, chapters_url, title="A"):
    response = await client.post(chapters_url, json={"title": title, "content": "原正文"})
    assert response.status_code == 201, response.text
    chapter = response.json()
    url = f"{chapters_url}/{chapter['id']}"
    response = await client.get(f"{url}/scenes")
    assert response.status_code == 200, response.text
    return chapter, url, response.json()["items"][0]["id"]


async def _setup(client, title="A"):
    await register(client)
    project = await create_project(client)
    return await _create_chapter(client, f"/api/v1/projects/{project['id']}/chapters", title)


def _save_body(chapter, scene_id, title):
    return {
        "title": title,
        "expected_title": chapter["title"],
        "expected_scene_id": scene_id,
        "expected_content_revision": chapter["content_revision"],
    }


async def test_title_routes_require_guards_but_old_non_title_writes_still_work(client):
    chapter, url, scene_id = await _setup(client)
    cleared = await client.patch(f"{url}/editor-title", json=_save_body(chapter, scene_id, ""))
    assert cleared.status_code == 200, cleared.text
    chapter = cleared.json()
    body = _save_body(chapter, scene_id, " 新标题 ")
    for path in (url, f"{url}/editor-title"):
        for key in ("expected_title", "expected_scene_id", "expected_content_revision"):
            response = await client.patch(path, json={k: v for k, v in body.items() if k != key})
            assert response.status_code == 422, response.text
        for key, value in (("expected_title", None), ("expected_scene_id", "1"), ("expected_content_revision", True)):
            response = await client.patch(path, json={**body, key: value})
            assert response.status_code == 422, response.text
    for extra in ({"summary": "不应修改"}, {"status": "done"}, {"title": None}, {"title": "长" * 201}):
        response = await client.patch(f"{url}/editor-title", json={**body, **extra})
        assert response.status_code == 422, response.text

    unchanged = await client.patch(url, json={"title": None, "style_id": None, "status": "done"})
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["title"] == "" and unchanged.json()["status"] == "done"
    edited = await client.patch(f"{url}/editor-title", json=body)
    assert edited.status_code == 200, edited.text
    assert edited.json()["title"] == "新标题"
    assert edited.json()["content_revision"] == chapter["content_revision"] + 1


async def test_current_title_and_revision_are_checked_before_noop(client, app, system_admin):
    chapter, url, scene_id = await _setup(client)
    plan = (await client.get(f"{url}/scenes")).json()["planning_revision"]
    path = f"{url}/editor-title"

    same = await system_admin.patch(path, json=_save_body(chapter, scene_id, " A "))
    assert same.status_code == 200, same.text
    assert same.json()["content_revision"] == chapter["content_revision"]
    assert (await client.get(f"{url}/scenes")).json()["planning_revision"] == plan
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(AdminAuditLog.id)).where(
            AdminAuditLog.action == "chapter.update", AdminAuditLog.target_id == chapter["id"],
        )) == 0

    wrong_title = await system_admin.patch(path, json={**_save_body(chapter, scene_id, "A"), "expected_title": " A "})
    assert wrong_title.status_code == 409, wrong_title.text
    assert wrong_title.json()["code"] == "stale_chapter_title"

    accepted = await system_admin.patch(path, json=_save_body(chapter, scene_id, "B"))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["content"] == "原正文"
    assert accepted.json()["content_revision"] == chapter["content_revision"] + 1
    assert (await client.get(f"{url}/scenes")).json()["planning_revision"] == plan + 1
    for title in ("B", "不同的并发标题"):
        rejected = await system_admin.patch(path, json=_save_body(chapter, scene_id, title))
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "stale_scene_revision"
    assert (await client.get(url)).json()["title"] == "B"
    assert (await client.get(f"{url}/scenes")).json()["planning_revision"] == plan + 1
    assert len((await client.get(f"{url}/versions")).json()) == 1
    async with app.state.sessionmaker() as db:
        succeeded = (await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.action == "chapter.update", AdminAuditLog.target_id == chapter["id"],
            AdminAuditLog.result == "success",
        ))).one()
        assert succeeded.summary["changed_fields"] == ["title"]


async def test_legacy_combined_save_consumes_revision_once_and_stale_body_is_not_written(client):
    chapter, url, scene_id = await _setup(client)
    accepted = await client.patch(url, json={**_save_body(chapter, scene_id, "B"), "content": "同时更新的正文"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["content_revision"] == chapter["content_revision"] + 1
    assert accepted.json()["content"] == "同时更新的正文"
    assert accepted.json()["title"] == "B"
    rejected = await client.patch(url, json={**_save_body(chapter, scene_id, "C"), "content": "旧正文"})
    assert rejected.status_code == 409, rejected.text
    assert (await client.get(url)).json()["content"] == "同时更新的正文"
    assert (await client.get(url)).json()["title"] == "B"


async def test_other_scene_and_retired_scene_cannot_serve_as_anchor(client):
    chapter, url, scene_id = await _setup(client)
    _, _, other_scene_id = await _create_chapter(client, url.rsplit("/", 1)[0])
    rejected = await client.patch(f"{url}/editor-title", json=_save_body(chapter, other_scene_id, "B"))
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "editor_target_changed"
    inserted = await client.post(f"{url}/scenes", json={"expected_content_revision": chapter["content_revision"]})
    assert inserted.status_code == 201, inserted.text
    removed = await client.delete(f"{url}/scenes/{scene_id}", params={
        "expected_content_revision": inserted.json()["content_revision"],
    })
    assert removed.status_code == 200, removed.text
    current = (await client.get(url)).json()
    rejected = await client.patch(f"{url}/editor-title", json=_save_body(current, scene_id, "B"))
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "editor_target_changed"
    assert (await client.get(url)).json()["title"] == "A"


async def test_recreated_chapter_with_same_integer_id_rejects_old_scene_anchor(client):
    chapter, url, scene_id = await _setup(client)
    assert (await client.delete(url)).status_code == 204
    recreated, recreated_url, replacement_scene = await _create_chapter(client, url.rsplit("/", 1)[0])
    assert recreated_url == url and replacement_scene != scene_id
    assert recreated["content_revision"] == chapter["content_revision"]
    rejected = await client.patch(f"{url}/editor-title", json=_save_body(chapter, scene_id, "B"))
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "editor_target_changed"
    assert (await client.get(url)).json()["title"] == "A"


async def test_late_original_title_request_cannot_commit_after_title_returns_to_original(
    client, make_client, monkeypatch,
):
    chapter, url, scene_id = await _setup(client)
    late_client = make_client()
    login = await late_client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
    assert login.status_code == 200, login.text
    path = f"{url}/editor-title"
    body = _save_body(chapter, scene_id, "B")
    reached, release = asyncio.Event(), asyncio.Event()
    original = chapter_service.get_manual_chapter

    async def gate_before_lock(*args, **kwargs):
        if asyncio.current_task() is late:
            reached.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(chapter_service, "get_manual_chapter", gate_before_lock)
    late = asyncio.create_task(late_client.patch(path, json=body))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        retried = await client.patch(path, json=body)
        assert retried.status_code == 200, retried.text
        reversed_ = await client.patch(path, json=_save_body(retried.json(), scene_id, "A"))
        assert reversed_.status_code == 200, reversed_.text
    finally:
        release.set()
        stale = await asyncio.wait_for(late, 3)

    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_scene_revision"
    current = (await client.get(url)).json()
    assert current["title"] == "A" and current["content_revision"] == chapter["content_revision"] + 2


async def test_editor_save_rejects_stale_actor_even_when_new_cookie_can_edit(client, make_client):
    chapter, url, scene_id = await _setup(client)
    original_actor = (await client.get("/api/v1/auth/me")).json()["id"]
    editor = make_client()
    actual_actor = await register(editor, "other_editor")
    project_url = f"/api/v1/projects/{chapter['project_id']}"
    invite = await client.post(f"{project_url}/collaborators", json={"username": "other_editor", "role": "editor"})
    assert invite.status_code == 201, invite.text
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": 1},
    )
    assert accepted.status_code == 200, accepted.text
    scene_url = f"{url}/scenes/{scene_id}"
    doc_url = f"{scene_url}/document"
    current_document = (await editor.get(doc_url)).json()
    metadata = {"expected_content_revision": chapter["content_revision"], "summary": "新摘要"}
    document = {**current_document["document"], "content": "新正文"}
    save = {
        "expected_content_revision": chapter["content_revision"], "document": document,
        "add_section_keys": [], "delete_section_keys": [], "mapped_section_keys": [],
        "add_marker_keys": [], "delete_marker_keys": [], "mapped_marker_keys": [],
    }
    for method, path, body in (
        ("PATCH", f"{url}/editor-title", _save_body(chapter, scene_id, "新标题")),
        ("PATCH", scene_url, metadata),
        ("PUT", doc_url, save),
    ):
        response = await editor.request(method, path, json=body, headers={"X-Editor-Actor-Id": str(original_actor)})
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "editor_actor_changed"

    assert (await client.get(url)).json() == chapter
    assert (await client.get(scene_url)).json()["summary"] == ""
    assert (await client.get(doc_url)).json() == current_document
    success = await editor.patch(scene_url, json=metadata, headers={"X-Editor-Actor-Id": str(actual_actor["id"])})
    assert success.status_code == 200, success.text
    assert success.json()["summary"] == "新摘要"


async def test_editor_save_binds_account_creation_even_if_id_is_reused_between_read_and_lock(
    client, make_client, monkeypatch, system_admin,
):
    chapter, url, scene_id = await _setup(client)
    project_url = f"/api/v1/projects/{chapter['project_id']}"
    old_editor, new_editor = make_client(), make_client()

    async def register_editor(editor, username):
        user = await register(editor, username)
        invite = await client.post(f"{project_url}/collaborators", json={"username": username, "role": "editor"})
        assert invite.status_code == 201, invite.text
        accepted = await editor.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": 1},
        )
        assert accepted.status_code == 200, accepted.text
        return user

    clock = datetime(2026, 11, 17, 12, 0, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr(auth_service, "_now_utc", lambda: clock)
    previous = await register_editor(old_editor, "previous_editor")
    old_stamp = {"X-Editor-Actor-Id": str(previous["id"]), "X-Editor-Actor-Created-At": previous["created_at"]}
    title_url = f"{url}/editor-title"
    title = _save_body(chapter, scene_id, "迟到的标题")
    reached, release = asyncio.Event(), asyncio.Event()
    check = authorization_service.require_authenticated

    async def gate_before_actor_lock(db, actor, *, write=False, targets=()):
        if write and asyncio.current_task() is late:
            reached.set()
            await release.wait()
        return await check(db, actor, write=write, targets=targets)

    monkeypatch.setattr(authorization_service, "require_authenticated", gate_before_actor_lock)
    late = asyncio.create_task(old_editor.patch(title_url, json=title, headers=old_stamp))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        deleted = await system_admin.delete(f"/api/v1/admin/users/{previous['id']}")
        assert deleted.status_code == 204, deleted.text
        clock += timedelta(microseconds=1)
        replacement = await register_editor(new_editor, "replacement_editor")
        assert replacement["id"] == previous["id"]
        scene_url = f"{url}/scenes/{scene_id}"
        doc_url = f"{scene_url}/document"
        current_document = (await new_editor.get(doc_url)).json()
        metadata = {"expected_content_revision": chapter["content_revision"], "summary": "迟到的摘要"}
        save = {
            "expected_content_revision": chapter["content_revision"],
            "document": {**current_document["document"], "content": "迟到的正文"},
            "add_section_keys": [], "delete_section_keys": [], "mapped_section_keys": [],
            "add_marker_keys": [], "delete_marker_keys": [], "mapped_marker_keys": [],
        }
        for method, path, body in (("PATCH", title_url, title), ("PATCH", scene_url, metadata), ("PUT", doc_url, save)):
            rejected = await new_editor.request(method, path, json=body, headers=old_stamp)
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["code"] == "editor_actor_changed"
    finally:
        release.set()
        interrupted = await asyncio.wait_for(late, 5)

    assert interrupted.status_code == 409, interrupted.text
    assert interrupted.json()["code"] == "editor_actor_changed"
    assert (await client.get(url)).json() == chapter
    assert (await client.get(scene_url)).json()["summary"] == ""
    assert (await client.get(doc_url)).json() == current_document
    assert datetime.fromisoformat(previous["created_at"]).microsecond == 123456
    assert datetime.fromisoformat(replacement["created_at"]).microsecond == 123457
    offset_stamp = clock.astimezone(timezone(timedelta(hours=8))).isoformat()
    accepted = await new_editor.patch(title_url, json={**title, "title": "新账号保存"}, headers={
        **old_stamp, "X-Editor-Actor-Created-At": offset_stamp,
    })
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["title"] == "新账号保存"
