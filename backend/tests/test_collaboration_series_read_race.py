"""系列读取期间单书权限变化不能泄露已失权的作品及书内引用。"""

import asyncio
from contextlib import suppress

import pytest
from sqlalchemy import event, select

from app.models import Project, User
from app.services import project_access, project_service, series_read, series_service
from tests.helpers import create_project, register
from tests.test_collaboration_series import _accept
from tests.test_series import _entry, _join, _new_series, _use


@pytest.mark.parametrize("view", ["detail", "entry", "entries", "impact"])
async def test_book_revocation_during_series_projection_is_rechecked(client, make_client, monkeypatch, view):
    await register(client, f"series-race-owner-{view}")
    guest = make_client()
    user = await register(guest, f"series-race-guest-{view}")
    book = await create_project(client, title="秘密书名")
    series = await _join(client, await _new_series(client), book)
    base = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, base, series["revision"], "character", name="共享人物")
    entry_url = f"{base}/entries/{entry['item']['id']}"
    await _use(client, entry_url, entry["revision"], book["id"])
    await _accept(client, guest, base, f"series-race-guest-{view}", "series")
    member = await _accept(
        client, guest, f"/api/v1/projects/{book['id']}", f"series-race-guest-{view}", "project", "editor",
    )

    reached, release = asyncio.Event(), asyncio.Event()
    name = "_blockers" if view == "impact" else "_visible_project_ids"
    original = getattr(series_service, name)
    gated_once = False

    async def gated(*args, **kwargs):
        nonlocal gated_once
        result = await original(*args, **kwargs)
        if not gated_once:
            gated_once = True
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(series_service, name, gated)
    if view == "detail":
        request = guest.get(base)
    elif view == "entry":
        request = guest.get(entry_url)
    elif view == "entries":
        request = guest.get(f"{base}/entries")
    else:
        request = guest.get(f"{base}/impact", params={
            "action": "unuse", "project_id": book["id"], "entry_id": entry["item"]["id"],
        })
    pending = asyncio.create_task(request)
    try:
        await asyncio.wait_for(reached.wait(), 5)
        revoked = await client.delete(
            f"/api/v1/projects/{book['id']}/collaborators/{user['id']}",
            params={"expected_revision": member["revision"]},
        )
        assert revoked.status_code == 204, revoked.text
        release.set()
        response = await asyncio.wait_for(pending, 5)
        if view == "impact":
            assert response.status_code == 409 and response.json()["code"] == "series_book_edit_required"
        else:
            assert response.status_code == 200, response.text
            if view == "detail":
                assert response.json()["book_count"] == 1 and response.json()["books"] == []
            else:
                item = response.json() if view == "entry" else response.json()["items"][0]
                assert item["has_uses"] is True and item["uses"] == []
            assert "秘密书名" not in response.text
            assert f'"project_id":{book["id"]}' not in response.text.replace(" ", "")
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


