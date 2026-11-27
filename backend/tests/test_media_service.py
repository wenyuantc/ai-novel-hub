import hashlib
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.errors import AppError
from app.media import service
from app.models.media import MediaObject, MediaStorageSettings
from tests.helpers import register

VIDEO = b"\x00\x00\x00\x18ftypmp42" + bytes(128)


async def chunks(data):
    yield data[:3]
    yield data[3:]


async def test_store_and_copy_image_with_thumb(app, client):
    user = (await register(client))["id"]
    png = BytesIO()
    Image.new("RGB", (720, 360)).save(png, "PNG")
    data = png.getvalue()
    async with app.state.sessionmaker() as db:
        image = await service.store(db, owner_id=user, scope="user", kind="image", mime="image/png",
                                    source=chunks(data), max_bytes=len(data))
        assert (image.width, image.height, image.sha256) == (720, 360, hashlib.sha256(data).hexdigest())
        thumb = await db.scalar(select(MediaObject).where(MediaObject.parent_id == image.id))
        assert (thumb.variant, thumb.width, thumb.height, thumb.mime) == ("thumb", 400, 200, "image/webp")
        assert await service.usage_of(db, user) == len(data) + thumb.size_bytes
        copied = await service.copy(db, image, owner_id=user, scope="user")
        assert copied.id != image.id and copied.sha256 == image.sha256
        stream = await service.open_range(db, copied, 2, 6)
        assert b"".join([chunk async for chunk in stream]) == data[2:7]


async def test_store_rejects_invalid_input_and_limits(app, client):
    user = (await register(client))["id"]
    async with app.state.sessionmaker() as db:
        for data, limit, code in [(b"bad", 90, "media_type_invalid"), (VIDEO, 120, "media_too_large")]:
            with pytest.raises(AppError) as exc:
                await service.store(db, owner_id=user, scope="user", kind="video", mime="video/mp4",
                                    source=chunks(data), max_bytes=limit)
            assert exc.value.extra["code"] == code
        settings = await db.get(MediaStorageSettings, 1)
        settings.default_quota_bytes = 64
        await db.commit()
        with pytest.raises(AppError) as exc:
            await service.store(db, owner_id=user, scope="user", kind="video", mime="video/mp4",
                                source=chunks(VIDEO), max_bytes=1024)
        assert exc.value.extra["code"] == "media_quota_exceeded"
        assert await service.usage_of(db, user) == 0


async def test_partial_backend_write_never_publishes_media(app, client, monkeypatch):
    from app.media.backends import LocalBackend

    user = (await register(client))["id"]

    async def fail(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(LocalBackend, "put", fail)
    async with app.state.sessionmaker() as db:
        with pytest.raises(OSError):
            await service.store(db, owner_id=user, scope="user", kind="video", mime="video/mp4",
                                source=chunks(VIDEO), max_bytes=1024)
        obj = await db.scalar(select(MediaObject))
        assert obj is not None and obj.status != "ready"
