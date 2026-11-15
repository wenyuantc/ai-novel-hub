"""隔离 PostgreSQL 多连接验证系列和项目屏障，不能以 SQLite 锁代替。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.ai.providers import OpenAICompatProvider
from app.models import Project, ProjectCollaborator, Series, SeriesCollaborator, SeriesEntryUse
from app.services import ai_dispatch, project_lifecycle, series_service
from app.services.ai_dispatch import VisibilityChangedError, guard_provider
from app.services.setting_ai_context import build_scope
from tests.helpers import create_project, register
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_series import _accept
from tests.test_series import _entry, _join, _new_series, _use

postgres_series_collaboration_apps = _postgres_fixture


async def _setup(owner, guest):
    await register(owner, "series-pg-owner")
    user = await register(guest, "series-pg-editor")
    book = await create_project(owner, title="项目")
    series = await _join(owner, await _new_series(owner), book)
    path = f"/api/v1/series/{series['id']}"
    entry = await _entry(owner, path, series["revision"], "character", name="共享人物", profile="旧基础")
    url = f"{path}/entries/{entry['item']['id']}"
    used = await _use(owner, url, entry["revision"], book["id"])
    series_member = await _accept(owner, guest, path, "series-pg-editor", "series")
    project_member = await _accept(
        owner, guest, f"/api/v1/projects/{book['id']}", "series-pg-editor", "project", "editor",
    )
    return user, book, series, path, entry, url, used, series_member, project_member


async def _assert_locked(maker, model, identity):
    async with maker() as db:
        with pytest.raises(DBAPIError) as caught:
            await db.execute(select(model.id).where(model.id == identity).with_for_update(nowait=True))
        original = caught.value.orig
        assert any(getattr(item, "sqlstate", None) == "55P03"
                   for item in (original, getattr(original, "__cause__", None)))
        await db.rollback()


@pytest.mark.parametrize("scope", ["series", "project"])
async def test_row_barrier_rechecks_revocation_after_wait(postgres_series_collaboration_apps, scope):
    owner_app, guest_app = postgres_series_collaboration_apps
    maker = owner_app.state.sessionmaker
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        user, book, series, path, entry, url, used, _, _ = await _setup(owner, guest)
        pending = None
        try:
            async with maker() as holder:
                if scope == "series":
                    await holder.execute(update(Series).where(Series.id == series["id"]).values(id=Series.id))
                    await holder.execute(update(SeriesCollaborator).where(
                        SeriesCollaborator.series_id == series["id"], SeriesCollaborator.user_id == user["id"],
                    ).values(status="revoked"))
                else:
                    await project_lifecycle.lock_record(holder, book["id"])
                    await holder.execute(update(ProjectCollaborator).where(
                        ProjectCollaborator.project_id == book["id"], ProjectCollaborator.user_id == user["id"],
                    ).values(status="revoked"))
                holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                if scope == "series":
                    pending = asyncio.create_task(guest.patch(url, json={
                        "expected_revision": used["revision"], "data": {"profile": "不可提交"},
                    }))
                else:
                    pending = asyncio.create_task(guest.delete(
                        f"{url}/uses/{book['id']}", params={"expected_revision": used["revision"]},
                    ))
                await asyncio.wait_for(_wait_for_blocker(maker, holder_pid), 6)
                await holder.commit()
            response = await asyncio.wait_for(pending, 6)
            assert response.status_code == (404 if scope == "series" else 409), response.text
            if scope == "project":
                assert response.json()["code"] == "series_book_edit_required"
            async with maker() as db:
                assert await db.get(SeriesEntryUse, (book["id"], entry["item"]["id"])) is not None
            actual = await owner.get(f"/api/v1/projects/{book['id']}/characters/{used['ref']['id']}")
            assert actual.json()["profile"] == "旧基础"
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize("scope", ["series", "project"])
async def test_locked_editor_write_commits_before_revoke(postgres_series_collaboration_apps, monkeypatch, scope):
    owner_app, guest_app = postgres_series_collaboration_apps
    maker = owner_app.state.sessionmaker
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        user, book, series, path, entry, url, used, series_member, project_member = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder_pid = {}
        name = "_project_settings" if scope == "series" else "_remove_projections"
        original = getattr(series_service, name)

        async def gated(db, *args, **kwargs):
            holder_pid["value"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return await original(db, *args, **kwargs)

        monkeypatch.setattr(series_service, name, gated)
        if scope == "series":
            write = asyncio.create_task(guest.patch(url, json={
                "expected_revision": used["revision"], "data": {"profile": "先提交"},
            }))
            revoke_url = f"{path}/collaborators/{user['id']}"
            revision = series_member["revision"]
        else:
            write = asyncio.create_task(guest.delete(
                f"{url}/uses/{book['id']}", params={"expected_revision": used["revision"]},
            ))
            revoke_url = f"/api/v1/projects/{book['id']}/collaborators/{user['id']}"
            revision = project_member["revision"]
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 6)
            await _assert_locked(maker, Series, series["id"])
            await _assert_locked(maker, Project, book["id"])
            revoke = asyncio.create_task(owner.delete(revoke_url, params={"expected_revision": revision}))
            await asyncio.wait_for(_wait_for_blocker(maker, holder_pid["value"]), 6)
            release.set()
            assert (await asyncio.wait_for(write, 6)).status_code == 200
            assert (await asyncio.wait_for(revoke, 6)).status_code == 204
            async with maker() as db:
                use = await db.get(SeriesEntryUse, (book["id"], entry["item"]["id"]))
                assert (use is not None) == (scope == "series")
            if scope == "series":
                assert (await guest.get(path)).status_code == 404
                actual = await owner.get(f"/api/v1/projects/{book['id']}/characters/{used['ref']['id']}")
                assert actual.json()["profile"] == "先提交"
        finally:
            release.set()
            for pending in (write, revoke):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_series_editor_propagation_waits_for_real_send_and_fences_stale_scope(
    postgres_series_collaboration_apps, monkeypatch,
):
    owner_app, guest_app = postgres_series_collaboration_apps
    maker = owner_app.state.sessionmaker
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 5.0)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _, book, _, _, _, entry_url, used, _, _ = await _setup(owner, guest)
        async with maker() as db:
            scope = await build_scope(db, book["id"], explicit=[("character", used["ref"]["id"])])
        async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
            provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), scope.stamp, maker)
            sending = asyncio.create_task(_collect(provider.generate_stream([
                {"role": "user", "content": "发送中的旧设定"},
            ], "test")))
            patch = None
            try:
                await asyncio.wait_for(received.wait(), 6)
                patch = asyncio.create_task(guest.patch(entry_url, json={
                    "expected_revision": used["revision"], "ai_settings": {"ai_mode": "never"},
                }))
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(patch), 0.2)
                headers.set()
                result = await asyncio.wait_for(patch, 6)
                assert result.status_code == 200, result.text
                body.set()
                await asyncio.wait_for(sending, 6)
                with pytest.raises(VisibilityChangedError):
                    await _collect(provider.generate_stream([{"role": "user", "content": "不可再发送"}], "test"))
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                for pending in (sending, patch):
                    if pending is not None and not pending.done():
                        pending.cancel()
                        with suppress(asyncio.CancelledError):
                            await pending


@pytest.mark.parametrize("first", ["use", "delete"])
async def test_bound_entry_cannot_be_deleted_during_adoption(postgres_series_collaboration_apps, monkeypatch, first):
    owner_app, guest_app = postgres_series_collaboration_apps
    maker = owner_app.state.sessionmaker
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        await register(owner, "series-race-owner")
        await register(guest, "series-race-editor")
        book = await create_project(owner)
        series = await _join(owner, await _new_series(owner), book)
        path = f"/api/v1/series/{series['id']}"
        entry = await _entry(owner, path, series["revision"], "character", name="共享人物")
        url = f"{path}/entries/{entry['item']['id']}"
        await _accept(owner, guest, path, "series-race-editor", "series")
        reached, release = asyncio.Event(), asyncio.Event()
        holder_pid = {}
        name = "_new_local" if first == "use" else "_uses"
        original = getattr(series_service, name)

        async def gated(db, *args, **kwargs):
            result = await original(db, *args, **kwargs)
            holder_pid["value"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return result

        monkeypatch.setattr(series_service, name, gated)

        async def use():
            return await owner.post(f"{url}/uses", json={
                "project_id": book["id"], "expected_revision": entry["revision"],
            })

        async def delete():
            return await guest.delete(url, params={"expected_revision": entry["revision"]})

        write = asyncio.create_task(use() if first == "use" else delete())
        pending = None
        try:
            await asyncio.wait_for(reached.wait(), 6)
            await _assert_locked(maker, Series, series["id"])
            pending = asyncio.create_task(delete() if first == "use" else use())
            await asyncio.wait_for(_wait_for_blocker(maker, holder_pid["value"]), 6)
            release.set()
            result, other = await asyncio.wait_for(write, 6), await asyncio.wait_for(pending, 6)
            assert result.status_code == 200, result.text
            assert other.status_code == 409 and other.json()["code"] == "stale_revision", other.text
            current = await owner.get(url)
            if first == "use":
                assert current.status_code == 200 and current.json()["has_uses"] is True
            else:
                assert current.status_code == 404
        finally:
            release.set()
            for task in (write, pending):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
