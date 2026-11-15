"""系列邀请不授予单书权限；响应不能泄露隐藏作品及其本书引用。"""

import pytest
from sqlalchemy import event

from app.core.errors import ConflictError
from app.models import Project, Series, SeriesBook, SeriesEntry, SeriesEntryUse, User
from app.services import series_service
from tests.helpers import create_project, register
from tests.test_series import _entry, _join, _new_series, _use


async def _accept(owner, guest, url, username, scope, role=None):
    body = {"username": username}
    if role is not None:
        body["role"] = role
    response = await owner.post(f"{url}/collaborators", json=body)
    assert response.status_code == 201, response.text
    member = response.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/{scope}/{member['invitation_id']}/accept",
        json={"expected_revision": member["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def test_series_editor_reads_only_authorized_books_and_can_edit_shared_baseline(client, make_client):
    await register(client, "series-owner")
    guest = make_client()
    await register(guest, "series-editor")
    a, b = [await create_project(client, title=title) for title in ("可查看的书", "秘密续作")]
    series = await _new_series(client)
    path = f"/api/v1/series/{series['id']}"
    for book in (a, b):
        series = await _join(client, series, book)
    created = await _entry(client, path, series["revision"], "character", name="共享角色", profile="旧基础")
    entry_path = f"{path}/entries/{created['item']['id']}"
    first = await _use(client, entry_path, created["revision"], a["id"])
    second = await _use(client, entry_path, first["revision"], b["id"])

    invite = await client.post(f"{path}/collaborators", json={"username": "series-editor"})
    assert invite.status_code == 201
    assert (await guest.get(path)).status_code == 404
    assert (await guest.get("/api/v1/series")).json() == []
    await guest.post(
        f"/api/v1/collaboration/invitations/series/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    await _accept(client, guest, f"/api/v1/projects/{a['id']}", "series-editor", "project", "viewer")

    listed = await guest.get("/api/v1/series")
    assert [item["id"] for item in listed.json()] == [series["id"]]
    detail = await guest.get(path)
    assert detail.status_code == 200, detail.text
    assert detail.json()["is_owner"] is False and detail.json()["can_manage_collaborators"] is False
    assert detail.json()["book_count"] == 2
    assert detail.json()["books"] == [{"id": a["id"], "title": "可查看的书", "recycled": False}]
    assert "秘密续作" not in detail.text
    result = await guest.get(entry_path)
    assert result.status_code == 200, result.text
    assert result.json()["has_uses"] is True
    assert result.json()["uses"] == [{"project_id": a["id"], "ref": first["ref"]}]
    listed_entry = (await guest.get(f"{path}/entries")).json()["items"][0]
    assert listed_entry["has_uses"] is True and listed_entry["uses"] == result.json()["uses"]
    assert (await guest.get(f"/api/v1/projects/{b['id']}")).status_code == 404

    patched = await guest.patch(entry_path, json={
        "expected_revision": second["revision"], "data": {"profile": "共享更新"},
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["item"]["uses"] == result.json()["uses"]
    assert "秘密续作" not in patched.text
    for book, used in ((a, first), (b, second)):
        actual = await client.get(f"/api/v1/projects/{book['id']}/characters/{used['ref']['id']}")
        assert actual.json()["profile"] == "共享更新"

    blocked = await guest.delete(entry_path, params={"expected_revision": patched.json()["revision"]})
    assert blocked.status_code == 409 and blocked.json()["code"] == "series_entry_bound"
    assert "秘密续作" not in blocked.text and "blockers" not in blocked.json()
    free = await _entry(guest, path, patched.json()["revision"], "character", name="未采用")
    assert free["item"]["has_uses"] is False
    removed = await guest.delete(f"{path}/entries/{free['item']['id']}", params={"expected_revision": free["revision"]})
    assert removed.status_code == 200, removed.text


async def test_series_editor_requires_book_editor_for_ownership_changes(client, make_client):
    await register(client, "owner-f")
    guest = make_client()
    guest_user = await register(guest, "guest-f")
    a, b = [await create_project(client, title=title) for title in ("甲", "隐藏书")]
    series = await _new_series(client)
    path = f"/api/v1/series/{series['id']}"
    for book in (a, b):
        series = await _join(client, series, book)
    entry = await _entry(client, path, series["revision"], "character", name="角色")
    url = f"{path}/entries/{entry['item']['id']}"
    first = await _use(client, url, entry["revision"], a["id"])
    second = await _use(client, url, first["revision"], b["id"])
    await _accept(client, guest, path, "guest-f", "series")
    member = await _accept(client, guest, f"/api/v1/projects/{a['id']}", "guest-f", "project", "viewer")

    rename = await guest.patch(path, json={"name": "无权改名", "expected_revision": second["revision"]})
    assert rename.status_code == 404
    leave = await guest.delete(f"{path}/books/{a['id']}", params={"expected_revision": second["revision"]})
    assert leave.status_code == 404
    denied = await guest.delete(f"{url}/uses/{a['id']}", params={"expected_revision": second["revision"]})
    assert denied.status_code in (403, 404, 409)
    upgrade = await client.patch(f"/api/v1/projects/{a['id']}/collaborators/{guest_user['id']}", json={
        "role": "editor", "expected_revision": member["revision"],
    })
    assert upgrade.status_code == 200

    # 移动涉及隐藏书，不能仅因为有目标书权限就删除它的投影。
    moved = await guest.post(f"{url}/move-to-book", json={
        "project_id": a["id"], "expected_revision": second["revision"],
    })
    assert moved.status_code == 409 and "隐藏书" not in moved.text
    preview = await guest.get(f"{path}/impact", params={
        "action": "move", "project_id": a["id"], "entry_id": entry["item"]["id"],
    })
    assert preview.status_code == 409 and "隐藏书" not in preview.text
    unuse = await guest.delete(f"{url}/uses/{a['id']}", params={"expected_revision": second["revision"]})
    assert unuse.status_code == 200, unuse.text
    assert unuse.json()["item"]["has_uses"] is True and unuse.json()["item"]["uses"] == []

    # 已获两本书编辑权限后，归属操作才可影响两本书。
    await _accept(client, guest, f"/api/v1/projects/{b['id']}", "guest-f", "project", "editor")
    adopted = await _use(guest, url, unuse.json()["revision"], a["id"])
    preview = await guest.get(f"{path}/impact", params={
        "action": "move", "project_id": a["id"], "entry_id": entry["item"]["id"],
    })
    assert preview.status_code == 200 and len(preview.json()["affected_books"]) == 2
    moved = await guest.post(f"{url}/move-to-book", json={
        "project_id": a["id"], "expected_revision": adopted["revision"],
    })
    assert moved.status_code == 200, moved.text
    assert moved.json()["ref"] == adopted["ref"]


async def test_recycled_use_and_admin_cannot_bypass_series_or_book_rights(app, client, make_client):
    await register(client, "f-owner")
    guest = make_client()
    user = await register(guest, "f-admin")
    book = await create_project(client, title="已回收的书")
    series = await _join(client, await _new_series(client), book)
    path = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, path, series["revision"], "character", name="关联")
    url = f"{path}/entries/{entry['item']['id']}"
    used = await _use(client, url, entry["revision"], book["id"])
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        recycled = await db.get(Project, book["id"])
        recycled.deleted_at = recycled.created_at
        await db.commit()
    assert (await guest.get(path)).status_code == 404
    member = await _accept(client, guest, path, "f-admin", "series")
    detail = (await guest.get(path)).json()
    assert detail["book_count"] == 1 and detail["books"] == []
    entry_out = (await guest.get(url)).json()
    assert entry_out["has_uses"] is True and entry_out["uses"] == []
    deleted = await guest.delete(url, params={"expected_revision": used["revision"]})
    assert deleted.status_code == 409 and deleted.json()["code"] == "series_entry_bound"
    unuse = await guest.delete(f"{url}/uses/{book['id']}", params={"expected_revision": used["revision"]})
    assert unuse.status_code == 409 and unuse.json()["code"] == "series_book_edit_required"
    revoked = await client.delete(
        f"{path}/collaborators/{user['id']}", params={"expected_revision": member["revision"]},
    )
    assert revoked.status_code == 204
    assert (await guest.get(path)).status_code == 404
    assert (await guest.get("/api/v1/series")).json() == []


async def test_series_admin_still_needs_book_editor_for_promote_and_unuse(app, client, make_client):
    await register(client, "f-admin-owner")
    guest = make_client()
    user = await register(guest, "f-admin-editor")
    book = await create_project(client, title="管理范围内作品")
    book_url = f"/api/v1/projects/{book['id']}"
    character = (await client.post(f"{book_url}/characters", json={"name": "待提升角色", "profile": "基础"})).json()
    series = await _join(client, await _new_series(client), book)
    path = f"/api/v1/series/{series['id']}"
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    await _accept(client, guest, path, "f-admin-editor", "series")
    detail = await guest.get(path)
    assert detail.status_code == 200 and detail.json()["books"][0]["id"] == book["id"]
    params = {"action": "promote", "project_id": book["id"], "kind": "character", "entry_id": character["id"]}
    preview = await guest.get(f"{path}/impact", params=params)
    assert preview.status_code == 409 and preview.json()["code"] == "series_book_edit_required"
    member = await _accept(client, guest, book_url, "f-admin-editor", "project", "viewer")
    assert (await guest.get(f"{path}/impact", params=params)).status_code == 409
    changed = await client.patch(f"{book_url}/collaborators/{user['id']}", json={
        "role": "editor", "expected_revision": member["revision"],
    })
    assert changed.status_code == 200
    preview = await guest.get(f"{path}/impact", params=params)
    assert preview.status_code == 200, preview.text
    promoted = await guest.post(f"{path}/entries/promote", json={
        "expected_revision": series["revision"], "project_id": book["id"],
        "ref": {"kind": "character", "id": character["id"]}, "source_stamp": preview.json()["source_stamp"],
    })
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["ref"]["id"] == character["id"]
    assert promoted.json()["item"]["has_uses"] is True
    unuse = await guest.delete(
        f"{path}/entries/{promoted.json()['item']['id']}/uses/{book['id']}",
        params={"expected_revision": promoted.json()["revision"]},
    )
    assert unuse.status_code == 200, unuse.text


async def test_hidden_book_propagation_conflict_does_not_disclose_dependency(app, client, make_client):
    await register(client, "f-conflict-owner")
    guest = make_client()
    await register(guest, "f-conflict-editor")
    book = await create_project(client, title="秘密作品")
    series = await _join(client, await _new_series(client), book)
    path = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, path, series["revision"], "world", title="白塔", category="place", content="旧基础")
    url = f"{path}/entries/{entry['item']['id']}"
    used = await _use(client, url, entry["revision"], book["id"])
    book_url = f"/api/v1/projects/{book['id']}"
    definition = await client.post(f"{book_url}/setting-field-definitions", json={
        "expected_fields_revision": 0, "label": "秘密字段", "field_type": "line", "applicable_types": ["world.place"],
    })
    assert definition.status_code == 201, definition.text
    fields_url = f"{book_url}/setting-entries/world/{used['ref']['id']}/fields"
    fields = (await client.get(fields_url)).json()
    changed = await client.patch(f"{fields_url}/{definition.json()['item']['id']}/baseline", json={
        "expected_fields_revision": fields["fields_revision"],
        "expected_timeline_revision": fields["timeline_revision"],
        "field_type": "line", "value": "秘密值",
    })
    assert changed.status_code == 200, changed.text
    await _accept(client, guest, path, "f-conflict-editor", "series")
    conflict = await guest.patch(url, json={"expected_revision": used["revision"], "data": {"category": "item"}})
    assert conflict.status_code == 409 and conflict.json()["code"] == "series_projection_conflict"
    assert "秘密作品" not in conflict.text and "秘密字段" not in conflict.text and "秘密值" not in conflict.text
    assert (await client.get(url)).json()["data"]["category"] == "place"
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, book["id"])
        project.deletion_started_at, project.deletion_token = project.created_at, "test-deletion-intent"
        await db.commit()
    deleting = await guest.patch(url, json={"expected_revision": used["revision"], "data": {"category": "item"}})
    assert deleting.status_code == 409 and deleting.json()["code"] == "series_projection_conflict"
    assert "秘密作品" not in deleting.text and "删除" not in deleting.text


async def test_series_project_refresh_preserves_pending_fields_and_rejects_pending_owner(app, client):
    owner = await register(client, "f-refresh-owner")
    book = await create_project(client, title="数据库标题")
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, book["id"])
        project.title = "调用方暂存标题"
        result = await series_service._projects(db, owner["id"], [book["id"]])
        assert result[book["id"]].title == "调用方暂存标题"
        assert project in db.dirty
        await db.rollback()
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, book["id"])
        project.user_id = 999_999
        with pytest.raises(ConflictError) as caught:
            await series_service._projects(db, owner["id"], [book["id"]])
        assert caught.value.extra["code"] == "series_project_changed"
        await db.rollback()
        assert (await db.get(Project, book["id"])).title == "数据库标题"


