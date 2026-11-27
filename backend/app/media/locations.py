"""管理存储位置及卷标识。位置配置在创建后不可变，旧文件始终按其位置读取。"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.media.backends import Backend, LocalBackend, S3Backend
from app.models.media import MediaObject, MediaStorageLocation, MediaStorageSettings, MediaVolumeReport
from app.schemas.media_storage import (
    LocationInput,
    LocationOut,
    SavedLocation,
    StorageOut,
    StoragePatch,
    VolumeReportOut,
)
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation
from app.services.backups.network import NetworkPolicy

REPORT_LIFETIME = timedelta(minutes=25)


def backend_for(db: AsyncSession, location: MediaStorageLocation) -> Backend:
    settings = db.info["media_settings"]
    if location.kind == "local":
        return LocalBackend(Path(settings.media_dir))
    cipher = db.info["media_cipher"]
    return S3Backend(
        **location.configuration,
        access_key=cipher.decrypt(location.secrets_enc["access_key"]),
        secret_key=cipher.decrypt(location.secrets_enc["secret_key"]),
        policy=NetworkPolicy(settings.allow_private_backup_endpoints, settings.backup_ca_bundle),
    )


def _volume_id(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".volume-id"
    if not marker.exists():
        # link 只发布已完整写入的文件；两个实例首次挂载同一个卷时只会有一个胜者。
        with NamedTemporaryFile(mode="w", dir=root, delete=False, prefix=".volume-init-") as tmp:
            tmp.write(str(uuid4()))
        try:
            os.link(tmp.name, marker)
        except FileExistsError:
            pass
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    return str(UUID(marker.read_text().strip()))


async def local_volume_id(db: AsyncSession) -> str:
    return await asyncio.to_thread(_volume_id, Path(db.info["media_settings"].media_dir))


async def report_volume(db: AsyncSession, worker_key: str) -> None:
    now = datetime.now(UTC)
    volume_id = await local_volume_id(db)
    await db.execute(delete(MediaVolumeReport).where(MediaVolumeReport.seen_at < now - REPORT_LIFETIME))
    await db.merge(MediaVolumeReport(worker_key=worker_key, volume_id=volume_id, seen_at=now))


def _fresh_reports():
    return select(MediaVolumeReport).where(MediaVolumeReport.seen_at >= datetime.now(UTC) - REPORT_LIFETIME)


async def volumes_mismatch(db: AsyncSession) -> bool:
    ids = await db.scalars(_fresh_reports().with_only_columns(MediaVolumeReport.volume_id).distinct().limit(2))
    return len(ids.all()) > 1


async def describe(db: AsyncSession) -> StorageOut:
    settings = await db.get_one(MediaStorageSettings, 1, populate_existing=True)
    usage = (
        select(
            MediaObject.location_id,
            func.count(MediaObject.id).label("count"),
            func.sum(MediaObject.size_bytes).label("size"),
        )
        .group_by(MediaObject.location_id)
        .subquery()
    )
    rows = await db.execute(
        select(MediaStorageLocation, usage.c.count, usage.c.size)
        .outerjoin(usage, usage.c.location_id == MediaStorageLocation.id)
        .order_by(MediaStorageLocation.id.desc())
    )
    locations = [
        LocationOut(
            id=row.id,
            kind=row.kind,
            state=row.state,
            configuration=row.configuration,
            has_access_key=bool(row.secrets_enc.get("access_key")),
            has_secret_key=bool(row.secrets_enc.get("secret_key")),
            file_count=count or 0,
            size_bytes=size or 0,
        )
        for row, count, size in rows
    ]
    reports = [
        VolumeReportOut(
            worker_key=row.worker_key,
            volume_id=row.volume_id,
            seen_at=row.seen_at if row.seen_at.tzinfo else row.seen_at.replace(tzinfo=UTC),
        )
        for row in await db.scalars(_fresh_reports().order_by(MediaVolumeReport.worker_key))
    ]
    return StorageOut(
        revision=settings.revision,
        default_quota_bytes=settings.default_quota_bytes,
        locations=locations,
        total_bytes=sum(row.size_bytes for row in locations),
        volume_reports=reports,
        volume_mismatch=len({report.volume_id for report in reports}) > 1,
    )


async def _active(db: AsyncSession) -> MediaStorageLocation:
    return (await db.scalars(select(MediaStorageLocation).where(MediaStorageLocation.state == "active"))).one()


def _candidate(db: AsyncSession, current: MediaStorageLocation, draft: LocationInput) -> MediaStorageLocation:
    configuration = {}
    secrets = {}
    if draft.kind == "s3":
        configuration = draft.model_dump(exclude={"kind", "access_key", "secret_key"})
        if current.kind == "s3" and draft.endpoint == current.configuration.get("endpoint"):
            secrets = dict(current.secrets_enc)
        for key in ("access_key", "secret_key"):
            value = getattr(draft, key)
            if value is not None:
                secrets[key] = db.info["media_cipher"].encrypt(value.get_secret_value())
            if not secrets.get(key):
                raise BadRequestError("请为此 S3 端点填写访问密钥和秘密密钥")
    return MediaStorageLocation(kind=draft.kind, configuration=configuration, secrets_enc=secrets, state="active")


def _stale() -> ConflictError:
    return ConflictError("媒体存储设置已变化，请刷新后重试", extra={"code": "stale_revision"})


async def _probe(db: AsyncSession, location: MediaStorageLocation) -> None:
    try:
        backend = backend_for(db, location)
        await db.commit()  # 探针执行期间释放读取事务。
        await backend.check()
    except Exception:
        # SDK 错误可能包含请求信息，不向 HTTP 或日志传递原文。
        raise BadRequestError(
            "存储连接测试失败，请检查地址、密钥与读写权限", extra={"code": "media_probe_failed"}
        ) from None


async def test_location(db: AsyncSession, draft: LocationInput | SavedLocation | None) -> None:
    if isinstance(draft, SavedLocation):
        location = await db.get(MediaStorageLocation, draft.location_id)
        if location is None:
            raise NotFoundError("存储位置不存在")
        await _probe(db, location)
        return
    current = await _active(db)
    await _probe(db, _candidate(db, current, draft) if draft is not None else current)


async def apply(db: AsyncSession, actor: authz.AuthenticatedActor, patch: StoragePatch) -> StorageOut:
    candidate = None
    if patch.location is not None:
        if await db.scalar(select(MediaStorageSettings.revision).where(MediaStorageSettings.id == 1)) != patch.revision:
            raise _stale()
        candidate = _candidate(db, await _active(db), patch.location)
        await _probe(db, candidate)

    await authz.require(db, actor, "media_storage.configure", write=True)
    async with AuditOperation(db, actor.id, "media_storage.update", "media_storage", 1) as audit:
        changes = dict(revision=patch.revision + 1, last_operation_id=audit.operation_id, updated_at=datetime.now(UTC))
        if patch.default_quota_bytes is not None:
            changes["default_quota_bytes"] = patch.default_quota_bytes
            audit.summary.changed_fields.append("default_quota_bytes")
        claimed = await db.execute(
            update(MediaStorageSettings)
            .where(MediaStorageSettings.id == 1, MediaStorageSettings.revision == patch.revision)
            .values(**changes)
        )
        if claimed.rowcount != 1:
            raise _stale()
        if candidate is not None:
            await db.execute(
                update(MediaStorageLocation).where(MediaStorageLocation.state == "active").values(state="readonly")
            )
            candidate.created_by = actor.id
            db.add(candidate)
            audit.summary.changed_fields.append("storage_location")
        audit.values["target_label"] = "媒体存储设置"
        await db.flush()
        result = await describe(db)
        await audit.commit()
    return result
