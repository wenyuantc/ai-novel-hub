"""为外部素材拉取签发单文件、限时的访问能力。"""

import hmac
import re
from time import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.media import access, locations
from app.media.config import MediaSettings
from app.models.media import MediaObject, MediaStorageLocation


def _signature(payload: str, key: bytes) -> str:
    return hmac.digest(key, payload.encode("ascii"), "sha256").hex()


def verify(token: str, settings: MediaSettings) -> str:
    match = re.fullmatch(r"([0-9a-f-]{36})\.([0-9]{1,12})\.([0-9a-f]{64})", token)
    if not match:
        raise access.missing()
    object_id, deadline, signature = match.groups()
    expected = _signature(f"{object_id}.{deadline}", settings.signing_key)
    if not hmac.compare_digest(signature, expected) or int(deadline) <= int(time()):
        raise access.missing()
    return object_id


async def external_url(db: AsyncSession, obj: MediaObject, *, expires: int = 3600) -> str | None:
    # S3 SigV4 预签名的有效期上限为七天，本地外链使用同一约定。
    if not 1 <= expires <= 604800:
        raise ValueError("媒体外链有效期必须在 1 秒至 7 天之间")
    obj = await access.resolve(db, obj.id, actor_id=None)
    location = await db.get(MediaStorageLocation, obj.location_id)
    if location is None:
        raise access.missing()
    if location.kind == "s3":
        return await locations.backend_for(db, location).presign_get(obj.storage_key, expires=expires)
    settings: MediaSettings = db.info["media_settings"]
    if not settings.public_base_url:
        return None
    payload = f"{obj.id}.{int(time()) + expires}"
    return f"{settings.public_base_url}/api/v1/media/external/{payload}.{_signature(payload, settings.signing_key)}"
