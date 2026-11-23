"""SQLite 租约文件及主库的持久配对。生命周期锁使用 SQLite 自身的跨平台文件锁。"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import tempfile
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession


class SQLiteLeasePairError(RuntimeError):
    """配对无法证明，当前进程必须停止任务写入。"""


def _invalid() -> SQLiteLeasePairError:
    return SQLiteLeasePairError("SQLite 主库与租约文件配对无效；请停机保留整组文件，使用 sqlite_restore 恢复到新副本")


def _identity(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise _invalid() from exc
    if not stat.S_ISREG(info.st_mode) or not info.st_ino or info.st_nlink != 1:
        raise _invalid()
    return f"{info.st_dev}:{info.st_ino}"


def _connect(path: Path, mode: str, *, timeout: float = 5.0) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=timeout,
                           isolation_level=None, check_same_thread=False)


def _busy(exc: sqlite3.Error) -> bool:
    return getattr(exc, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


@contextmanager
def _pair_errors():
    try:
        yield
    except sqlite3.Error as exc:
        if _busy(exc):
            raise
        raise _invalid() from exc
    except OSError as exc:
        raise _invalid() from exc


@dataclass(frozen=True)
class _Binding:
    generation: str
    main_path: str
    main_file: str
    lock_file: str
    lease_file: str | None


_BINDING = "SELECT generation,main_path,main_file,lock_file,lease_file FROM task_lease_store WHERE id=1"
_SAVE = (
    "INSERT INTO task_lease_store(id,generation,main_path,main_file,lock_file,lease_file) "
    "VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,"
    "main_path=excluded.main_path,main_file=excluded.main_file,lock_file=excluded.lock_file,"
    "lease_file=excluded.lease_file"
)


def _load(db: sqlite3.Connection) -> _Binding | None:
    row = db.execute(_BINDING).fetchone()
    return _Binding(*row) if row else None


def _valid_generation(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _side(path: Path) -> Path:
    return Path(str(path) + ".leases.sqlite3")


def _lock(path: Path) -> Path:
    return Path(str(path) + ".leases.lock")


@dataclass
class SQLiteLeasePair:
    path: Path
    generation: str
    _physical_files: dict[Path, str]
    _handles: ExitStack | None

    def verify_files(self) -> None:
        if self._handles is None or any(_identity(path) != key for path, key in self._physical_files.items()):
            raise _invalid()

    def close(self) -> None:
        handles, self._handles = self._handles, None
        if handles is not None:
            handles.close()


def _create_file(path: Path) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        _identity(path)
    else:
        os.close(fd)


def _begin_guard(conn: sqlite3.Connection, *, exclusive: bool) -> None:
    # 默认的回滚日志模式中，读事务的共享锁与修复/初始化的独占锁互斥。
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
        raise _invalid()
    conn.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN")
    conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()


def _anchor(conn: sqlite3.Connection) -> str | None:
    if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='lease_anchor' AND type='table'").fetchone():
        row = conn.execute("SELECT generation FROM lease_anchor WHERE id=1").fetchone()
        if row is None or not _valid_generation(row[0]):
            raise _invalid()
        return row[0]
    if conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
        raise _invalid()
    return None


def _side_meta(conn: sqlite3.Connection) -> tuple:
    row = conn.execute("SELECT generation,main_path,main_file,lock_file FROM lease_pair WHERE id=1").fetchone()
    if row is None or not _valid_generation(row[0]):
        raise _invalid()
    return row


def _prepare_side(base: Path, generation: str, main_path: str, main_file: str, guard_key: str) -> str:
    side = _side(base)
    _create_file(side)
    key = _identity(side)
    with closing(_connect(side, "rw")) as conn:
        conn.execute("BEGIN IMMEDIATE")
        expected = (generation, main_path, main_file, guard_key)
        if conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is None:
            conn.execute("CREATE TABLE lease_pair(id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL,"
                         "main_path TEXT NOT NULL,main_file TEXT NOT NULL,lock_file TEXT NOT NULL)")
            conn.execute("INSERT INTO lease_pair VALUES(1,?,?,?,?)", expected)
            conn.execute("CREATE TABLE worker_leases(worker_key TEXT PRIMARY KEY NOT NULL,heartbeat_at TEXT NOT NULL,"
                         "stopped_at TEXT,fenced_at TEXT)")
        if _side_meta(conn) != expected:
            raise _invalid()
        conn.commit()
    if _identity(side) != key:
        raise _invalid()
    return key


def _attach(base: Path, binding: _Binding, *, memory: bool = False) -> SQLiteLeasePair:
    physical = {_lock(base): binding.lock_file, _side(base): binding.lease_file}
    if not memory:
        physical[base] = binding.main_file
    if not binding.lease_file or any(_identity(path) != expected for path, expected in physical.items()):
        raise _invalid()
    with ExitStack() as handles:
        guard = handles.enter_context(closing(_connect(_lock(base), "rw")))
        _begin_guard(guard, exclusive=False)
        if _anchor(guard) != binding.generation:
            raise _invalid()
        # 连接保持打开，以免在进程存活时已 unlink 的 inode 被复用于另一个库文件。
        if not memory:
            primary = handles.enter_context(closing(_connect(base, "ro")))
            if _load(primary) != binding:
                raise _invalid()
        side = handles.enter_context(closing(_connect(_side(base), "ro")))
        if _side_meta(side) != (binding.generation, binding.main_path, binding.main_file, binding.lock_file):
            raise _invalid()
        if side.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise _invalid()
        side.execute("SELECT worker_key,heartbeat_at,stopped_at,fenced_at FROM worker_leases LIMIT 0")
        pair = SQLiteLeasePair(_side(base), binding.generation, physical, handles.pop_all())
        try:
            pair.verify_files()
        except BaseException:
            pair.close()
            raise
        return pair


def _init_guard(guard: sqlite3.Connection, generation: str) -> None:
    recorded = _anchor(guard)
    if recorded is None:
        guard.execute("CREATE TABLE lease_anchor(id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL)")
        guard.execute("INSERT INTO lease_anchor VALUES(1,?)", (generation,))
    elif recorded != generation:
        raise _invalid()


def _bootstrap(main: sqlite3.Connection, base: Path, *, cold: bool = False) -> None:
    # 调用方只在无有效配对时进入；并发首次启动在独占锁内重读主库记录。
    guard_path = _lock(base)
    _create_file(guard_path)
    lock_key = _identity(guard_path)
    main_key = _identity(base)
    with closing(_connect(guard_path, "rw", timeout=0.15)) as guard:
        _begin_guard(guard, exclusive=True)
        main.execute("BEGIN IMMEDIATE")
        previous = _load(main)
        if previous and not cold and (previous.main_path != str(base) or previous.main_file != main_key
                                      or previous.lock_file != lock_key):
            raise _invalid()
        if previous and previous.lease_file and not cold:
            main.rollback()
            return
        if not cold and main.execute("SELECT 1 FROM task_workers WHERE stopped_at IS NULL "
                                     "AND fenced_at IS NULL LIMIT 1").fetchone():
            raise _invalid()
        if cold:
            main.execute("UPDATE task_workers SET fenced_at=CURRENT_TIMESTAMP "
                         "WHERE stopped_at IS NULL AND fenced_at IS NULL")
        established = _anchor(guard)
        generation = previous.generation if previous and not cold else str(uuid4())
        if previous is None and _side(base).exists():
            # 合法降级后再升级可重用同一组干净文件；从别处复制来的残留文件不能初始化本主库。
            with closing(_connect(_side(base), "ro")) as side:
                marker = _side_meta(side)
                if marker != (established, str(base), main_key, lock_key) or side.execute(
                        "SELECT 1 FROM worker_leases WHERE stopped_at IS NULL AND fenced_at IS NULL LIMIT 1"
                ).fetchone():
                    raise _invalid()
                generation = marker[0]
        if not _valid_generation(generation):
            raise _invalid()
        _init_guard(guard, generation)
        args = (generation, str(base), main_key, lock_key)
        main.execute(_SAVE, (*args, None))
        main.commit()
        # PREPARED 已落库。此处断电后相同物理主库可以继续按原 generation 创建空旁库。
        side_key = _prepare_side(base, *args)
        guard.commit()
        main.execute("BEGIN IMMEDIATE")
        if _identity(base) != main_key or _identity(guard_path) != lock_key or _identity(_side(base)) != side_key:
            raise _invalid()
        actual = _load(main)
        if actual == _Binding(*args, side_key):
            # 同代的另一启动者在本进程提交 guard 后已经补完 READY。
            main.rollback()
            return
        if actual != _Binding(*args, None):
            raise _invalid()
        main.execute("UPDATE task_lease_store SET lease_file=? WHERE id=1", (side_key,))
        main.commit()


def _disk_pair(path: Path) -> SQLiteLeasePair:
    with _pair_errors():
        path = path.resolve(strict=True)
    with _pair_errors(), closing(_connect(path, "rw")) as main:
        end = time.monotonic() + 8
        while True:
            binding = _load(main)
            if binding:
                if binding.main_path != str(path) or binding.main_file != _identity(path):
                    raise _invalid()
                if binding.lease_file is not None:
                    return _attach(path, binding)
                if binding.lock_file != _identity(_lock(path)):
                    raise _invalid()
            try:
                _bootstrap(main, path)
            except sqlite3.Error as exc:
                main.rollback()
                if not _busy(exc) or time.monotonic() >= end:
                    raise
                time.sleep(0.025)


_memory_temp: tempfile.TemporaryDirectory | None = None
_memory_temp_lock = threading.Lock()


def _memory_directory() -> tempfile.TemporaryDirectory:
    # 同时启动两个命名内存连接池时必须使用同一目录，包括进程的首次调用。
    global _memory_temp
    with _memory_temp_lock:
        if _memory_temp is None:
            _memory_temp = tempfile.TemporaryDirectory(prefix="ainovel-memory-leases-")
        return _memory_temp


def _memory_pair(binding: _Binding) -> SQLiteLeasePair:
    if not _valid_generation(binding.generation) or binding.main_path != ":memory:":
        raise _invalid()
    base = Path(_memory_directory().name) / binding.generation
    with _pair_errors():
        if binding.lease_file is not None:
            return _attach(base, binding, memory=True)
        _create_file(_lock(base))
        lock_key = _identity(_lock(base))
        end = time.monotonic() + 8
        while True:
            try:
                with closing(_connect(_lock(base), "rw", timeout=0.15)) as guard:
                    _begin_guard(guard, exclusive=False)
                    anchored = _anchor(guard)
                    guard.rollback()
                    if anchored is None:
                        _begin_guard(guard, exclusive=True)
                        _init_guard(guard, binding.generation)
                        _prepare_side(base, binding.generation, ":memory:", binding.main_file, lock_key)
                        guard.commit()
                    elif anchored != binding.generation:
                        raise _invalid()
                side_key = _identity(_side(base))
                return _attach(base, _Binding(binding.generation, ":memory:", binding.main_file, lock_key, side_key),
                               memory=True)
            except sqlite3.Error as exc:
                if not _busy(exc) or time.monotonic() >= end:
                    raise
                time.sleep(0.025)


async def _acquire_pair(func, *args) -> SQLiteLeasePair:
    # to_thread 不能取消运行中的文件操作。调用者取消启动时，漏掉返回值会泄漏生命周期锁。
    pending = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        def release(finished):
            if not finished.cancelled() and finished.exception() is None:
                finished.result().close()
        pending.add_done_callback(release)
        raise


async def _verify_current_db(db: AsyncSession, pair: SQLiteLeasePair) -> None:
    # 拒绝仍指向已替换文件的旧连接池。该会话在注册前使用，未携带业务事务。
    observed = (await db.execute(text(_BINDING))).one_or_none()
    await db.rollback()
    if observed is None or observed.generation != pair.generation or observed.lease_file != _identity(pair.path):
        raise _invalid()
    pair.verify_files()


async def _open_memory(db: AsyncSession) -> SQLiteLeasePair:
    # 不同 engine 通过同一内存主库行取得代次。纯内存库随进程退出而消失。
    row = (await db.execute(text(_BINDING))).one_or_none()
    if row is None:
        generation = str(uuid4())
        await db.execute(text(
            "INSERT OR IGNORE INTO task_lease_store(id,generation,main_path,main_file,lock_file) "
            "SELECT 1,:generation,':memory:',:generation,'' WHERE NOT EXISTS "
            "(SELECT 1 FROM task_workers WHERE stopped_at IS NULL AND fenced_at IS NULL)"
        ), {"generation": generation})
        await db.commit()
        row = (await db.execute(text(_BINDING))).one_or_none()
        if row is None:
            raise _invalid()
    await db.rollback()
    binding = _Binding(*row)
    pair = await _acquire_pair(_memory_pair, binding)
    try:
        if binding.lease_file is None:
            guard = _lock(Path(_memory_directory().name) / binding.generation)
            await db.execute(text("UPDATE task_lease_store SET lock_file=:guard,lease_file=:side "
                                  "WHERE id=1 AND generation=:key AND lease_file IS NULL"),
                             {"guard": _identity(guard), "side": _identity(pair.path), "key": pair.generation})
            await db.commit()
        await _verify_current_db(db, pair)
    except BaseException:
        pair.close()
        raise
    return pair


async def open_pair(db: AsyncSession) -> SQLiteLeasePair | None:
    """启动时用一个专用主库会话调用；返回的共享锁由 WorkerLease 持有至完全停止。"""
    if db.get_bind().dialect.name != "sqlite":
        return None
    entries = (await db.execute(text("PRAGMA database_list"))).all()
    main_path = next(path for _, name, path in entries if name == "main")
    await db.rollback()
    if main_path:
        pair = await _acquire_pair(_disk_pair, Path(main_path))
        try:
            await _verify_current_db(db, pair)
        except BaseException:
            pair.close()
            raise
        return pair
    # 共享内存的表锁为 SQLITE_LOCKED，驱动的 busy_timeout 不会等待。回滚后重新读取同一持久代次。
    end = time.monotonic() + 8
    while True:
        try:
            return await _open_memory(db)
        except OperationalError as exc:
            if not _busy(exc.orig) or time.monotonic() >= end:
                raise
            await db.rollback()
            await asyncio.sleep(0.025)


def _guard_offline(stack: ExitStack, path: Path, *, expected: _Binding | None = None) -> None:
    identity = _identity(path)
    if expected and identity != expected.lock_file:
        raise SQLiteLeasePairError("原锁文件与备份的配对身份不符")
    guard = stack.enter_context(closing(_connect(path, "rw", timeout=0)))
    try:
        _begin_guard(guard, exclusive=True)
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            raise SQLiteLeasePairError("原执行者仍持有租约锁，请停止全部旧进程") from exc
        raise
    generation = _anchor(guard)
    if not generation or (expected and expected.generation != generation) or identity != _identity(path):
        raise _invalid()


def guard_downgrade(path: Path) -> ExitStack:
    """持有生命周期独占锁到 Alembic 归还连接，覆盖旁库已启动、主库登记尚未提交的窗口。"""
    with _pair_errors(), ExitStack() as stack:
        with closing(_connect(path, "ro")) as main:
            binding = _load(main)
        if binding is None:
            return stack.pop_all()
        if not binding.lease_file or binding.main_file != _identity(path) or binding.main_path != str(path):
            raise _invalid()
        _guard_offline(stack, _lock(path), expected=binding)
        if binding.lease_file != _identity(_side(path)):
            raise _invalid()
        with closing(_connect(_side(path), "ro")) as side:
            if _side_meta(side) != (binding.generation, binding.main_path, binding.main_file, binding.lock_file):
                raise _invalid()
            if side.execute("SELECT 1 FROM worker_leases WHERE stopped_at IS NULL AND fenced_at IS NULL LIMIT 1"
                            ).fetchone():
                raise SQLiteLeasePairError("租约库中仍有未停止的执行者，拒绝降级")
        return stack.pop_all()


def initialize_cold_copy(path: Path, *, previous_lock: Path | None = None) -> None:
    """对处于最终位置的新物理恢复副本配对；调用者已确认原服务全部停机。"""
    path = path.resolve(strict=True)
    with _pair_errors(), ExitStack() as stack:
        main = stack.enter_context(closing(_connect(path, "rw")))
        if previous_lock is not None:
            _guard_offline(stack, previous_lock.resolve(strict=True))
        if not main.execute("SELECT 1 FROM sqlite_schema WHERE name='task_lease_store'").fetchone():
            # 旧备份走原 Alembic 升级路径，0066 首次启动时才需要配对文件。
            revision = main.execute("SELECT version_num FROM alembic_version").fetchone()
            if revision and revision[0].isdigit() and int(revision[0]) < 66:
                return
            raise _invalid()
        old = _load(main)
        if old and old.main_file == _identity(path):
            raise SQLiteLeasePairError("冷恢复只接受新物理副本，请保留原文件并复制备份至目标")
        for companion in (_side(path), _lock(path)):
            if any(os.path.lexists(str(companion) + ext) for ext in ("", "-journal", "-wal", "-shm")):
                raise SQLiteLeasePairError("冷恢复目标存在旧租约或锁文件，请先停机并完整移存旧文件")
        if old:
            origin_lock = _lock(Path(old.main_path))
            if origin_lock.exists() and not (previous_lock is not None and origin_lock.samefile(previous_lock)):
                _guard_offline(stack, origin_lock, expected=old)
        _bootstrap(main, path, cold=True)
        pair = _disk_pair(path)
        pair.close()
