import wave
from io import BytesIO
from urllib.parse import urlsplit

import pytest

from app.core.errors import BadRequestError
from app.media import service, signing
from tests.helpers import register


@pytest.fixture
def settings_overrides(tmp_path):
    return {"media_dir": str(tmp_path / "media"), "public_base_url": "https://media.example.org"}


def wav_data():
    out = BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setparams((1, 2, 8_000, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(16_000))
    return out.getvalue()


async def stream(data):
    yield data


async def test_wav_reference_owner_and_signed_read(app, client, make_client):
    user = await register(client)
    content = wav_data()
    async with app.state.sessionmaker() as db:
        obj = await service.store(db, owner_id=user["id"], scope="user", kind="audio", mime="audio/wav",
                                  source=stream(content), max_bytes=len(content))
        assert obj.storage_key.endswith(".wav") and obj.duration_ms == 1_000
        assert await service.usage_of(db, user["id"]) == len(content)
        signed = await signing.external_url(db, obj)
    url = f"/api/v1/media/{obj.id}"
    owned = await client.get(url)
    assert owned.status_code == 200 and owned.headers["content-type"] == "audio/wav"
    assert owned.content == content
    other = make_client()
    await register(other, "other")
    assert (await other.get(url)).status_code == 404
    external = await make_client().get(urlsplit(signed).path, headers={"Range": "bytes=0-11"})
    assert external.status_code == 206 and external.content == content[:12]


@pytest.mark.parametrize("data", [b"RIFF" + bytes(4) + b"WAVE" + bytes(32), wav_data()[:-2], b"not audio"])
async def test_invalid_or_truncated_wav_rejected_before_reserving_quota(app, client, data):
    user = await register(client)
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError) as exc:
            await service.store(db, owner_id=user["id"], scope="user", kind="audio", mime="audio/wav",
                                source=stream(data), max_bytes=len(data))
        assert exc.value.extra["code"] == "media_type_invalid"
        assert await service.usage_of(db, user["id"]) == 0
