"""SQLite 配对、文件替换、首次初始化中断及离线换代。"""

import asyncio
import shutil
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import TaskWorker
from app.models.entities import TaskLeaseStore
from app.services.task_engine import sqlite_lease_pair as pairing


async def _engine(url, **options):
    engine = create_async_engine(url, **options)
    async with engine.begin() as conn:
        for table in (TaskWorker.__table__, TaskLeaseStore.__table__):
            await conn.run_sync(table.create, checkfirst=True)
    return engine


async def _open(engine):
    async with async_sessionmaker(engine)() as db:
        return await pairing.open_pair(db)


@pytest.mark.parametrize("name", ["main", "side", "lock"])
async def test_replaced_file_blocks_old_and_new_pair(tmp_path, name):
    main = tmp_path / "novel.db"
    engine = await _engine(f"sqlite+aiosqlite:///{main}")
    try:
        pair = await _open(engine)
        try:
            changed = {"main": main, "side": pair.path, "lock": Path(str(main) + ".leases.lock")}[name]
            substitute = tmp_path / "byte-for-byte-copy"
            shutil.copyfile(changed, substitute)
            # Windows 的 SQLite 文件句柄在使用期间可直接阻止替换，这同样确保只有原配对生效。
            try:
                substitute.replace(changed)
            except PermissionError:
                pair.verify_files()
                return
            with pytest.raises(pairing.SQLiteLeasePairError):
                pair.verify_files()
            with pytest.raises(pairing.SQLiteLeasePairError):
                await _open(engine)
        finally:
            pair.close()
    finally:
        await engine.dispose()


async def test_missing_side_is_not_recreated_and_cold_copy_fences_old_workers(tmp_path):
    main = tmp_path / "novel.db"
    copy = tmp_path / "restored.db"
    engine = await _engine(f"sqlite+aiosqlite:///{main}")
    try:
        pair = await _open(engine)
        try:
            async with engine.begin() as db:
                await db.execute(text("INSERT INTO task_workers(worker_key,hostname,pid) VALUES(:k,'old',1)"),
                                 {"k": str(uuid4())})
            shutil.copyfile(main, copy)
            with pytest.raises(pairing.SQLiteLeasePairError, match="仍持有"):
                pairing.initialize_cold_copy(copy)
            copy_side = Path(str(copy) + ".leases.sqlite3")
            assert not await asyncio.to_thread(copy_side.exists)
        finally:
            pair.close()
        await engine.dispose()
        pair.path.unlink()
        with pytest.raises(pairing.SQLiteLeasePairError):
            await _open(engine)
        assert not pair.path.exists()
        pairing.initialize_cold_copy(copy)
        with closing(sqlite3.connect(copy)) as db:
            new = db.execute("SELECT generation FROM task_lease_store").fetchone()[0]
            assert new != pair.generation
            assert db.execute("SELECT fenced_at FROM task_workers").fetchone()[0] is not None
        with closing(sqlite3.connect(str(copy) + ".leases.sqlite3")) as db:
            assert db.execute("SELECT generation FROM lease_pair").fetchone() == (new,)
            assert db.execute("SELECT count(*) FROM worker_leases").fetchone() == (0,)
        with closing(sqlite3.connect(main)) as db:
            assert db.execute("SELECT fenced_at FROM task_workers").fetchone()[0] is None
    finally:
        await engine.dispose()


async def test_prepared_pair_can_finish_bootstrap_after_crash(tmp_path, monkeypatch):
    main = tmp_path / "novel.db"
    engine = await _engine(f"sqlite+aiosqlite:///{main}")
    try:
        with monkeypatch.context() as patch:
            def crash(*args, **kwargs):
                raise RuntimeError("中断首次创建旁库")
            patch.setattr(pairing, "_prepare_side", crash)
            with pytest.raises(RuntimeError, match="中断首次"):
                await _open(engine)
        async with engine.connect() as db:
            generation, marker = (await db.execute(text("SELECT generation,lease_file FROM task_lease_store"))).one()
            assert marker is None
        first, second = await asyncio.gather(_open(engine), _open(engine))
        try:
            assert first.generation == second.generation == generation
            first.verify_files()
            second.verify_files()
            with pytest.raises(pairing.SQLiteLeasePairError, match="仍持有"):
                pairing.guard_downgrade(main)
        finally:
            first.close()
            second.close()
        with pairing.guard_downgrade(main):
            pass
    finally:
        await engine.dispose()


