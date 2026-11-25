"""SQLite 独立租约账簿。每次访问结束就释放连接，业务锁和网络请求不持有本库锁。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import closing, suppress

from app.services.task_engine.sqlite_lease_pair import SQLiteLeasePair, SQLiteLeasePairError

# 保留 SQLite 数据库时钟的毫秒。按整秒截断截止时间会把本轮刚过期的记录推迟到下一次轮询。
_NOW = "strftime('%Y-%m-%d %H:%M:%f','now')"


def _transaction[Result](pair: SQLiteLeasePair, write: bool, action: Callable[[sqlite3.Connection], Result]) -> Result:
    pair.verify_files()
    mode = "rw" if write else "ro"
    uri = f"{pair.path.as_uri()}?mode={mode}"
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            generation = db.execute("SELECT generation FROM lease_pair WHERE id = 1").fetchone()
            if generation != (pair.generation,):
                raise SQLiteLeasePairError("SQLite 租约文件代次与主库不一致")
            result = action(db)
            pair.verify_files()
            db.commit()
        pair.verify_files()
        return result
    except sqlite3.DatabaseError as exc:
        # 文件丢失、破损或打开了另一个代次时必须失败关闭；锁忙可在下个周期重试。
        code = getattr(exc, "sqlite_errorcode", 0) & 0xff
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        raise SQLiteLeasePairError("无法核验 SQLite 租约文件") from exc


async def register(pair: SQLiteLeasePair, key: str) -> None:
    def insert(db: sqlite3.Connection) -> None:
        db.execute(f"INSERT INTO worker_leases (worker_key, heartbeat_at) VALUES (?, {_NOW})", (key,))

    # 线程中的 SQLite 不会因协程取消而停止。先等 INSERT 落定，启动清理才能给同一身份写停止标记。
    inserting = asyncio.create_task(asyncio.to_thread(_transaction, pair, True, insert))
    try:
        await asyncio.shield(inserting)
    except asyncio.CancelledError:
        with suppress(Exception):
            await inserting
        raise


def _valid_parameters(key: str, seconds: float) -> tuple[str, str]:
    return key, f"-{seconds:g} seconds"


_LIVE = "stopped_at IS NULL AND fenced_at IS NULL AND julianday(heartbeat_at) >= julianday('now', ?)"
_VALID = f"worker_key = ? AND {_LIVE}"


async def live(pair: SQLiteLeasePair, key: str, seconds: float) -> bool:
    def check(db: sqlite3.Connection) -> bool:
        return db.execute("SELECT 1 FROM worker_leases WHERE " + _VALID, _valid_parameters(key, seconds)).fetchone() \
            is not None

    return await asyncio.to_thread(_transaction, pair, False, check)


async def live_heartbeats(pair: SQLiteLeasePair, keys: list[str], seconds: float) -> dict[str, str]:
    def lookup(db: sqlite3.Connection) -> dict[str, str]:
        result: dict[str, str] = {}
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            result.update(db.execute(
                f"SELECT worker_key, heartbeat_at FROM worker_leases WHERE worker_key IN ({placeholders}) AND {_LIVE}",
                [*batch, f"-{seconds:g} seconds"],
            ))
        return result

    return await asyncio.to_thread(_transaction, pair, False, lookup)


async def heartbeat(pair: SQLiteLeasePair, key: str, seconds: float) -> bool:
    def renew(db: sqlite3.Connection) -> bool:
        return db.execute(f"UPDATE worker_leases SET heartbeat_at = {_NOW} WHERE " + _VALID,
                          _valid_parameters(key, seconds)).rowcount == 1

    return await asyncio.to_thread(_transaction, pair, True, renew)


async def stop(pair: SQLiteLeasePair, key: str) -> None:
    def halt(db: sqlite3.Connection) -> None:
        db.execute(f"UPDATE worker_leases SET stopped_at = {_NOW} "
                   "WHERE worker_key = ? AND stopped_at IS NULL", (key,))

    await asyncio.to_thread(_transaction, pair, True, halt)


async def fence_expired(pair: SQLiteLeasePair, seconds: float, worker_key: str | None = None) -> None:
    def fence(db: sqlite3.Connection) -> None:
        statement = (f"UPDATE worker_leases SET fenced_at = {_NOW} WHERE stopped_at IS NULL AND fenced_at IS NULL "
                     "AND julianday(heartbeat_at) < julianday('now', ?)")
        parameters = [f"-{seconds:g} seconds"]
        if worker_key is not None:
            statement += " AND worker_key = ?"
            parameters.append(worker_key)
        db.execute(statement, parameters)

    await asyncio.to_thread(_transaction, pair, True, fence)


async def terminal_registrations(
    pair: SQLiteLeasePair, keys: list[str],
) -> list[tuple[str, str | None, str | None]]:
    def lookup(db: sqlite3.Connection) -> list[tuple[str, str | None, str | None]]:
        result = []
        for offset in range(0, len(keys), 500):
            batch = keys[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            result.extend(db.execute(
                f"SELECT worker_key, stopped_at, fenced_at FROM worker_leases WHERE worker_key IN ({placeholders}) "
                "AND (stopped_at IS NOT NULL OR fenced_at IS NOT NULL)", batch,
            ))
        return result

    return await asyncio.to_thread(_transaction, pair, False, lookup)


async def prune_terminal(pair: SQLiteLeasePair, retained: Iterable[str]) -> None:
    """启动时随主库登记清理旧的侧库孤儿。调用方持有主库登记写事务，排除并发登记。"""
    retained = set(retained)

    def prune(db: sqlite3.Connection) -> None:
        old = db.execute("SELECT worker_key FROM worker_leases WHERE "
                         "stopped_at < datetime(CURRENT_TIMESTAMP, '-7 days') OR "
                         "fenced_at < datetime(CURRENT_TIMESTAMP, '-7 days')")
        stale = [(key,) for key, in old if key not in retained]
        db.executemany("DELETE FROM worker_leases WHERE worker_key = ?", stale)

    await asyncio.to_thread(_transaction, pair, True, prune)
