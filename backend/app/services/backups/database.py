"""PostgreSQL 原生快照与 SQLite 在线 backup API。同步函数需在线程中消费。"""

import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from app.services.backups.process import BackupAdapterError, check_running, run_command, tool_environment


@dataclass(frozen=True)
class SnapshotInfo:
    engine: str
    extension: str
    schema_revision: str | None = None


def database_engine(database_url: str) -> str:
    try:
        url = make_url(database_url)
        if url.get_backend_name() == "postgresql":
            return "postgresql"
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            if url.query.get("mode") != "memory" and not url.database.startswith("file:"):
                return "sqlite"
    except Exception:
        pass
    raise BackupAdapterError("database_unsupported")


def _create_private(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)


def _sqlite_snapshot(source: Path, destination: Path, deadline: float, stop: threading.Event) -> SnapshotInfo:
    # mode=ro保证不存在的源文件不会被SQLite悄悄创建为空数据库。
    with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2) as origin:
        with sqlite3.connect(destination) as output:
            def progress(_status: int, _remaining: int, _total: int):
                check_running(deadline, stop)

            origin.backup(output, pages=256, progress=progress, sleep=0.05)
            check_running(deadline, stop)
            # 大库校验也可能持续较久，不能只在backup阶段检查取消和截止时间。
            output.set_progress_handler(lambda: int(stop.is_set() or time.monotonic() >= deadline), 1000)
            try:
                if output.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise BackupAdapterError("snapshot_failed")
                revisions = output.execute("SELECT name FROM sqlite_master WHERE name='alembic_version'").fetchall()
                revision = output.execute("SELECT version_num FROM alembic_version").fetchone() if revisions else None
                check_running(deadline, stop)
            except sqlite3.OperationalError:
                check_running(deadline, stop)
                raise
    return SnapshotInfo("sqlite", ".sqlite3", str(revision[0]) if revision else None)


def _pgpass_part(value: str) -> str:
    if any(char in value for char in ("\r", "\n", "\x00")):
        raise BackupAdapterError("snapshot_failed")
    return value.replace("\\", "\\\\").replace(":", "\\:")


def snapshot_database(
    database_url: str, destination: Path, *, pg_dump_path: str = "pg_dump",
    deadline: float, stop_event: threading.Event,
) -> SnapshotInfo:
    """destination必须位于运行器的私有目录中且尚不存在；失败移除不完整产物。"""
    engine = database_engine(database_url)
    check_running(deadline, stop_event)
    created = False
    try:
        _create_private(destination)
        created = True
        if engine == "sqlite":
            return _sqlite_snapshot(Path(make_url(database_url).database), destination, deadline, stop_event)
        url = make_url(database_url)
        version = run_command([pg_dump_path, "--version"], deadline=deadline, stop_event=stop_event,
                              env=tool_environment(), capture_output=True)
        if not re.search(rb"pg_dump \(PostgreSQL\) (1[6-9]|[2-9][0-9])\.", version):
            raise BackupAdapterError("tool_unavailable")
        # 临时凭据位于与输出同一私有目录，工具结束（包括异常）立即清理。
        host, port = url.host or "localhost", str(url.port or 5432)
        user, database = url.username or "", url.database or ""
        line = ":".join(_pgpass_part(part) for part in (host, port, database, user, url.password or ""))
        fd, password_file = tempfile.mkstemp(prefix=".pgpass-", dir=destination.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(line + "\n")
            environment = tool_environment()
            environment["PGPASSFILE"] = password_file
            # 仅白名单TLS选项从受控部署URL转入环境；不继承PGPASSWORD或连接串。
            for parameter, variable in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"),
                                        ("sslcert", "PGSSLCERT"), ("sslkey", "PGSSLKEY")):
                if parameter in url.query:
                    value = url.query[parameter]
                    if not isinstance(value, str) or "\x00" in value:
                        raise BackupAdapterError("snapshot_failed")
                    environment[variable] = value
            run_command(
                [pg_dump_path, "--no-password", "--format=custom", "--host", host, "--port", port,
                 "--username", user, "--dbname", database, "--file", str(destination)],
                deadline=deadline, stop_event=stop_event, env=environment,
            )
            with destination.open("rb") as artifact:
                if artifact.read(5) != b"PGDMP":
                    raise BackupAdapterError("snapshot_failed")
            return SnapshotInfo("postgresql", ".dump")
        finally:
            Path(password_file).unlink(missing_ok=True)
    except BackupAdapterError:
        if created:
            destination.unlink(missing_ok=True)
        raise
    except Exception:
        if created and destination.exists() and stat.S_ISREG(destination.lstat().st_mode):
            destination.unlink(missing_ok=True)
        raise BackupAdapterError("snapshot_failed") from None
