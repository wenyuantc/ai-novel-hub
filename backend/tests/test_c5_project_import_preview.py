import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest
from sqlalchemy import func, select, update

from app.models import Project
from app.models.project_import_preview import ProjectImportPreview, ProjectImportPreviewChunk
from app.services.project_transfer import importer, preview_store
from tests.helpers import create_project, register
from tests.schema_helpers import isolated_worker_database

API = "/api/v1/projects"


@pytest.fixture(params=("sqlite", "postgresql"))
async def settings_overrides(request, monkeypatch):
    if request.param == "sqlite":
        yield {}
    else:
        async with isolated_worker_database(monkeypatch, required=True) as overrides:
            yield overrides


def _client(app, cookies=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", cookies=cookies,
    )


def _digest(token):
    return sha256(token.encode("ascii")).hexdigest()


async def _source(client):
    user = await register(client)
    source = await create_project(client, title="跨进程导入原稿")
    response = await client.get(f"{API}/{source['id']}/backup")
    assert response.status_code == 200, response.text
    return user, response.content


async def _preview(client, raw):
    result = await client.post(f"{API}/import/preview", files={"file": ("backup.json", raw, "application/json")})
    assert result.status_code == 200, result.text
    assert result.json()["expires_in"] == 600
    return result.json()["token"]


async def test_two_apps_cannot_commit_same_import_preview_twice(app, second_app, client, monkeypatch):
    user, raw = await _source(client)
    token = await _preview(client, raw)

    # 两个实例都已经读到有效令牌，随后才允许竞争写入事务。
    enter = asyncio.Barrier(2)
    original = importer.import_backup

    async def synchronized_import(*args, **kwargs):
        async with asyncio.timeout(5):
            await enter.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(importer, "import_backup", synchronized_import)
    async with _client(second_app, client.cookies) as other:
        responses = await asyncio.gather(*(
            connection.post(f"{API}/import", json={"token": token}) for connection in (client, other)
        ))

    assert sorted(result.status_code for result in responses) == [201, 400]
    rejected = next(result for result in responses if result.status_code == 400)
    assert rejected.json() == {"detail": "导入凭据已过期，请重新选择备份文件"}
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == user["id"])) == 2


async def test_peer_confirms_encrypted_preview_from_database(app, second_app, client):
    user, raw = await _source(client)
    token = await _preview(client, raw)
    digest = _digest(token)
    async with second_app.state.sessionmaker() as db:
        preview = await db.get(ProjectImportPreview, digest)
        assert preview.user_id == user["id"] and preview.size_bytes == len(raw)
        ciphertext = (await db.get(ProjectImportPreviewChunk, (digest, 0))).ciphertext
        assert len(ciphertext) > len(raw)
        assert raw not in ciphertext and token.encode() not in ciphertext
        assert "跨进程导入原稿".encode() not in ciphertext

    async with _client(second_app, client.cookies) as peer:
        result = await peer.post(f"{API}/import", json={"token": token, "title": "在对端确认"})
    assert result.status_code == 201 and result.json()["project"]["title"] == "在对端确认", result.text
    async with app.state.sessionmaker() as db:
        assert await db.get(ProjectImportPreview, digest) is None
        assert await db.get(ProjectImportPreviewChunk, (digest, 0)) is None


async def test_preview_eviction_is_ordered_and_shared(app, second_app, client, make_client):
    _, raw = await _source(client)
    async with _client(second_app, client.cookies) as peer:
        user_first = await _preview(client, raw)
        user_second = await _preview(peer, raw)
        user_third = await _preview(client, raw)
        assert (await peer.post(f"{API}/import", json={"token": user_first})).json() == {
            "detail": preview_store.TOKEN_EXPIRED,
        }
    other_users = [make_client() for _ in range(4)]
    for index, other in enumerate(other_users):
        await register(other, f"import_other_{index}")
    admitted = []
    for other in other_users[:2]:
        async with _client(second_app, other.cookies) as peer:
            admitted.append(await _preview(peer, raw))
            admitted.append(await _preview(other, raw))
    async with _client(second_app, other_users[3].cookies) as peer:
        # 两个账号各持自己的 User 屏障，两个实例仍须争同一条全局配额行。
        admitted.extend(await asyncio.gather(_preview(other_users[2], raw), _preview(peer, raw)))
    async with app.state.sessionmaker() as db:
        retained = set(await db.scalars(select(ProjectImportPreview.token_hash)))
        assert retained == {_digest(token) for token in admitted}
        for token in (user_first, user_second, user_third):
            assert await db.get(ProjectImportPreviewChunk, (_digest(token), 0)) is None
    result = await client.post(f"{API}/import", json={"token": user_third})
    assert result.status_code == 400 and result.json() == {"detail": preview_store.TOKEN_EXPIRED}


async def test_preview_maintenance_cleans_expired_chunks(app, second_app, client):
    _, raw = await _source(client)
    expired = _digest(await _preview(client, raw))
    live = _digest(await _preview(client, raw))
    async with app.state.sessionmaker.begin() as db:
        await db.execute(update(ProjectImportPreview).where(ProjectImportPreview.token_hash == expired)
                         .values(expires_at=datetime.now(UTC) - timedelta(seconds=2)))
    async with preview_store.preview_maintenance(second_app.state.sessionmaker):
        async with app.state.sessionmaker() as db:
            assert await db.get(ProjectImportPreview, expired) is None
            assert await db.get(ProjectImportPreviewChunk, (expired, 0)) is None
            assert await db.get(ProjectImportPreviewChunk, (live, 0)) is not None


async def test_preview_ciphertext_is_bound_to_the_token(app, second_app, client):
    _, raw = await _source(client)
    first = await _preview(client, raw)
    other = await _preview(client, raw)
    async with app.state.sessionmaker.begin() as db:
        old_chunk = await db.get(ProjectImportPreviewChunk, (_digest(first), 0))
        new_chunk = await db.get(ProjectImportPreviewChunk, (_digest(other), 0))
        new_chunk.ciphertext = old_chunk.ciphertext
    async with _client(second_app, client.cookies) as peer:
        result = await peer.post(f"{API}/import", json={"token": other})
    assert result.status_code == 400 and result.json() == {"detail": preview_store.TOKEN_EXPIRED}


async def test_cancel_after_token_delete_rolls_back_new_project(app, second_app, client, monkeypatch):
    user, raw = await _source(client)
    token = await _preview(client, raw)
    delete_reached = asyncio.Event()
    original = preview_store.consume

    async def cancel_after_delete(*args):
        await original(*args)
        delete_reached.set()
        await asyncio.Event().wait()

    async with _client(second_app, client.cookies) as peer:
        with monkeypatch.context() as patch:
            patch.setattr(preview_store, "consume", cancel_after_delete)
            request = asyncio.create_task(peer.post(f"{API}/import", json={"token": token}))
            try:
                await asyncio.wait_for(delete_reached.wait(), timeout=5)
            finally:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == user["id"])) == 1
            assert await db.get(ProjectImportPreviewChunk, (_digest(token), 0)) is not None
        retry = await peer.post(f"{API}/import", json={"token": token})
    assert retry.status_code == 201, retry.text