async def test_memory_db_has_own_independent_lease_connection():
    engine = await _engine("sqlite+aiosqlite:///:memory:")
    try:
        pair = await _open(engine)
        pair.close()
        again = await _open(engine)
        try:
            assert pair.generation == again.generation
            with closing(sqlite3.connect(again.path)) as side:
                assert side.execute("SELECT count(*) FROM worker_leases").fetchone() == (0,)
        finally:
            again.close()
    finally:
        await engine.dispose()


async def test_named_shared_memory_uses_same_pair_across_engines():
    url = f"sqlite+aiosqlite:///file:lease-{uuid4().hex}?mode=memory&cache=shared&uri=true"
    engine = await _engine(url, poolclass=StaticPool)
    other = create_async_engine(url, poolclass=StaticPool)
    try:
        first, second = await asyncio.gather(_open(engine), _open(other))
        try:
            assert first.generation == second.generation and first.path == second.path
            first.verify_files()
            second.verify_files()
        finally:
            first.close()
            second.close()
    finally:
        await engine.dispose()
        await other.dispose()


async def test_concurrent_bootstrap_accepts_the_same_already_committed_binding(tmp_path, monkeypatch):
    main = tmp_path / "novel.db"
    url = f"sqlite+aiosqlite:///{main}"
    engine = await _engine(url)
    other = create_async_engine(url)
    guard_committed = threading.Event()
    release_first = threading.Event()
    connect = pairing._connect

    class PauseAfterGuardCommit:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def commit(self):
            self.db.commit()
            if not guard_committed.is_set():
                guard_committed.set()
                assert release_first.wait(8)

    def observe_guard(path, mode, **kwargs):
        db = connect(path, mode, **kwargs)
        return PauseAfterGuardCommit(db) if path == pairing._lock(main) else db

    monkeypatch.setattr(pairing, "_connect", observe_guard)
    first = asyncio.create_task(_open(engine))
    second_pair = None
    try:
        assert await asyncio.to_thread(guard_committed.wait, 5)
        # 首个进程已提交旁库和 guard，未得到最后的 READY 回执，另一独立连接先完成同一配对。
        second_pair = await _open(other)
    finally:
        release_first.set()
        try:
            first_pair = await first
            with closing(first_pair):
                assert second_pair is not None
                assert (first_pair.generation, first_pair.path) == (second_pair.generation, second_pair.path)
                first_pair.verify_files()
                second_pair.verify_files()
        finally:
            if second_pair is not None:
                second_pair.close()
            await engine.dispose()
            await other.dispose()


async def test_side_generation_changed_without_replacing_inode_fails_closed(tmp_path):
    from app.services.task_engine.sqlite_worker_leases import live

    main = tmp_path / "novel.db"
    engine = await _engine(f"sqlite+aiosqlite:///{main}")
    try:
        pair = await _open(engine)
        with closing(pair):
            with closing(sqlite3.connect(pair.path)) as side:
                side.execute("UPDATE lease_pair SET generation=?", (str(uuid4()),))
                side.commit()
            with pytest.raises(pairing.SQLiteLeasePairError):
                await live(pair, str(uuid4()), 30)
            with pytest.raises(pairing.SQLiteLeasePairError):
                await _open(engine)
    finally:
        await engine.dispose()


async def test_cancelling_start_releases_lock_returned_by_background_thread(tmp_path, monkeypatch):
    main = tmp_path / "novel.db"
    engine = await _engine(f"sqlite+aiosqlite:///{main}")
    acquired = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    acquire_pair = pairing._disk_pair
    close_pair = pairing.SQLiteLeasePair.close

    def acquire_and_wait(path):
        pair = acquire_pair(path)
        acquired.set()
        assert release.wait(8)
        return pair

    def observed_close(pair):
        close_pair(pair)
        closed.set()

    monkeypatch.setattr(pairing, "_disk_pair", acquire_and_wait)
    monkeypatch.setattr(pairing.SQLiteLeasePair, "close", observed_close)
    opening = asyncio.create_task(_open(engine))
    try:
        assert await asyncio.to_thread(acquired.wait, 5)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        release.set()
        assert await asyncio.to_thread(closed.wait, 5)
        with pairing.guard_downgrade(main):
            pass
    finally:
        release.set()
        await engine.dispose()
