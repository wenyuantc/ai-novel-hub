"""媒体对象的写入、派生与配额。写入使用独立业务会话，成功返回时文件已经可读。"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import ExitStack, aclosing
from dataclasses import dataclass
from io import BytesIO
from tempfile import TemporaryFile
from typing import BinaryIO
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.images import IMAGE_EXTENSIONS, sniff_image
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.media import locations
from app.media.cleanup import mark_deleted as mark_deleted
from app.media.cleanup import mark_project_deleted as mark_project_deleted
from app.media.cleanup import sweep as sweep
from app.models import User
from app.models.media import MediaObject, MediaStorageLocation, MediaStorageSettings
from app.services import project_lifecycle

_EXTENSIONS = {**IMAGE_EXTENSIONS, "video/mp4": "mp4", "video/webm": "webm"}
_BLOCK_SIZE = 256 * 1024


class MediaUnavailable(AppError):
    status_code = 503


@dataclass
class _Content:
    file: BinaryIO
    mime: str
    size: int
    digest: str
    width: int | None = None
    height: int | None = None
    variant: str | None = None
    duration_ms: int | None = None


def _type_error() -> BadRequestError:
    return BadRequestError("媒体格式无效或与声明不符", extra={"code": "media_type_invalid"})


def _sniff(head: bytes) -> str | None:
    if head[4:8] == b"ftyp" and len(head) >= 12:
        box_size = min(len(head), int.from_bytes(head[:4]))
        brands = [head[8:12]]
        brands.extend(head[i:i + 4] for i in range(16, box_size - 3, 4))
        if any(brand in {b"isom", b"mp41", b"mp42", b"avc1", b"dash", b"MSNV"}
               or brand[:3] == b"iso" for brand in brands):
            return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in head:
        return "video/webm"
    return sniff_image(head)


def _read_image(content: _Content, *, thumbnail: bool) -> _Content | None:
    try:
        with Image.open(content.file) as image:
            # Pillow 的默认阈值在完整解码前即可检查，避免小压缩包耗尽内存。
            if Image.MAX_IMAGE_PIXELS is not None and image.width * image.height > Image.MAX_IMAGE_PIXELS:
                raise _type_error()
            content.width, content.height = image.size
            image.load()
            if not thumbnail:
                return None
            image.thumbnail((400, 400))
            output = BytesIO()
            image.convert("RGBA" if image.has_transparency_data else "RGB").save(output, "WEBP")
            data = output.getvalue()
            return _Content(output, "image/webp", len(data), hashlib.sha256(data).hexdigest(),
                            image.width, image.height, "thumb")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise _type_error() from exc


async def _prepare(stack, source, *, mime, kind, max_bytes, variant=None) -> _Content:
    file = stack.enter_context(TemporaryFile())
    digest = hashlib.sha256()
    size = 0
    async for chunk in source:
        size += len(chunk)
        if size > max_bytes:
            raise BadRequestError("超过单文件大小限制", extra={"code": "media_too_large"})
        digest.update(chunk)
        await asyncio.to_thread(file.write, chunk)
    file.seek(0)
    detected = _sniff(file.read(256))
    if detected is None or detected != mime or detected.split("/")[0] != kind:
        raise _type_error()
    return _Content(file, detected, size, digest.hexdigest(), variant=variant)


async def _chunks(file: BinaryIO) -> AsyncIterator[bytes]:
    file.seek(0)
    while chunk := await asyncio.to_thread(file.read, _BLOCK_SIZE):
        yield chunk


async def usage_of(db: AsyncSession, user_id: int) -> int:
    return await db.scalar(select(func.coalesce(func.sum(MediaObject.size_bytes), 0)).where(
        MediaObject.owner_user_id == user_id, MediaObject.status.in_(("pending", "ready")),
    ))


async def _publish(db, parts, *, owner_id, project_id, scope, plugin_id, origin, parent=None):
    try:
        if scope not in {"project", "user"} or (scope == "project") != (project_id is not None):
            raise BadRequestError("媒体归属范围无效")
        # 同一用户在统计占用与预留字节之间不能并发插入另一份预留。提交后即释放用户锁。
        locked = await db.execute(update(User).where(User.id == owner_id)
                                  .values(id=User.id, updated_at=User.updated_at))
        if not locked.rowcount:
            raise NotFoundError("用户不存在")
        if scope == "project":
            await project_lifecycle.lock_active(db, project_id)
        if parent is not None:
            result = await db.execute(update(MediaObject).where(
                MediaObject.id == parent.id, MediaObject.status == "ready", MediaObject.parent_id.is_(None),
            ).values(status=MediaObject.status))
            if not result.rowcount:
                raise NotFoundError("原媒体不存在")
            part = parts[0]
            existing = await db.scalar(select(MediaObject).where(
                MediaObject.parent_id == parent.id, MediaObject.variant == part.variant,
            ).execution_options(populate_existing=True))
            if existing is not None:
                if existing.status == "ready" and existing.mime == part.mime and existing.sha256 == part.digest:
                    await db.commit()
                    return existing
                raise ConflictError("该派生媒体已存在或正在处理", extra={"code": "media_variant_conflict"})
        # 与管理员切换位置共同锁定稳定的单行，随后重新查询当前 active。
        settings = await db.get(MediaStorageSettings, 1, populate_existing=True, with_for_update=True)
        if await usage_of(db, owner_id) + sum(part.size for part in parts) > settings.default_quota_bytes:
            raise ConflictError("媒体占用超过用户配额", extra={"code": "media_quota_exceeded"})

        location = await db.scalar(select(MediaStorageLocation).where(MediaStorageLocation.state == "active"))
        backend = locations.backend_for(db, location)
        volume_id = await locations.local_volume_id(db) if location.kind == "local" else None
        rows = []
        for part in parts:
            uid = str(uuid4())
            root_id = rows[0].id if rows else None
            row = MediaObject(
                id=uid, location_id=location.id, volume_id=volume_id,
                storage_key=f"{uid[:2]}/{uid}.{_EXTENSIONS[part.mime]}", owner_user_id=owner_id,
                project_id=project_id, scope=scope, plugin_id=plugin_id, kind=part.mime.split("/")[0],
                mime=part.mime, size_bytes=part.size, sha256=part.digest, width=part.width, height=part.height,
                parent_id=parent.id if parent is not None else root_id, duration_ms=part.duration_ms,
                variant=part.variant, source=origin, status="pending",
            )
            db.add(row)
            # 自引用 FK 的原件必须先于派生文件插入。
            await db.flush()
            rows.append(row)
        ids = [row.id for row in rows]
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    try:
        # 传输不占数据库写锁。清理者保留年轻的 pending 行；删除意图禁止最后的 CAS 发布。
        # 单次传输的总时限远短于崩溃恢复的 24 小时宽限期。
        async with asyncio.timeout(6 * 3600):
            for row, part in zip(rows, parts, strict=True):
                await backend.put(row.storage_key, _chunks(part.file), size_hint=part.size, max_bytes=part.size)
        published = await db.execute(update(MediaObject).where(
            MediaObject.id.in_(ids), MediaObject.status == "pending", MediaObject.deleted_at.is_(None),
            MediaObject.owner_user_id == owner_id, MediaObject.project_id == project_id,
        ).values(status="ready"))
        if published.rowcount != len(rows):
            raise ConflictError("媒体已进入清理")
        await db.commit()
    except BaseException:
        await db.rollback()
        # 提交结果不确定时只标记仍为 pending 的预留，不能删除已成功发布的文件。
        await db.execute(update(MediaObject).where(MediaObject.id.in_(ids), MediaObject.status == "pending")
                         .values(status="deleting", deleted_at=project_lifecycle.now_utc()))
        await db.commit()
        raise
    return rows[0]


async def store(db: AsyncSession, *, owner_id: int, scope: str, kind: str, mime: str, source: AsyncIterator[bytes],
                max_bytes: int, project_id: int | None = None, plugin_id: str | None = None,
                origin: str = "upload") -> MediaObject:
    with ExitStack() as stack:
        content = await _prepare(stack, source, mime=mime, kind=kind, max_bytes=max_bytes)
        parts = [content]
        if kind == "image":
            thumb = await asyncio.to_thread(_read_image, content, thumbnail=True)
            stack.callback(thumb.file.close)
            parts.append(thumb)
        return await _publish(db, parts, owner_id=owner_id, project_id=project_id, scope=scope,
                              plugin_id=plugin_id, origin=origin)


async def store_derived(db: AsyncSession, parent: MediaObject, variant: str, mime: str, data: bytes) -> MediaObject:
    if (parent.kind, variant) not in {("video", "poster"), ("image", "thumb")}:
        raise BadRequestError("媒体派生类型无效")
    with ExitStack() as stack:
        content = await _prepare(stack, _chunks(BytesIO(data)), mime=mime, kind="image", max_bytes=len(data),
                                 variant=variant)
        await asyncio.to_thread(_read_image, content, thumbnail=False)
        return await _publish(db, [content], owner_id=parent.owner_user_id, project_id=parent.project_id,
                              scope=parent.scope, plugin_id=parent.plugin_id, origin=parent.source, parent=parent)


async def open_range(db: AsyncSession, obj: MediaObject, start: int | None = None,
                     end: int | None = None) -> AsyncGenerator[bytes, None]:
    if obj.status != "ready":
        raise NotFoundError("媒体文件不存在")
    location = await db.get(MediaStorageLocation, obj.location_id)
    backend = locations.backend_for(db, location)
    if location.kind == "local" and obj.volume_id and await locations.local_volume_id(db) != obj.volume_id:
        _wrong_volume(obj)
    if await backend.stat(obj.storage_key) is None:
        if location.kind == "local" and not obj.volume_id and await locations.volumes_mismatch(db):
            _wrong_volume(obj)
        raise NotFoundError("媒体文件不存在")
    return backend.open(obj.storage_key, start=start, end=end)


def _wrong_volume(obj: MediaObject):
    logging.getLogger(__name__).warning("当前实例未挂载媒体文件所属的卷 object=%s", obj.id)
    raise MediaUnavailable("当前实例未挂载文件所属的媒体卷", extra={"code": "media_unavailable_on_instance"})


async def copy(db: AsyncSession, obj: MediaObject, *, owner_id: int, scope: str,
               project_id: int | None = None, plugin_id: str | None = None) -> MediaObject:
    if obj.parent_id is not None:
        raise BadRequestError("请选择媒体原件")
    variants = (await db.scalars(select(MediaObject).where(
        MediaObject.parent_id == obj.id, MediaObject.status == "ready",
    ))).all()
    with ExitStack() as stack:
        parts = []
        for item in (obj, *variants):
            async with aclosing(await open_range(db, item)) as stream:
                content = await _prepare(stack, stream, mime=item.mime, kind=item.kind, max_bytes=item.size_bytes,
                                         variant=item.variant)
            if content.size != item.size_bytes or content.digest != item.sha256:
                raise ConflictError("源文件内容与媒体记录不符")
            content.width, content.height = item.width, item.height
            content.duration_ms = item.duration_ms
            parts.append(content)
        return await _publish(db, parts, owner_id=owner_id, project_id=project_id, scope=scope,
                              plugin_id=plugin_id, origin="copy")
