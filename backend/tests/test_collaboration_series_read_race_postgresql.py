"""在隔离 PostgreSQL 以两个应用和实际撤权验证系列最终投影。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import update

from app.models import User
from app.services import project_access, project_service, series_read, series_service
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_series import _accept
from tests.test_collaboration_series_postgresql import _setup

postgres_series_collaboration_apps = _postgres_fixture


@pytest.mark.parametrize("view", ["detail", "entry", "entries", "impact", "book-series", "book-project"])
async def test_series_projection_after_committed_revocation(postgres_series_collaboration_apps, monkeypatch, view):
    owner_app, guest_app = postgres_series_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        user, book, _, base, entry, entry_url, _, series_member, project_member = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        target = series_read if view.startswith("book-") else series_service
        name = "visible_series_ids" if target is series_read else (
            "_blockers" if view == "impact" else "_visible_project_ids"
        )
        original = getattr(target, name)
        first = True

        async def gated(*args, **kwargs):
            nonlocal first
            result = await original(*args, **kwargs)
            if first:
                first = False
                reached.set()
                await release.wait()
            return result

        monkeypatch.setattr(target, name, gated)
        book_url = f"/api/v1/projects/{book['id']}"
        if view == "detail":
            request = guest.get(base)
        elif view == "entry":
            request = guest.get(entry_url)
        elif view == "entries":
            request = guest.get(f"{base}/entries")
        elif view == "impact":
            request = guest.get(f"{base}/impact", params={
                "action": "unuse", "project_id": book["id"], "entry_id": entry["item"]["id"],
            })
        else:
            request = guest.get(f"{book_url}/characters")
        pending = asyncio.create_task(request)
        try:
            await asyncio.wait_for(reached.wait(), 10)
            parent = base if view == "book-series" else book_url
            member = series_member if view == "book-series" else project_member
            revoked = await owner.delete(
                f"{parent}/collaborators/{user['id']}", params={"expected_revision": member["revision"]},
            )
            assert revoked.status_code == 204, revoked.text
            release.set()
            response = await asyncio.wait_for(pending, 10)
            if view == "impact":
                assert response.status_code == 409 and response.json()["code"] == "series_book_edit_required"
            elif view == "book-project":
                assert response.status_code == 404
            elif view == "book-series":
                assert response.status_code == 200
                assert response.json()[0]["origin"] == {"scope": "book", "series_id": None, "series_entry_id": None}
            elif view == "detail":
                assert response.status_code == 200 and response.json()["books"] == []
                assert response.json()["book_count"] == 1
            else:
                assert response.status_code == 200
                item = response.json() if view == "entry" else response.json()["items"][0]
                assert item["has_uses"] is True and item["uses"] == []
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize("view", ["shelf", "detail"])
@pytest.mark.parametrize("change", ["revoke", "downgrade", "reinvite"])
async def test_project_projection_after_committed_grant_change(
    postgres_series_collaboration_apps, monkeypatch, view, change,
):
    owner_app, guest_app = postgres_series_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        user, book, _, _, _, _, _, _, member = await _setup(owner, guest)
        book_url = f"/api/v1/projects/{book['id']}"
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
            await asyncio.wait_for(reached.wait(), 10)
            member_url = f"{book_url}/collaborators/{user['id']}"
            if change == "downgrade":
                response = await owner.patch(member_url, json={
                    "role": "viewer", "expected_revision": member["revision"],
                })
                assert response.status_code == 200, response.text
            else:
                response = await owner.delete(member_url, params={"expected_revision": member["revision"]})
                assert response.status_code == 204, response.text
                if change == "reinvite":
                    await _accept(owner, guest, book_url, "series-pg-editor", "project", "editor")
            release.set()
            response = await asyncio.wait_for(pending, 10)
            if view == "shelf":
                assert response.status_code == 200 and response.json() == []
            elif change == "revoke":
                assert response.status_code == 404
            else:
                assert response.status_code == 409 and response.json()["code"] == "stale_project_access"
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_bookshelf_projection_after_committed_series_revocation(postgres_series_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_series_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        user, _, _, base, _, _, _, member, _ = await _setup(owner, guest)
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
            await asyncio.wait_for(reached.wait(), 10)
            revoked = await owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert revoked.status_code == 204, revoked.text
            release.set()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 200 and response.json()[0]["series"] is None
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_series_list_after_committed_token_revoke(postgres_series_collaboration_apps, monkeypatch):
    owner_app, guest_app = postgres_series_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        user, *_ = await _setup(owner, guest)
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
            await asyncio.wait_for(reached.wait(), 10)
            async with owner_app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
                await db.commit()
            release.set()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == 401 and "系列" not in response.text
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