async def test_series_read_and_impact_do_not_flush_caller_pending_project(app, client):
    owner = await register(client, "f-read-pending")
    book = await create_project(client, title="已保存标题")
    series = await _join(client, await _new_series(client), book)
    path = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, path, series["revision"], "character", name="角色")
    await _use(client, f"{path}/entries/{entry['item']['id']}", entry["revision"], book["id"])
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, book["id"])
        project.title = "调用方尚未提交"
        await series_service.list_series(db, owner["id"])
        await series_service.detail(db, owner["id"], series["id"])
        await series_service.read_entry(db, owner["id"], series["id"], entry["item"]["id"])
        await series_service.list_entries(db, owner["id"], series["id"], kind=None, q=None, page=1, page_size=30)
        await series_service.impact(db, owner["id"], series["id"], action="unuse", pid=book["id"],
                                    kind=None, eid=entry["item"]["id"])
        assert project.title == "调用方尚未提交" and project in db.dirty
        async with app.state.sessionmaker() as other:
            assert (await other.get(Project, book["id"])).title == "已保存标题"
        await db.rollback()


async def test_series_reads_preserve_pending_values_and_reject_pending_identity(app, client, make_client):
    owner = await register(client, "f-series-pending-owner")
    guest = make_client()
    guest_user = await register(guest, "f-series-pending-editor")
    series = await _new_series(client)
    path = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, path, 0, "character", name="已保存角色")
    await _accept(client, guest, path, "f-series-pending-editor", "series")
    eid = entry["item"]["id"]

    async with app.state.sessionmaker() as db:
        cached_series = await db.get(Series, series["id"])
        cached_entry = await db.get(SeriesEntry, eid)
        cached_series.name = "暂存系列名称"
        cached_entry.data = cached_entry.data | {"profile": "暂存简介"}
        result = await series_service.read_entry(db, owner["id"], series["id"], eid)
        assert result.data.profile == "暂存简介"
        assert cached_series.name == "暂存系列名称" and cached_series in db.dirty
        assert cached_entry.data["profile"] == "暂存简介" and cached_entry in db.dirty
        await series_service.list_series(db, owner["id"])
        assert cached_series.name == "暂存系列名称" and cached_series in db.dirty
        async with app.state.sessionmaker() as other:
            assert (await other.get(Series, series["id"])).name == series["name"]
            assert (await other.get(SeriesEntry, eid)).data["profile"] == ""
        await db.rollback()

    for field, value in (("id", 999_900), ("owner_id", guest_user["id"]), ("revision", 999_900)):
        async with app.state.sessionmaker() as db:
            cached = await db.get(Series, series["id"])
            setattr(cached, field, value)
            with pytest.raises(ConflictError):
                await series_service.detail(db, owner["id"], series["id"])
            assert getattr(cached, field) == value and cached in db.dirty
            await db.rollback()
    async with app.state.sessionmaker() as db:
        cached = await db.get(SeriesEntry, eid)
        cached.series_id = 999_900
        with pytest.raises(ConflictError):
            await series_service.read_entry(db, owner["id"], series["id"], eid)
        assert cached.series_id == 999_900 and cached in db.dirty
        await db.rollback()
    async with app.state.sessionmaker() as db:
        cached = await db.get(SeriesEntry, eid)
        cached.id = 999_900
        with pytest.raises(ConflictError):
            await series_service.list_entries(db, owner["id"], series["id"], kind=None, q=None, page=1, page_size=30)
        assert cached.id == 999_900 and cached in db.dirty
        await db.rollback()
    async with app.state.sessionmaker() as db:
        cached = await db.get(Series, series["id"])
        cached.name = "无权暂存系列名称"
        with pytest.raises(ConflictError):
            await series_service.read_entry(db, guest_user["id"], series["id"], eid)
        assert cached.name == "无权暂存系列名称" and cached in db.dirty
        await db.rollback()


