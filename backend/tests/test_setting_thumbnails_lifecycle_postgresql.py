"""PostgreSQL 图片写入与系列归属及作品永久删除的实际事务顺序。"""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select, text

from app import main
from app.models import Project, SeriesEntryUse, User
from app.models.setting_images import SettingImageResource, SettingThumbnailBinding
from app.services import authorization_service, series_service, setting_image_service
from tests.helpers import register
from tests.test_series_postgresql import _wait_for_blocker
from tests.test_series_postgresql import postgres_series_app as postgres_series_app
from tests.test_series_transfer import _join_books
from tests.test_setting_thumbnails import image_data
from tests.test_setting_thumbnails_postgresql import _create, _stop, _upload


@pytest.mark.parametrize("action", ["move", "unuse"])
@pytest.mark.parametrize("winner", ["upload", "transition"])
async def test_series_upload_and_transition_serialize(postgres_series_app, monkeypatch, fake_llm, action, winner):
    app = postgres_series_app
    other_app = main.create_app(app.state.settings, provider_factory=fake_llm.factory, start_task_engine=False)
    await authorization_service.initialize(other_app.state.sessionmaker)
    await other_app.state.system_settings.load()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as other,
    ):
        await register(client, "thumbnail-transition-owner")
        other.cookies.update(client.cookies)
        book, _, char_id, book_image, _ = await _create(client)
        route, series = await _join_books(client, book)
        impact = await client.get(f"{route}/impact", params={
            "action": "promote", "project_id": book["id"], "kind": "character", "entry_id": char_id,
        })
        promoted = await client.post(f"{route}/entries/promote", json={
            "project_id": book["id"], "ref": {"kind": "character", "id": char_id},
            "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"],
        })
        assert promoted.status_code == 201, promoted.text
        entry = promoted.json()["item"]
        entry_id, revision = entry["id"], promoted.json()["revision"]
        version = entry["thumbnail"]["version"]
        shared_image = f"{route}/entries/{entry_id}/thumbnail"
        initial = await client.get(shared_image)
        assert initial.status_code == 200

        async def transition(actor, current_revision):
            if action == "move":
                return await actor.post(f"{route}/entries/{entry_id}/move-to-book", json={
                    "project_id": book["id"], "expected_revision": current_revision,
                })
            return await actor.delete(f"{route}/entries/{entry_id}/uses/{book['id']}",
                                      params={"expected_revision": current_revision})

        async def upload(actor):
            return await actor.put(shared_image, files={"file": ("new.png", image_data("blue"), "image/png")}, data={
                "expected_version": version, "expected_revision": str(revision),
            })

        ready, release = asyncio.Event(), asyncio.Event()
        holder = {}
        if winner == "upload":
            original = setting_image_service.set_series_image_without_commit

            async def held_upload(db, *args, **kwargs):
                result = await original(db, *args, **kwargs)
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                ready.set()
                await release.wait()
                return result

            monkeypatch.setattr(setting_image_service, "set_series_image_without_commit", held_upload)
        else:
            original = series_service._remove_projections

            async def held_transition(db, *args, **kwargs):
                await original(db, *args, **kwargs)
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                ready.set()
                await release.wait()

            monkeypatch.setattr(series_service, "_remove_projections", held_transition)

        first = asyncio.create_task(upload(client) if winner == "upload" else transition(client, revision))
        second = None
        try:
            await asyncio.wait_for(ready.wait(), 10)
            second = asyncio.create_task(transition(other, revision) if winner == "upload" else upload(other))
            await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, holder["pid"]), 10)
            release.set()
            result = await asyncio.wait_for(first, 10)
            loser = await asyncio.wait_for(second, 10)
            assert result.is_success, result.text
            assert loser.status_code in (404, 409), loser.text
            if winner == "upload":
                assert loser.status_code == 409 and loser.json()["code"] == "stale_revision"
                latest = result.json()["thumbnail"]["version"]
                updated = await client.get(shared_image)
                assert updated.status_code == 200 and updated.headers["etag"] == f'"{latest}"'
                # 重试时停用屏障，仅测试用户采用新版本后的明确操作。
                monkeypatch.setattr(setting_image_service, "set_series_image_without_commit", original)
                changed = await transition(client, result.json()["revision"])
                assert changed.is_success, changed.text
                expected_bytes = updated.content
            else:
                expected_bytes = initial.content
            if action == "move":
                moved = await client.get(book_image)
                assert moved.status_code == 200 and moved.content == expected_bytes
                assert (await client.get(shared_image)).status_code == 404
            else:
                assert (await client.get(book_image)).status_code == 404
                shared = await client.get(shared_image)
                assert shared.status_code == 200 and shared.content == expected_bytes
            async with app.state.sessionmaker() as db:
                use_count = await db.scalar(select(func.count()).select_from(SeriesEntryUse).where(
                    SeriesEntryUse.series_entry_id == entry_id,
                ))
                assert use_count == 0
        finally:
            release.set()
            await _stop(first)
            await _stop(second)
            await other_app.state.system_settings.close()


