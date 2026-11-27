"""媒体删除分为业务事务内标记及独立事务内回收，后端删除失败可重试。"""

import asyncio
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched
from uuid import uuid4

from sqlalchemy import and_, case, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.media import locations
from app.models.media import MediaObject, MediaStorageLocation, MediaVolumeReport

logger = logging.getLogger(__name__)
_BATCH = 128


def _mark_values(now):
    # 正在传输的预留保持 pending，直到写入者收到撤销，或宽限期过后由清理任务接管。
    status = case((MediaObject.status == "pending", "pending"), else_="deleting")
    return {"status": status, "deleted_at": now}


async def mark_deleted(db: AsyncSession, obj_ids: Iterable[str]) -> None:
    now = datetime.now(UTC)
    for ids in batched(obj_ids, _BATCH):
        await db.execute(update(MediaObject).where(
            or_(MediaObject.id.in_(ids), MediaObject.parent_id.in_(ids)),
        ).values(**_mark_values(now)))


async def mark_project_deleted(db: AsyncSession, project_id: int) -> list[str]:
    return list(await db.scalars(update(MediaObject).where(MediaObject.project_id == project_id)
                                .values(**_mark_values(datetime.now(UTC))).returning(MediaObject.id)))


def _eligible(now):
    orphan = or_(MediaObject.owner_user_id.is_(None),
                 and_(MediaObject.scope == "project", MediaObject.project_id.is_(None)))
    return or_(and_(MediaObject.status == "pending", MediaObject.created_at < now - timedelta(hours=24)),
               and_(MediaObject.status != "pending", or_(MediaObject.status == "deleting", orphan)))


async def _remove(db, oid, now) -> bool:
    # 只在短事务内领取和确认；网络超时或慢盘不会占据 SQLite 的全库写锁。
    token = str(uuid4())
    lease_time = datetime.now(UTC)
    available = or_(MediaObject.cleanup_token.is_(None),
                    MediaObject.cleanup_claimed_at < lease_time - timedelta(minutes=10))
    held = await db.execute(update(MediaObject).where(MediaObject.id == oid, _eligible(now), available).values(
        status="deleting", cleanup_token=token, cleanup_claimed_at=lease_time,
    ))
    if not held.rowcount:
        await db.rollback()
        return False
    owned = (MediaObject.id == oid, MediaObject.cleanup_token == token)
    obj = await db.get(MediaObject, oid, populate_existing=True)
    location = await db.get(MediaStorageLocation, obj.location_id)
    children = list(await db.scalars(update(MediaObject).where(MediaObject.parent_id == oid)
                                     .values(**_mark_values(now)).returning(MediaObject.id)))
    kind, volume, key = location.kind, obj.volume_id, obj.storage_key
    backend = locations.backend_for(db, location)
    await db.commit()
    confirmed = False
    try:
        for child_id in children:
            await _remove(db, child_id, now)
        # 每个位置单独清除；挂错卷的实例留下该对象，拥有正确卷的实例可在下轮接手。
        child_exists = await db.scalar(select(exists().where(MediaObject.parent_id == oid))) if children else False
        mismatched = kind == "local" and not volume and await locations.volumes_mismatch(db)
        await db.commit()
        if child_exists or (kind == "local" and volume and volume != await locations.local_volume_id(db)):
            return False
        if mismatched and await backend.stat(key) is None:
            return False
        await backend.delete(key)
        done = await db.execute(delete(MediaObject).where(*owned))
        await db.commit()
        confirmed = bool(done.rowcount)
        return confirmed
    finally:
        # 失败（含停止后台任务）释放自己的租约；崩溃或释放失败则按超时重新领取。
        if not confirmed:
            await db.rollback()
            await db.execute(update(MediaObject).where(*owned).values(cleanup_token=None, cleanup_claimed_at=None))
            await db.commit()


@dataclass
class SweepReport:
    removed: int = 0
    failed: int = 0


async def _collect(sessionmaker, ids, now, report):
    for oid in ids:
        async with sessionmaker() as db:
            try:
                if await _remove(db, oid, now):
                    report.removed += 1
            except Exception:
                report.failed += 1
                # 后端异常可能带签名与凭据，避免输出原始异常文本。
                logger.warning("媒体清理失败，下轮重试 object=%s", oid)


async def sweep(sessionmaker, *, now: datetime | None = None, object_ids: Iterable[str] | None = None) -> SweepReport:
    now = now or datetime.now(UTC)
    report = SweepReport()
    if object_ids is not None:
        await _collect(sessionmaker, object_ids, now, report)
    else:
        cursor = ""
        while True:
            async with sessionmaker() as db:
                ids = list(await db.scalars(select(MediaObject.id).where(MediaObject.id > cursor, _eligible(now))
                                            .order_by(MediaObject.id).limit(_BATCH)))
            if not ids:
                break
            await _collect(sessionmaker, ids, now, report)
            cursor = ids[-1]
    async with sessionmaker() as db:
        await db.execute(update(MediaStorageLocation).where(
            MediaStorageLocation.state == "readonly",
            ~exists().where(MediaObject.location_id == MediaStorageLocation.id),
        ).values(state="retired"))
        await db.commit()
    return report


@asynccontextmanager
async def maintenance(sessionmaker, worker, *, interval: float = 600):

    async def report():
        try:
            async with sessionmaker() as db:
                if worker.worker_key:
                    await locations.report_volume(db, worker.worker_key)
                    await db.commit()
        except Exception:
            logger.warning("媒体目录卷标识上报失败，下轮重试")

    async def run_sweeps():
        while True:
            try:
                await sweep(sessionmaker)
            except Exception:
                logger.warning("媒体清理暂时失败，下轮重试")
            await asyncio.sleep(interval)

    async def heartbeat():
        while True:
            await asyncio.sleep(interval)
            await report()

    await report()
    sweeper = asyncio.create_task(run_sweeps(), name="media-storage-maintenance")
    reporter = asyncio.create_task(heartbeat(), name="media-volume-report")
    try:
        yield
    finally:
        sweeper.cancel()
        reporter.cancel()
        await asyncio.gather(sweeper, reporter, return_exceptions=True)
        try:
            async with sessionmaker() as db:
                await db.execute(delete(MediaVolumeReport).where(MediaVolumeReport.worker_key == worker.worker_key))
                await db.commit()
        except Exception:
            logger.warning("媒体卷报告将在过期后移除")