@pytest.mark.parametrize("scope", ["series", "project"])
async def test_book_setting_rechecks_project_and_series_authorization(client, make_client, monkeypatch, scope):
    await register(client, f"setting-race-owner-{scope}")
    guest = make_client()
    user = await register(guest, f"setting-race-guest-{scope}")
    book = await create_project(client, title="秘密书名")
    series = await _join(client, await _new_series(client), book)
    base = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, base, series["revision"], "character", name="共享人物")
    await _use(client, f"{base}/entries/{entry['item']['id']}", entry["revision"], book["id"])
    series_member = await _accept(client, guest, base, f"setting-race-guest-{scope}", "series")
    book_url = f"/api/v1/projects/{book['id']}"
    project_member = await _accept(client, guest, book_url, f"setting-race-guest-{scope}", "project", "viewer")

    reached, release = asyncio.Event(), asyncio.Event()
    original = series_read.visible_series_ids

    async def gated(*args, **kwargs):
        result = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(series_read, "visible_series_ids", gated)
    pending = asyncio.create_task(guest.get(f"{book_url}/characters"))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        parent = base if scope == "series" else book_url
        member = series_member if scope == "series" else project_member
        revoked = await client.delete(
            f"{parent}/collaborators/{user['id']}", params={"expected_revision": member["revision"]},
        )
        assert revoked.status_code == 204, revoked.text
        release.set()
        response = await asyncio.wait_for(pending, 5)
        if scope == "series":
            assert response.status_code == 200
            assert response.json()[0]["origin"] == {"scope": "book", "series_id": None, "series_entry_id": None}
        else:
            assert response.status_code == 404
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def test_bookshelf_rechecks_series_membership_after_stats_query(client, make_client, monkeypatch):
    await register(client, "bookshelf-race-owner")
    guest = make_client()
    user = await register(guest, "bookshelf-race-guest")
    book = await create_project(client)
    series = await _join(client, await _new_series(client), book)
    base = f"/api/v1/series/{series['id']}"
    member = await _accept(client, guest, base, "bookshelf-race-guest", "series")
    await _accept(client, guest, f"/api/v1/projects/{book['id']}", "bookshelf-race-guest", "project", "viewer")
    reached, release = asyncio.Event(), asyncio.Event()
    original = project_service._last_edited

    async def gated(*args, **kwargs):
        result = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(project_service, "_last_edited", gated)
    pending = asyncio.create_task(guest.get("/api/v1/projects"))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        revoked = await client.delete(f"{base}/collaborators/{user['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204, revoked.text
        release.set()
        result = await asyncio.wait_for(pending, 5)
        assert result.status_code == 200 and result.json()[0]["series"] is None
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


@pytest.mark.parametrize("view", ["shelf", "detail"])
@pytest.mark.parametrize("change", ["revoke", "downgrade", "reinvite"])
async def test_project_projection_rechecks_grant_after_await(client, make_client, monkeypatch, view, change):
    await register(client, f"p-owner-{view}-{change}")
    guest = make_client()
    username = f"p-guest-{view}-{change}"
    user = await register(guest, username)
    book = await create_project(client, title="不可返回的旧作品")
    book_url = f"/api/v1/projects/{book['id']}"
    member = await _accept(client, guest, book_url, username, "project", "editor")
    reached, release = asyncio.Event(), asyncio.Event()

    if view == "shelf":
        original = project_service._last_edited

        async def gated(*args, **kwargs):
            result = await original(*args, **kwargs)
            reached.set()
            await release.wait()
            return result

        monkeypatch.setattr(project_service, "_last_edited", gated)
    else:
        original = project_access.resolve_access
        calls = 0

        async def gated(*args, **kwargs):
            nonlocal calls
            result = await original(*args, **kwargs)
            calls += 1
            if calls == 2:
                reached.set()
                await release.wait()
            return result

        monkeypatch.setattr(project_access, "resolve_access", gated)

    pending = asyncio.create_task(guest.get("/api/v1/projects" if view == "shelf" else book_url))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        member_url = f"{book_url}/collaborators/{user['id']}"
        if change == "downgrade":
            changed = await client.patch(member_url, json={"role": "viewer", "expected_revision": member["revision"]})
            assert changed.status_code == 200, changed.text
        else:
            revoked = await client.delete(member_url, params={"expected_revision": member["revision"]})
            assert revoked.status_code == 204, revoked.text
            if change == "reinvite":
                await _accept(client, guest, book_url, username, "project", "editor")
        release.set()
        response = await asyncio.wait_for(pending, 5)
        if view == "shelf":
            assert response.status_code == 200 and response.json() == []
        elif change == "revoke":
            assert response.status_code == 404
        else:
            assert response.status_code == 409 and response.json()["code"] == "stale_project_access"
        assert "不可返回的旧作品" not in response.text
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def test_project_projection_does_not_flush_or_discard_pending_fields(app, client):
    owner = await register(client, "project-pending-owner")
    book = await create_project(client, title="已保存标题")
    writes = []

    def observe(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    async with app.state.sessionmaker() as db:
        project = await db.get(Project, book["id"])
        project.title = "尚未保存的标题"
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", observe)
        try:
            detail = await project_service.project_out(db, project, actor_user_id=owner["id"])
            shelf = await project_service.list_projects(db, owner["id"])
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", observe)
        assert detail.title == shelf[0].title == "尚未保存的标题"
        assert project.title == "尚未保存的标题" and project in db.dirty and writes == []
        async with app.state.sessionmaker() as proof:
            saved = await proof.scalar(select(Project.title).where(Project.id == book["id"]))
        assert saved == "已保存标题"
        await db.rollback()


async def test_project_projection_retains_independent_admin_access_after_revoke(app, client, make_client, monkeypatch):
    await register(client, "admin-projection-owner")
    guest = make_client()
    user = await register(guest, "admin-projection-guest")
    book = await create_project(client)
    book_url = f"/api/v1/projects/{book['id']}"
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    member = await _accept(client, guest, book_url, "admin-projection-guest", "project", "viewer")
    reached, release = asyncio.Event(), asyncio.Event()
    original = project_access.resolve_access
    calls = 0

    async def gated(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(project_access, "resolve_access", gated)
    pending = asyncio.create_task(guest.get(book_url))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        revoked = await client.delete(f"{book_url}/collaborators/{user['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204, revoked.text
        release.set()
        old = await asyncio.wait_for(pending, 5)
        assert old.status_code == 409 and old.json()["code"] == "stale_project_access"
        current = await guest.get(book_url)
        assert current.status_code == 200 and current.json()["access"]["role"] == "admin"
        assert (await guest.get("/api/v1/projects")).json() == []
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def test_series_list_checks_token_version_in_final_query(app, client, make_client, monkeypatch):
    await register(client, "series-token-owner")
    guest = make_client()
    user = await register(guest, "series-token-guest")
    series = await _new_series(client)
    await _accept(client, guest, f"/api/v1/series/{series['id']}", "series-token-guest", "series")
    reached, release = asyncio.Event(), asyncio.Event()
    original = series_service.authz.require_authenticated

    async def gated(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return snapshot

    monkeypatch.setattr(series_service.authz, "require_authenticated", gated)
    pending = asyncio.create_task(guest.get("/api/v1/series"))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with app.state.sessionmaker() as db:
            actor = await db.get(User, user["id"])
            actor.token_version += 1
            await db.commit()
        release.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == 401 and "系列" not in response.text
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
