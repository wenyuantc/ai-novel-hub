"""真实 PostgreSQL：解码期间身份/归属变化，以及图片写入和系列转换的锁顺序。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import func, select, text

from app import main
from app.models.setting_images import SettingImageResource
from app.services import authorization_service, setting_image_service
from tests.helpers import create_project, register
from tests.test_series_postgresql import _wait_for_blocker
from tests.test_series_postgresql import postgres_series_app as postgres_series_app
from tests.test_series_transfer import _join_books
from tests.test_setting_thumbnails import image_data


async def _create(client):
    book = await create_project(client)
    base = f"/api/v1/projects/{book['id']}"
    char = await client.post(f"{base}/characters", json={"name": "甲"})
    assert char.status_code == 201, char.text
    char_id = char.json()["id"]
    thumbnail = f"{base}/settings/character/{char_id}/thumbnail"
    upload = await client.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")},
                              data={"expected_version": ""})
    assert upload.status_code == 200, upload.text
    return book, base, char_id, thumbnail, upload.json()["version"]


def _upload(client, path, version):
    return client.put(path, files={"file": ("b.png", image_data("blue"), "image/png")},
                      data={"expected_version": version})


async def _stop(task):
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("change", ["revoke", "recycle"])
async def test_decoded_upload_rechecks_revocation_and_recycle(postgres_series_app, monkeypatch, change):
    app = postgres_series_app
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as guest,
    ):
        await register(owner, "thumbnail-pg-owner")
        guest_user = await register(guest, "thumbnail-pg-editor")
        book, base, _, thumbnail, version = await _create(owner)
        actor = owner
        if change == "revoke":
            invite = await owner.post(f"{base}/collaborators", json={
                "username": "thumbnail-pg-editor", "role": "editor",
            })
            assert invite.status_code == 201, invite.text
            member = invite.json()
            accepted = await guest.post(f"/api/v1/collaboration/invitations/project/{member['invitation_id']}/accept",
                                        json={"expected_revision": member["revision"]})
            assert accepted.status_code == 200, accepted.text
            actor = guest

        ready, release = asyncio.Event(), asyncio.Event()
        original = setting_image_service.normalize_image_async

        async def paused(*args, **kwargs):
            result = await original(*args, **kwargs)
            ready.set()
            await release.wait()
            return result

        monkeypatch.setattr(setting_image_service, "normalize_image_async", paused)
        pending = asyncio.create_task(_upload(actor, thumbnail, version))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            if change == "revoke":
                result = await owner.delete(f"{base}/collaborators/{guest_user['id']}", params={"expected_revision": 2})
            else:
                result = await owner.delete(base)
            assert result.status_code == 204, result.text
            release.set()
            uploaded = await asyncio.wait_for(pending, 10)
            assert uploaded.status_code in (403, 404, 409), uploaded.text
            async with app.state.sessionmaker() as db:
                count = await db.scalar(select(func.count()).select_from(SettingImageResource).where(
                    SettingImageResource.project_id == book["id"],
                ))
                assert count == 1
            if change == "revoke":
                assert (await guest.get(thumbnail, headers={"If-None-Match": f'"{version}"'})).status_code == 404
                assert (await owner.get(thumbnail)).status_code == 200
        finally:
            release.set()
            await _stop(pending)


@pytest.mark.parametrize("scope", ["project", "series"])
async def test_decoded_upload_does_not_cross_reinvitation(postgres_series_app, monkeypatch, scope):
    """图片已解码但未加写锁时，旧邀请的请求不能借新一轮授权写入。"""
    app = postgres_series_app
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as guest,
    ):
        await register(owner, "thumbnail-reinvite-owner")
        guest_user = await register(guest, "thumbnail-reinvite-editor")
        book, base, _, thumbnail, version = await _create(owner)
        if scope == "series":
            series = (await owner.post("/api/v1/series", json={"name": "共享测试系列"})).json()
            base = f"/api/v1/series/{series['id']}"
            created = await owner.post(f"{base}/entries", json={
                "kind": "character", "data": {"name": "共享测试角色"}, "expected_revision": series["revision"],
            })
            assert created.status_code == 201, created.text
            revision = created.json()["revision"]
            thumbnail = f"{base}/entries/{created.json()['item']['id']}/thumbnail"
            initial = await owner.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")}, data={
                "expected_version": "", "expected_revision": str(revision),
            })
            assert initial.status_code == 200, initial.text
            version, revision = initial.json()["thumbnail"]["version"], initial.json()["revision"]
        invite_body = {"username": "thumbnail-reinvite-editor"}
        if scope == "project":
            invite_body["role"] = "editor"

        async def accept_new_invitation():
            invited = await owner.post(f"{base}/collaborators", json=invite_body)
            assert invited.status_code == 201, invited.text
            member = invited.json()
            accepted = await guest.post(
                f"/api/v1/collaboration/invitations/{scope}/{member['invitation_id']}/accept",
                json={"expected_revision": member["revision"]},
            )
            assert accepted.status_code == 200, accepted.text
            return accepted.json()

        member = await accept_new_invitation()
        ready, release = asyncio.Event(), asyncio.Event()
        original = setting_image_service.normalize_image_async

        async def paused(*args, **kwargs):
            image = await original(*args, **kwargs)
            ready.set()
            await release.wait()
            return image

        monkeypatch.setattr(setting_image_service, "normalize_image_async", paused)
        body = {"expected_version": version}
        if scope == "series":
            body["expected_revision"] = str(revision)
        pending = asyncio.create_task(guest.put(
            thumbnail, files={"file": ("b.png", image_data("blue"), "image/png")}, data=body,
        ))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            revoked = await owner.delete(f"{base}/collaborators/{guest_user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert revoked.status_code == 204, revoked.text
            await accept_new_invitation()
            release.set()
            stale = await asyncio.wait_for(pending, 10)
            assert stale.status_code in (403, 404, 409), stale.text
            current = await owner.get(thumbnail)
            assert current.status_code == 200 and current.headers["etag"] == f'"{version}"'
            async with app.state.sessionmaker() as db:
                if scope == "project":
                    count = await db.scalar(select(func.count()).select_from(SettingImageResource).where(
                        SettingImageResource.project_id == book["id"],
                    ))
                    assert count == 1
        finally:
            release.set()
            await _stop(pending)


@pytest.mark.parametrize("read_kind", ["image", "entity"])
async def test_book_image_read_does_not_cross_reinvitation(postgres_series_app, monkeypatch, read_kind):
    app = postgres_series_app
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as guest,
    ):
        await register(owner, "thumbnail-read-reinvite-owner")
        guest_user = await register(guest, "thumbnail-read-reinvite-viewer")
        _, base, character_id, thumbnail, version = await _create(owner)

        async def accept_new_invitation():
            invited = await owner.post(f"{base}/collaborators", json={
                "username": "thumbnail-read-reinvite-viewer", "role": "viewer",
            })
            assert invited.status_code == 201, invited.text
            member = invited.json()
            accepted = await guest.post(
                f"/api/v1/collaboration/invitations/project/{member['invitation_id']}/accept",
                json={"expected_revision": member["revision"]},
            )
            assert accepted.status_code == 200, accepted.text
            return accepted.json()

        member = await accept_new_invitation()
        ready, release = asyncio.Event(), asyncio.Event()
        method = "_current_book_resource" if read_kind == "image" else "metadata_for_book"
        original = getattr(setting_image_service, method)
        reader = None

        async def paused(*args, **kwargs):
            image = await original(*args, **kwargs)
            if asyncio.current_task() is reader:
                ready.set()
                await release.wait()
            return image

        monkeypatch.setattr(setting_image_service, method, paused)
        url = thumbnail if read_kind == "image" else f"{base}/characters/{character_id}"
        headers = {"If-None-Match": f'"{version}"'} if read_kind == "image" else {}
        reader = asyncio.create_task(guest.get(url, headers=headers))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            revoked = await owner.delete(f"{base}/collaborators/{guest_user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert revoked.status_code == 204, revoked.text
            await accept_new_invitation()
            release.set()
            stale = await asyncio.wait_for(reader, 10)
            assert stale.status_code in (403, 404, 409), stale.text
            current = await guest.get(url)
            assert current.status_code == 200
            if read_kind == "image":
                assert current.headers["etag"] == f'"{version}"'
            else:
                assert current.json()["thumbnail"]["version"] == version
        finally:
            release.set()
            await _stop(reader)


async def test_series_image_read_does_not_cross_reinvitation(postgres_series_app, monkeypatch):
    app = postgres_series_app
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as guest,
    ):
        await register(owner, "thumbnail-series-read-owner")
        guest_user = await register(guest, "thumbnail-series-read-editor")
        series = (await owner.post("/api/v1/series", json={"name": "系列"})).json()
        base = f"/api/v1/series/{series['id']}"
        created = await owner.post(f"{base}/entries", json={
            "kind": "character", "data": {"name": "共享角色"}, "expected_revision": series["revision"],
        })
        assert created.status_code == 201, created.text
        thumbnail = f"{base}/entries/{created.json()['item']['id']}/thumbnail"
        image = await owner.put(thumbnail, files={"file": ("a.png", image_data(), "image/png")}, data={
            "expected_version": "", "expected_revision": str(created.json()["revision"]),
        })
        assert image.status_code == 200, image.text
        version = image.json()["thumbnail"]["version"]

        async def accept_new_invitation():
            invited = await owner.post(f"{base}/collaborators", json={"username": "thumbnail-series-read-editor"})
            assert invited.status_code == 201, invited.text
            member = invited.json()
            accepted = await guest.post(
                f"/api/v1/collaboration/invitations/series/{member['invitation_id']}/accept",
                json={"expected_revision": member["revision"]},
            )
            assert accepted.status_code == 200, accepted.text
            return accepted.json()

        member = await accept_new_invitation()
        ready, release = asyncio.Event(), asyncio.Event()
        original = setting_image_service._series_image
        reader = None

        async def paused(*args, **kwargs):
            result = await original(*args, **kwargs)
            if asyncio.current_task() is reader:
                ready.set()
                await release.wait()
            return result

        monkeypatch.setattr(setting_image_service, "_series_image", paused)
        reader = asyncio.create_task(guest.get(thumbnail, headers={"If-None-Match": f'"{version}"'}))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            revoked = await owner.delete(f"{base}/collaborators/{guest_user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert revoked.status_code == 204, revoked.text
            await accept_new_invitation()
            release.set()
            stale = await asyncio.wait_for(reader, 10)
            assert stale.status_code in (403, 404, 409), stale.text
            current = await guest.get(thumbnail)
            assert current.status_code == 200 and current.headers["etag"] == f'"{version}"'
        finally:
            release.set()
            await _stop(reader)


@pytest.mark.parametrize("winner", ["promotion", "upload"])
async def test_upload_and_promotion_serialize_without_private_image_leak(
    postgres_series_app, monkeypatch, fake_llm, winner,
):
    app = postgres_series_app
    other_app = main.create_app(app.state.settings, provider_factory=fake_llm.factory, start_task_engine=False)
    await authorization_service.initialize(other_app.state.sessionmaker)
    await other_app.state.system_settings.load()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as other,
    ):
        await register(client, "thumbnail-pg-series-owner")
        other.cookies.update(client.cookies)
        book, _, char_id, thumbnail, version = await _create(client)
        path, series = await _join_books(client, book)
        impact = await client.get(f"{path}/impact", params={
            "action": "promote", "project_id": book["id"], "kind": "character", "entry_id": char_id,
        })
        assert impact.status_code == 200, impact.text
        payload = {"project_id": book["id"], "ref": {"kind": "character", "id": char_id},
                   "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"]}
        ready, release = asyncio.Event(), asyncio.Event()
        holder = {}
        if winner == "promotion":
            original = setting_image_service.normalize_image_async

            async def paused(*args, **kwargs):
                image = await original(*args, **kwargs)
                ready.set()
                await release.wait()
                return image

            monkeypatch.setattr(setting_image_service, "normalize_image_async", paused)
        else:
            original_set = setting_image_service.set_book_image_without_commit

            async def held(db, *args, **kwargs):
                result = await original_set(db, *args, **kwargs)
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                ready.set()
                await release.wait()
                return result

            monkeypatch.setattr(setting_image_service, "set_book_image_without_commit", held)

        upload_task = asyncio.create_task(_upload(client, thumbnail, version))
        promote_task = None
        try:
            await asyncio.wait_for(ready.wait(), 10)
            promote_task = asyncio.create_task(other.post(f"{path}/entries/promote", json=payload))
            if winner == "promotion":
                promoted = await asyncio.wait_for(promote_task, 10)
                assert promoted.status_code == 201, promoted.text
                release.set()
                uploaded = await asyncio.wait_for(upload_task, 10)
                assert uploaded.status_code == 409, uploaded.text
                eid = promoted.json()["item"]["id"]
                local = await client.get(thumbnail)
                shared = await client.get(f"{path}/entries/{eid}/thumbnail")
                assert local.content == shared.content
            else:
                await asyncio.wait_for(_wait_for_blocker(app.state.sessionmaker, holder["pid"]), 10)
                release.set()
                uploaded = await asyncio.wait_for(upload_task, 10)
                assert uploaded.status_code == 200, uploaded.text
                promoted = await asyncio.wait_for(promote_task, 10)
                assert promoted.status_code == 409 and promoted.json()["code"] == "series_source_changed", promoted.text
                assert (await client.get(thumbnail)).headers["etag"] == f'"{uploaded.json()["version"]}"'
        finally:
            release.set()
            await _stop(upload_task)
            await _stop(promote_task)
            await other_app.state.system_settings.close()


@pytest.mark.parametrize("change", ["replace", "delete", "series_replace", "unuse"])
async def test_book_read_revalidates_current_subject_and_binding(postgres_series_app, monkeypatch, change):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client, "thumbnail-pg-reader")
        book, base, char_id, thumbnail, version = await _create(client)
        path = None
        if change in ("series_replace", "unuse"):
            path, series = await _join_books(client, book)
            impact = await client.get(f"{path}/impact", params={
                "action": "promote", "project_id": book["id"], "kind": "character", "entry_id": char_id,
            })
            promoted = await client.post(f"{path}/entries/promote", json={
                "project_id": book["id"], "ref": {"kind": "character", "id": char_id},
                "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"],
            })
            assert promoted.status_code == 201, promoted.text
            entry_id = promoted.json()["item"]["id"]
            revision = promoted.json()["revision"]
            version = promoted.json()["item"]["thumbnail"]["version"]
        ready, release = asyncio.Event(), asyncio.Event()
        original = setting_image_service._current_book_resource
        reader_task = None

        async def paused(*args, **kwargs):
            image = await original(*args, **kwargs)
            if asyncio.current_task() is reader_task:
                ready.set()
                await release.wait()
            return image

        monkeypatch.setattr(setting_image_service, "_current_book_resource", paused)
        reader_task = asyncio.create_task(client.get(thumbnail, headers={"If-None-Match": f'"{version}"'}))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            if change == "replace":
                result = await _upload(client, thumbnail, version)
            elif change == "delete":
                result = await client.delete(f"{base}/characters/{char_id}")
            elif change == "series_replace":
                result = await client.put(f"{path}/entries/{entry_id}/thumbnail", files={
                    "file": ("b.png", image_data("blue"), "image/png"),
                }, data={"expected_version": version, "expected_revision": str(revision)})
            else:
                result = await client.delete(f"{path}/entries/{entry_id}/uses/{book['id']}",
                                             params={"expected_revision": revision})
            assert result.is_success, result.text
            release.set()
            stale = await asyncio.wait_for(reader_task, 10)
            assert stale.status_code == 404, stale.text
            if change in ("replace", "series_replace"):
                current = await client.get(thumbnail)
                assert current.status_code == 200
                assert current.headers["etag"] != f'"{version}"'
            else:
                assert (await client.get(thumbnail)).status_code == 404
        finally:
            release.set()
            await _stop(reader_task)


async def test_editable_flag_rechecks_adoption_in_final_authorization_snapshot(postgres_series_app, monkeypatch):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client, "thumbnail-pg-capability")
        book, base, char_id, _, _ = await _create(client)
        path, series = await _join_books(client, book)
        impact = await client.get(f"{path}/impact", params={
            "action": "promote", "project_id": book["id"], "kind": "character", "entry_id": char_id,
        })
        ready, release = asyncio.Event(), asyncio.Event()
        original = setting_image_service.metadata_for_book
        reader_task = None

        async def paused(*args, **kwargs):
            result = await original(*args, **kwargs)
            if asyncio.current_task() is reader_task:
                ready.set()
                await release.wait()
            return result

        monkeypatch.setattr(setting_image_service, "metadata_for_book", paused)
        reader_task = asyncio.create_task(client.get(f"{base}/characters/{char_id}"))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            promoted = await client.post(f"{path}/entries/promote", json={
                "project_id": book["id"], "ref": {"kind": "character", "id": char_id},
                "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"],
            })
            assert promoted.status_code == 201, promoted.text
            release.set()
            response = await asyncio.wait_for(reader_task, 10)
            assert response.status_code == 200 and response.json()["thumbnail_editable"] is False, response.text
        finally:
            release.set()
            await _stop(reader_task)
