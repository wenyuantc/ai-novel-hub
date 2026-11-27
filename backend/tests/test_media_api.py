from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from app.media import service, signing
from app.models.media import MediaObject, MediaStorageLocation
from tests.helpers import create_project, register
from tests.plugins.sample_plugin import SAMPLE

VIDEO = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + bytes(range(64))


@pytest.fixture
def settings_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr("app.plugins.builtins.REGISTERED", (SAMPLE,))
    return {"media_dir": str(tmp_path / "media"), "public_base_url": "https://media.example.org/"}


async def media(app, owner, project=None, plugin_id=None):
    async def chunks():
        yield VIDEO
    async with app.state.sessionmaker() as db:
        return await service.store(
            db, owner_id=owner, project_id=project, scope="project" if project else "user",
            plugin_id=plugin_id, kind="video", mime="video/mp4", source=chunks(), max_bytes=len(VIDEO),
        )


async def test_range_download_and_identity(app, client, make_client):
    owner = await register(client)
    obj = await media(app, owner["id"])
    url = f"/api/v1/media/{obj.id}"
    anon = make_client()
    assert (await anon.get(url)).status_code == 404
    await register(anon, "outsider")
    assert (await anon.get(url)).status_code == 404
    full = await client.get(url)
    assert full.content == VIDEO and full.headers["content-disposition"] == "inline"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["accept-ranges"] == "bytes"
    for requested, start, end in (("bytes=0-0", 0, 0), ("bytes=11-", 11, len(VIDEO) - 1),
                                  ("bytes=-8", len(VIDEO) - 8, len(VIDEO) - 1)):
        response = await client.get(url, headers={"Range": requested}, params={"download": 1})
        assert response.status_code == 206 and response.content == VIDEO[start:end + 1]
        assert response.headers["content-range"] == f"bytes {start}-{end}/{len(VIDEO)}"
        assert response.headers["content-disposition"] == "attachment"
        assert int(response.headers["content-length"]) == end - start + 1
    for invalid in ("bytes=999-", "bytes=-0", "bytes=5-2"):
        r = await client.get(url, headers={"Range": invalid})
        assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(VIDEO)}"
    assert (await client.get(url, params={"variant": "thumb"})).status_code == 404


async def test_project_collaborator_and_plugin_state(app, client, make_client, system_admin):
    owner = await register(client)
    project = await create_project(client)
    obj = await media(app, owner["id"], project["id"], "example")
    url = f"/api/v1/media/{obj.id}"
    assert (await client.get(url)).status_code == 404
    switch = "/api/v1/admin/plugins/example"
    assert (await system_admin.patch(switch, json={"revision": 0, "enabled": True})).status_code == 200
    assert (await client.get(url)).content == VIDEO
    guest = make_client()
    await register(guest, "reader")
    assert (await guest.get(url)).status_code == 404
    invite = await client.post(f"/api/v1/projects/{project['id']}/collaborators",
                               json={"username": "reader", "role": "viewer"})
    assert invite.status_code == 201, invite.text
    data = invite.json()
    accepted = await guest.post(f"/api/v1/collaboration/invitations/project/{data['invitation_id']}/accept",
                                json={"expected_revision": data["revision"]})
    assert accepted.status_code == 200
    assert (await guest.get(url)).content == VIDEO
    assert (await guest.delete(url)).status_code == 405
    assert (await system_admin.patch(switch, json={"revision": 1, "enabled": False})).status_code == 200
    assert (await guest.get(url)).status_code == 404


async def test_external_capability_lifetime_and_deletion(app, client, make_client, monkeypatch):
    owner = await register(client)
    obj = await media(app, owner["id"])
    now = 2_000_000_000
    monkeypatch.setattr(signing, "time", lambda: now)
    async with app.state.sessionmaker() as db:
        url = await signing.external_url(db, obj, expires=60)
        assert url.startswith("https://media.example.org/api/v1/media/external/")
        settings = db.info["media_settings"]
        assert not hasattr(settings, "jwt_secret") and not hasattr(settings, "encryption_key")
        db.info["media_settings"] = settings.model_copy(update={"public_base_url": ""})
        assert await signing.external_url(db, obj) is None
    path = urlsplit(url).path
    anon = make_client()
    allowed = await anon.get(path, headers={"Range": "bytes=1-2"})
    assert allowed.status_code == 206 and allowed.content == VIDEO[1:3]
    assert allowed.headers["cache-control"] == "private, no-store"
    assert (await anon.get(path + "x")).status_code == 404
    now += 60
    assert (await anon.get(path)).status_code == 404
    now -= 1
    async with app.state.sessionmaker() as db:
        await service.mark_deleted(db, [obj.id])
        await db.commit()
    assert (await anon.get(path)).status_code == 404