@pytest.mark.parametrize("winner", ["upload", "purge"])
async def test_upload_and_project_purge_serialize(postgres_series_app, monkeypatch, fake_llm, winner):
    app = postgres_series_app
    other_app = main.create_app(app.state.settings, provider_factory=fake_llm.factory, start_task_engine=False)
    await authorization_service.initialize(other_app.state.sessionmaker)
    await other_app.state.system_settings.load()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as other,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as admin,
    ):
        await register(owner, "thumbnail-purge-owner")
        other.cookies.update(owner.cookies)
        admin_user = await register(admin, "thumbnail-purge-admin")
        async with app.state.sessionmaker() as db:
            (await db.get(User, admin_user["id"])).is_admin = True
            await db.commit()
        book, base, _, thumbnail, version = await _create(owner)
        ready, release = asyncio.Event(), asyncio.Event()
        holder = {}
        if winner == "upload":
            original = setting_image_service.set_book_image_without_commit

            async def held_upload(db, *args, **kwargs):
                result = await original(db, *args, **kwargs)
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                ready.set()
                await release.wait()
                return result

            monkeypatch.setattr(setting_image_service, "set_book_image_without_commit", held_upload)
        else:
            original = setting_image_service.normalize_image_async

            async def held_decoded(*args, **kwargs):
                image = await original(*args, **kwargs)
                ready.set()
                await release.wait()
                return image

            monkeypatch.setattr(setting_image_service, "normalize_image_async", held_decoded)

        pending = asyncio.create_task(_upload(owner, thumbnail, version))
        soft_delete = None
        try:
            await asyncio.wait_for(ready.wait(), 10)
            soft_delete = asyncio.create_task(other.delete(base))
            if winner == "upload":
                await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, holder["pid"]), 10)
                release.set()
                uploaded = await asyncio.wait_for(pending, 10)
                assert uploaded.status_code == 200, uploaded.text
            deleted = await asyncio.wait_for(soft_delete, 10)
            assert deleted.status_code == 204, deleted.text
            recycle = "/api/v1/admin/recycle-bin/projects"
            listing = await admin.get(recycle)
            assert listing.status_code == 200, listing.text
            item = next(row for row in listing.json()["items"] if row["id"] == book["id"])
            purged = await admin.post(f"{recycle}/{book['id']}/purge", json={
                "deleted_at": item["deleted_at"], "title": item["title"], "confirm": True,
            })
            assert purged.status_code == 204, purged.text
            if winner == "purge":
                release.set()
                uploaded = await asyncio.wait_for(pending, 10)
                assert uploaded.status_code == 404, uploaded.text
            async with app.state.sessionmaker() as db:
                assert await db.get(Project, book["id"]) is None
                resources = await db.scalar(select(func.count()).select_from(SettingImageResource).where(
                    SettingImageResource.project_id == book["id"],
                ))
                bindings = await db.scalar(select(func.count()).select_from(SettingThumbnailBinding).where(
                    SettingThumbnailBinding.project_id == book["id"],
                ))
                assert resources == 0 and bindings == 0
        finally:
            release.set()
            await _stop(pending)
            await _stop(soft_delete)
            await other_app.state.system_settings.close()
