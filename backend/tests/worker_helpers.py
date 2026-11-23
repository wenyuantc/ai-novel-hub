"""多引擎测试读取真实租约所在的数据库，并用数据库时钟控制过期时刻。"""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite
from sqlalchemy import func, select, update

from app.models import TaskWorker
from app.services.task_engine import sqlite_worker_leases, workers


@dataclass(frozen=True)
class WorkerFacts:
    worker_key: str
    heartbeat_at: datetime
    stopped_at: datetime | None
    fenced_at: datetime | None


@asynccontextmanager
async def _authority(engine, previous_pair=None):
    url = engine.ctx.sessionmaker.kw["bind"].url
    if url.get_backend_name() != "sqlite":
        yield None
        return
    pair = previous_pair or engine.lease.sqlite_pair
    assert pair is not None, "SQLite 引擎应已绑定租约侧库"
    # rw 防止路径或测试装配有误时创建一份空库。
    async with aiosqlite.connect(f"{pair.path.as_uri()}?mode=rw", uri=True) as db:
        async with db.execute("SELECT generation FROM lease_pair WHERE id = 1") as cursor:
            assert await cursor.fetchone() == (pair.generation,)
        yield db


async def worker_row(engine, key=None, *, previous_pair=None):
    key = key or engine.lease.worker_key
    async with _authority(engine, previous_pair) as side:
        if side is not None:
            async with side.execute(
                "SELECT heartbeat_at, stopped_at, fenced_at FROM worker_leases WHERE worker_key = ?", (key,),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None, f"权威侧库缺少执行者 {key}"
            heartbeat, stopped, fenced = row
            return WorkerFacts(key, datetime.fromisoformat(heartbeat),
                               datetime.fromisoformat(stopped) if stopped else None,
                               datetime.fromisoformat(fenced) if fenced else None)
        async with engine.ctx.sessionmaker() as db:
            row = await db.get(TaskWorker, key)
            return WorkerFacts(key, row.heartbeat_at, row.stopped_at, row.fenced_at)


async def freeze_heartbeat(engine):
    """让数据库停止收到原进程的心跳，保留挂起的实际执行体供测试迟到的写入。"""
    heartbeat = engine._heartbeat_task
    assert heartbeat is not None
    heartbeat.cancel()
    with suppress(asyncio.CancelledError):
        await heartbeat


async def age_heartbeat(engine, seconds):
    """将真实心跳设为数据库当前时间之前 seconds 秒，返回写入的时间供对照。"""
    key = engine.lease.worker_key
    async with _authority(engine) as side:
        if side is not None:
            async with side.execute(
                "UPDATE worker_leases SET heartbeat_at = datetime('now', ?) "
                "WHERE worker_key = ? RETURNING heartbeat_at",
                (f"-{seconds} seconds", key),
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            await side.commit()
            return datetime.fromisoformat(row[0])
        async with engine.ctx.sessionmaker() as db:
            now = await db.scalar(select(func.now()))
            before = now - timedelta(seconds=seconds)
            result = await db.execute(update(TaskWorker).where(TaskWorker.worker_key == key).values(
                heartbeat_at=before,
            ))
            assert result.rowcount == 1
            await db.commit()
            return before


async def expire_worker(engine):
    await age_heartbeat(engine, engine.lease_seconds + 10)


async def fence_worker(engine, *, mirror=True):
    """仅围栏原执行者。SQLite 可暂停在权威决定已提交、主库镜像尚未同步的窗口。"""
    await expire_worker(engine)
    if not mirror and engine.lease.sqlite_pair is not None:
        key = engine.lease.worker_key
        await sqlite_worker_leases.fence_expired(engine.lease.sqlite_pair, engine.lease_seconds)
        assert (await worker_row(engine, key)).fenced_at is not None
        async with engine.ctx.sessionmaker() as db:
            diagnostic = await db.get(TaskWorker, key)
            assert diagnostic.fenced_at is None and diagnostic.stopped_at is None
        return
    async with engine.ctx.sessionmaker() as db:
        await workers.prepare_orphan_scan(db, engine.lease_seconds)
        await db.commit()