async def test_peer_reads_shared_volume_and_diagnoses_missing_file(app, client, second_app, tmp_path):
    owner = await register(client)
    obj = await media(app, owner["id"])
    url = f"/api/v1/media/{obj.id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
                                 cookies=client.cookies) as peer:
        assert (await peer.get(url, headers={"Range": "bytes=-10"})).content == VIDEO[-10:]
        sessions = second_app.state.sessionmaker
        info = sessions.kw["info"]
        other_volume = info["media_settings"].model_copy(update={"media_dir": str(tmp_path / "other-volume")})
        sessions.configure(info={**info, "media_settings": other_volume})
        unavailable = await peer.get(url)
        assert unavailable.status_code == 503 and unavailable.json()["code"] == "media_unavailable_on_instance"
    (Path(app.state.settings.media_dir) / obj.storage_key).unlink()
    assert (await client.get(url)).status_code == 404


async def test_poster_access_by_variant_and_direct_id(app, client, make_client):
    owner = await register(client)
    obj = await media(app, owner["id"])
    poster = BytesIO()
    Image.new("RGB", (2, 2)).save(poster, "PNG")
    async with app.state.sessionmaker() as db:
        child = await service.store_derived(db, obj, "poster", "image/png", poster.getvalue())
    url = f"/api/v1/media/{obj.id}?variant=poster"
    direct = f"/api/v1/media/{child.id}"
    for route in (url, direct):
        response = await client.get(route)
        assert response.content == poster.getvalue() and response.headers["content-type"] == "image/png"
    outsider = make_client()
    await register(outsider, "stranger")
    assert (await outsider.get(direct)).status_code == 404
    async with app.state.sessionmaker() as db:
        (await db.get(MediaObject, obj.id)).status = "deleting"
        await db.commit()
    assert (await client.get(direct)).status_code == 404


async def test_s3_external_url_has_sigv4_and_storage_key(app, client):
    owner = await register(client)
    obj = await media(app, owner["id"])
    async with app.state.sessionmaker() as db:
        cipher = db.info["media_cipher"]
        location = MediaStorageLocation(
            kind="s3", state="readonly", configuration={
                "endpoint": "https://93.184.216.34", "region": "us-east-1", "bucket": "media", "prefix": "assets",
            },
            secrets_enc={"access_key": cipher.encrypt("access"), "secret_key": cipher.encrypt("secret")},
        )
        db.add(location)
        await db.flush()
        stored = await db.get(MediaObject, obj.id)
        stored.location_id = location.id
        await db.flush()
        signed = urlsplit(await signing.external_url(db, stored, expires=90))
    assert signed.netloc == "93.184.216.34" and signed.path == f"/media/assets/{obj.storage_key}"
    parameters = parse_qs(signed.query)
    assert parameters["X-Amz-Expires"] == ["90"] and parameters["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert parameters["X-Amz-Signature"]


async def test_stream_releases_database_and_closes_on_disconnect(monkeypatch):
    from types import SimpleNamespace

    from starlette.requests import ClientDisconnect

    from app.api.v1.media import _response

    closed = False
    released = False

    async def disconnect(_message):
        raise BrokenPipeError

    async def rollback():
        nonlocal released
        released = True

    async def open_range(*args):
        async def stream():
            nonlocal closed
            try:
                assert released
                yield b"a"
                yield b"b"
            finally:
                closed = True
        return stream()

    monkeypatch.setattr(service, "open_range", open_range)
    response = await _response(SimpleNamespace(rollback=rollback), SimpleNamespace(size_bytes=2, mime="video/mp4"),
                               None, download=False)
    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, disconnect)
    assert closed
