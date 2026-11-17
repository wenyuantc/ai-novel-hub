"""单书有效缩略图快照；不导出资源身份或系列归属。"""

import base64
from collections.abc import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError
from app.models import Career, Character, Organization, WorldSetting
from app.models.setting_images import SettingImageResource
from app.services.setting_image_service import metadata_for_book

MAX_BACKUP_BYTES = 100 * 1024 * 1024
MODELS = {"character": Character, "world": WorldSetting, "organization": Organization, "career": Career}
TABLES = {"character": "characters", "world": "world_settings", "organization": "organizations", "career": "careers"}


async def versions(db: AsyncSession, project_id: int) -> dict[tuple[str, int], str]:
    """只读元数据；本书已采用的共享条目由图片服务解析为 canonical 版本。"""
    result = {}
    for kind, model in MODELS.items():
        ids = list(await db.scalars(select(model.id).where(model.project_id == project_id).order_by(model.id)))
        for offset in range(0, len(ids), 400):
            entries = await metadata_for_book(db, project_id, kind, ids[offset:offset + 400])
            result.update({(kind, ident): meta.version for ident, meta in entries.items()})
    return result


async def versions_for_entries(
    db: AsyncSession, project_id: int, entries: dict[str, list[int]],
) -> dict[tuple[str, int], str]:
    result = {}
    for kind, ids in entries.items():
        for offset in range(0, len(ids), 400):
            values = await metadata_for_book(db, project_id, kind, ids[offset:offset + 400])
            result.update({(kind, ident): meta.version for ident, meta in values.items()})
    return result


async def _check_sizes(
    db: AsyncSession, snapshot: dict[tuple[str, int], str], *,
    max_bytes: int = MAX_BACKUP_BYTES, too_large: str = "备份文件过大，上限为 100MB",
) -> None:
    ids = list(dict.fromkeys(snapshot.values()))
    sizes = {}
    for offset in range(0, len(ids), 400):
        rows = await db.execute(select(SettingImageResource.id, SettingImageResource.byte_size)
                                .where(SettingImageResource.id.in_(ids[offset:offset + 400])))
        sizes.update(dict(rows.all()))
    if len(sizes) != len(ids):
        raise ConflictError("备份导出期间缩略图已变化，请重新导出", extra={"code": "thumbnail_changed"})
    if sum(4 * ((sizes[version] + 2) // 3) for version in snapshot.values()) > max_bytes:
        raise BadRequestError(too_large)


async def iter_serialized(
    db: AsyncSession, snapshot: dict[tuple[str, int], str], *,
    max_bytes: int = MAX_BACKUP_BYTES, too_large: str = "备份文件过大，上限为 100MB",
) -> AsyncIterator[dict]:
    """先检查编码后体积，再小批量读取资源，避免积累整份备份图片。"""
    await _check_sizes(db, snapshot, max_bytes=max_bytes, too_large=too_large)
    items = sorted(snapshot.items())
    for offset in range(0, len(items), 4):
        batch = items[offset:offset + 4]
        ids = [version for _, version in batch]
        rows = await db.execute(
            select(SettingImageResource.id, SettingImageResource.mime_type, SettingImageResource.data)
            .where(SettingImageResource.id.in_(ids))
        )
        resources = {row.id: (row.mime_type, row.data) for row in rows}
        for (kind, ident), version in batch:
            if version not in resources:
                raise ConflictError("备份导出期间缩略图已变化，请重新导出", extra={"code": "thumbnail_changed"})
            mime_type, data = resources[version]
            yield {"ref": {"kind": kind, "id": ident}, "mime_type": mime_type,
                   "data": base64.b64encode(data).decode("ascii")}


async def serialized(
    db: AsyncSession, snapshot: dict[tuple[str, int], str], *,
    max_bytes: int = MAX_BACKUP_BYTES, too_large: str = "备份文件过大，上限为 100MB",
) -> list[dict]:
    return [item async for item in iter_serialized(db, snapshot, max_bytes=max_bytes, too_large=too_large)]


async def bounded_stream(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in source:
        total += len(chunk)
        if total > MAX_BACKUP_BYTES:
            raise BadRequestError("备份文件过大，上限为 100MB")
        yield chunk