async def test_series_reads_reject_pending_membership_and_binding_identity(app, client):
    owner = await register(client, "f-binding-pending-owner")
    book = await create_project(client)
    series = await _join(client, await _new_series(client), book)
    path = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, path, series["revision"], "character", name="角色")
    await _use(client, f"{path}/entries/{entry['item']['id']}", entry["revision"], book["id"])
    eid = entry["item"]["id"]
    for field in ("project_id", "series_entry_id", "series_id", "kind", "character_id"):
        async with app.state.sessionmaker() as db:
            cached = await db.get(SeriesEntryUse, (book["id"], eid))
            pending = "world" if field == "kind" else 999_900
            setattr(cached, field, pending)
            with pytest.raises(ConflictError):
                await series_service.read_entry(db, owner["id"], series["id"], eid)
            assert getattr(cached, field) == pending and cached in db.dirty
            await db.rollback()
    for field in ("project_id", "series_id", "owner_id"):
        async with app.state.sessionmaker() as db:
            cached = await db.get(SeriesBook, book["id"])
            setattr(cached, field, 999_900)
            with pytest.raises(ConflictError):
                await series_service.impact(db, owner["id"], series["id"], action="unuse", pid=book["id"],
                                            kind=None, eid=eid)
            assert getattr(cached, field) == 999_900 and cached in db.dirty
            await db.rollback()


async def test_series_entry_page_batches_hidden_use_projection(app, client, make_client):
    await register(client, "f-batch-owner")
    guest = make_client()
    await register(guest, "f-batch-editor")
    book = await create_project(client, title="隐藏书")
    series = await _join(client, await _new_series(client), book)
    path = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, path, series["revision"], "character", name="甲")
    used = await _use(client, f"{path}/entries/{entry['item']['id']}", entry["revision"], book["id"])
    await _accept(client, guest, path, "f-batch-editor", "series")

    async def count_page_queries():
        queries = []

        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe)
        try:
            result = await guest.get(f"{path}/entries")
            assert result.status_code == 200, result.text
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe)
        return len(queries), result.json()["items"]

    first_count, first_items = await count_page_queries()
    assert len(first_items) == 1 and first_items[0]["has_uses"] and first_items[0]["uses"] == []
    revision = used["revision"]
    for i in range(12):
        created = await _entry(client, path, revision, "character", name=f"未绑定{i}")
        revision = created["revision"]
    count, items = await count_page_queries()
    assert len(items) == 13
    assert sum(item["has_uses"] for item in items) == 1
    assert count == first_count
