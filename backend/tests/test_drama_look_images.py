from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.errors import ConflictError, NotFoundError
from app.media import service as media
from app.models.media import MediaObject
from app.plugins.drama.models import DramaCharacter, DramaLook, DramaSeries
from tests.helpers import create_project, register

BASE = "/api/v1/plugins/drama"


@pytest.fixture
def settings_overrides(tmp_path):
    return {"media_dir": str(tmp_path / "media")}


@pytest.fixture
async def looks(app, client, system_admin):
    owner = (await register(client))["id"]
    project_id = (await create_project(client))["id"]
    assert (await system_admin.patch("/api/v1/admin/plugins/drama", json={"revision": 0, "enabled": True})).is_success
    async with app.state.sessionmaker() as db:
        series = DramaSeries(project_id=project_id, title="一部剧")
        db.add(series)
        await db.flush()
        char = DramaCharacter(series_id=series.id, name="阿禾")
        db.add(char)
        await db.flush()
        default = DramaLook(character_id=char.id, is_default=True, name="常服")
        other = DramaLook(character_id=char.id, name="礼服")
        db.add_all([default, other])
        await db.commit()
        return owner, project_id, series.id, default.id, other.id


async def save_image(db, owner, project_id):
    buffer = BytesIO()
    Image.new("RGB", (32, 24), "#776655").save(buffer, format="PNG")

    async def chunks():
        yield buffer.getvalue()

    return await media.store(
        db,
        owner_id=owner,
        project_id=project_id,
        scope="project",
        plugin_id="drama",
        kind="image",
        mime="image/png",
        origin="generated",
        source=chunks(),
        max_bytes=10_000,
    )


async def test_history_select_reference_thumbnail_and_delete(app, client, make_client, system_admin, looks):
    from app.plugins.drama.services import images

    owner, pid, sid, first, other = looks
    async with app.state.sessionmaker() as db:
        for prompt in ("夜色", "晨光"):
            stored = await save_image(db, owner, pid)
            look = await db.get(DramaLook, first)
            row = await images.add_generated(db, look, stored.id, prompt, [])
            await db.commit()
        assert (await images.add_generated(db, look, stored.id, "晨光", [])).id == row.id
        assert (await images.default_reference(db, await db.get(DramaLook, other))).prompt == "夜色"
        assert await images.default_reference(db, look) is None
    url = f"{BASE}/assets/look/{first}/images"
    result = await client.get(url)
    assert result.is_success, result.text
    latest, older = result.json()
    assert older["is_selected"] and not latest["is_selected"]
    assert latest["prompt"] == "晨光"
    thumb_url = f"/api/v1/media/{latest['media_object_id']}?variant=thumb"
    thumb = await client.get(thumb_url)
    assert thumb.is_success and thumb.headers["content-type"] == "image/webp"
    bundle = (await client.get(f"{BASE}/series/{sid}/assets")).json()
    item = bundle["characters"][0]["looks"][0]
    assert item["image_count"] == 2 and item["selected_image"]["id"] == older["id"]
    guest = make_client()
    await register(guest, "look-viewer")
    assert (await guest.get(url)).status_code == 404
    invitation = (
        await client.post(
            f"/api/v1/projects/{pid}/collaborators",
            json={
                "username": "look-viewer",
                "role": "viewer",
            },
        )
    ).json()
    assert (
        await guest.post(
            f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
            json={"expected_revision": invitation["revision"]},
        )
    ).is_success
    image_url = f"{BASE}/asset-images/{latest['id']}"
    assert (await guest.get(url)).is_success and (await guest.get(thumb_url)).is_success
    assert (await guest.patch(image_url, json={"is_selected": True})).status_code == 403
    assert (await client.patch(image_url, json={"is_selected": True})).json()["is_selected"]
    assert (await system_admin.patch("/api/v1/admin/plugins/drama", json={"revision": 1, "enabled": False})).is_success
    assert (await client.get(thumb_url)).status_code == 404
    assert (await system_admin.patch("/api/v1/admin/plugins/drama", json={"revision": 2, "enabled": True})).is_success
    assert (await client.delete(image_url)).status_code == 204
    assert (await client.get(thumb_url)).status_code == 404
    (remaining,) = (await client.get(url)).json()
    assert remaining["is_selected"] and remaining["id"] == older["id"]
    async with app.state.sessionmaker() as db:
        statuses = list(
            await db.scalars(
                select(MediaObject.status).where(
                    (MediaObject.id == latest["media_object_id"]) | (MediaObject.parent_id == latest["media_object_id"])
                )
            )
        )
        assert statuses == ["deleting", "deleting"]


async def test_history_limit_and_media_ownership(app, client, looks):
    from app.plugins.drama.models import DramaAssetImage
    from app.plugins.drama.services import images

    owner, pid, sid, lid, other = looks
    other_pid = (await create_project(client))["id"]
    async with app.state.sessionmaker() as db:
        obj = await save_image(db, owner, other_pid)
        look = await db.get(DramaLook, lid)
        with pytest.raises(NotFoundError):
            await images.add_generated(db, look, obj.id, "", [])
        await db.rollback()
        db.add_all(
            DramaAssetImage(
                series_id=sid,
                owner_type="look",
                owner_id=lid,
                source="generated",
                status="failed",
                prompt="",
                reference_image_ids=[],
                error_message="失败",
                is_selected=False,
            )
            for _ in range(10)
        )
        await db.commit()
        with pytest.raises(ConflictError):
            await images.ensure_capacity(db, lid)
        await images.ensure_capacity(db, other)


async def test_pending_slot_visible_and_protected_until_failure(app, client, looks):
    from app.plugins.drama.models import DramaAssetImage
    from app.plugins.drama.services import images

    _, _, sid, lid, _ = looks
    async with app.state.sessionmaker() as db:
        look = await db.get(DramaLook, lid)
        reserved = await images.reserve_generation(db, look, prompt="古装人物")
        await db.commit()
        iid = reserved.id
    item_url = f"{BASE}/asset-images/{iid}"
    assert (await client.delete(item_url)).status_code == 409
    assert (await client.patch(item_url, json={"is_selected": True})).status_code == 409
    bundle_url = f"{BASE}/series/{sid}/assets"
    look = (await client.get(bundle_url)).json()["characters"][0]["looks"][0]
    assert look["image_count"] == look["pending_image_count"] == 1
    assert look["selected_image"] is None and look["latest_image_error"] is None
    async with app.state.sessionmaker() as db:
        reserved = await db.get(DramaAssetImage, iid)
        reserved.status = "failed"
        reserved.error_message = "模型未返回图片"
        await db.commit()
    failed = (await client.get(bundle_url)).json()["characters"][0]["looks"][0]
    assert failed["pending_image_count"] == 0 and failed["latest_image_error"] == "模型未返回图片"
    assert (await client.delete(item_url)).status_code == 204
    assert (await client.get(f"{BASE}/assets/look/{lid}/images")).json() == []
