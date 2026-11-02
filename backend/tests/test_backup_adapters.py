"""备份适配器在真实文件、独立子进程及受控网络边界上的契约。"""

import asyncio
import os
import smtplib
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.services.backups.crypto import cleanup_encryption_home, encrypt_file
from app.services.backups.database import snapshot_database
from app.services.backups.mail import FailureNotice, MailConfig, send_failure_notification
from app.services.backups.network import NetworkPolicy
from app.services.backups.process import BackupAdapterError, run_blocking, run_command


def test_sqlite_snapshot_includes_committed_wal_and_integrity(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE content (id INTEGER PRIMARY KEY, body TEXT)")
        db.execute("INSERT INTO content VALUES (1, '备份已提交正文')")
        db.execute("CREATE TABLE alembic_version (version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('0030')")
        db.commit()
        assert Path(f"{source}-wal").exists()
        result = snapshot_database(
            f"sqlite+aiosqlite:///{source}", destination,
            pg_dump_path="pg_dump", deadline=time.monotonic() + 10, stop_event=threading.Event(),
        )
    assert result.engine == "sqlite"
    assert result.schema_revision == "0030"
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as restored:
        assert restored.execute("SELECT body FROM content").fetchone() == ("备份已提交正文",)
        assert restored.execute("PRAGMA quick_check").fetchone() == ("ok",)


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite://", "mysql://localhost/db"])
def test_snapshot_rejects_nonpersistent_or_unsupported_database(tmp_path: Path, url: str):
    with pytest.raises(BackupAdapterError):
        snapshot_database(url, tmp_path / "snapshot", pg_dump_path="pg_dump",
                          deadline=time.monotonic() + 2, stop_event=threading.Event())
    assert not (tmp_path / "snapshot").exists()


def test_command_deadline_reaps_actual_process(tmp_path: Path):
    pid_file = tmp_path / "pid"
    code = "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)"
    with pytest.raises(BackupAdapterError, match="超时"):
        run_command([sys.executable, "-c", code, str(pid_file)],
                    deadline=time.monotonic() + 0.4, stop_event=threading.Event())
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_cancelled_blocking_worker_is_drained_before_return():
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def worker(stop: threading.Event):
        entered.set()
        assert stop.wait(3)
        assert release.wait(3)
        finished.set()

    task = asyncio.create_task(run_blocking(worker))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.parametrize("address", ["169.254.169.254", "fe80::1", "224.0.0.1", "0.0.0.0"])
def test_private_network_opt_in_still_blocks_metadata_and_invalid_addresses(monkeypatch, address):
    monkeypatch.setattr("app.services.backups.network._resolve", lambda host, port: [address])
    with pytest.raises(BackupAdapterError):
        NetworkPolicy(allow_private=True).validate_endpoint("https://storage.example.test")


def test_network_checks_every_address_and_origin(monkeypatch):
    monkeypatch.setattr("app.services.backups.network._resolve", lambda host, port: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(BackupAdapterError):
        NetworkPolicy().validate_endpoint("https://storage.example.test")
    policy = NetworkPolicy(allow_private=True)
    origin = policy.validate_endpoint("https://storage.example.test/")
    policy.validate_request("https://storage.example.test/bucket/object?uploadId=known", origin)
    with pytest.raises(BackupAdapterError):
        policy.validate_request("https://other.example.test/bucket", origin)


@pytest.mark.parametrize("endpoint", ["http://example.test", "https://name:secret@example.test", "https://example.test?a=b"])
def test_network_rejects_unsafe_endpoint_syntax(endpoint):
    with pytest.raises(BackupAdapterError):
        NetworkPolicy().validate_endpoint(endpoint)


def test_snapshot_does_not_overwrite_or_remove_preexisting_output(tmp_path: Path):
    existing = tmp_path / "existing"
    existing.write_text("already safe")
    with pytest.raises(BackupAdapterError):
        snapshot_database(f"sqlite+aiosqlite:///{tmp_path / 'db'}", existing,
                          deadline=time.monotonic() + 1, stop_event=threading.Event())
    assert existing.read_text() == "already safe"


def test_pg_snapshot_password_only_in_private_temporary_pgpass(monkeypatch, tmp_path: Path):
    destination = tmp_path / "result.dump"
    captured = []

    def command(argv, **options):
        if "--version" in argv:
            return b"pg_dump (PostgreSQL) 16.4\n"
        assert "dangerous:password" not in repr(argv)
        environment = options["env"]
        assert all("dangerous:password" not in value for value in environment.values())
        credential_file = Path(environment["PGPASSFILE"])
        assert credential_file.stat().st_mode & 0o777 == 0o600
        assert credential_file.read_text() == "localhost:5432:platform:account:dangerous\\:password\n"
        captured.append(credential_file)
        destination.write_bytes(b"PGDMP" + b"test snapshot")
        return b""

    monkeypatch.setattr("app.services.backups.database.run_command", command)
    result = snapshot_database("postgresql+asyncpg://account:dangerous%3Apassword@localhost/platform", destination,
                               deadline=time.monotonic() + 5, stop_event=threading.Event())
    assert result.engine == "postgresql"
    assert len(captured) == 1 and not captured[0].exists()


def test_pg_snapshot_rejects_old_client_without_running_dump(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "app.services.backups.database.run_command", lambda *args, **kwargs: b"pg_dump (PostgreSQL) 15.9"
    )
    output = tmp_path / "result.dump"
    with pytest.raises(BackupAdapterError, match="工具不可用"):
        snapshot_database("postgresql+asyncpg://account:password@localhost/platform", output,
                          deadline=time.monotonic() + 2, stop_event=threading.Event())
    assert not output.exists()


def test_pg_snapshot_rejects_invalid_passfile_text_before_allocating_descriptor(monkeypatch, tmp_path: Path):
    import tempfile

    original = tempfile.mkstemp
    allocated = []

    def capture_file(*args, **kwargs):
        fd, path = original(*args, **kwargs)
        allocated.append((fd, Path(path)))
        return fd, path

    monkeypatch.setattr("app.services.backups.database.tempfile.mkstemp", capture_file)
    monkeypatch.setattr(
        "app.services.backups.database.run_command", lambda *args, **kwargs: b"pg_dump (PostgreSQL) 16.4\n"
    )
    try:
        with pytest.raises(BackupAdapterError):
            snapshot_database("postgresql+asyncpg://account:invalid%0Apassword@localhost/platform", tmp_path / "dump",
                              deadline=time.monotonic() + 2, stop_event=threading.Event())
        assert allocated == []
    finally:
        for fd, path in allocated:
            try:
                os.close(fd)
            except OSError:
                pass
            path.unlink(missing_ok=True)


def test_sqlite_snapshot_cancelled_during_validation_never_reports_success(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE content (id INTEGER PRIMARY KEY)")
    original = sqlite3.connect
    stop = threading.Event()

    class CancellingValidation(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement == "PRAGMA quick_check":
                stop.set()
            return super().execute(statement, *args, **kwargs)

    monkeypatch.setattr(
        "app.services.backups.database.sqlite3.connect",
        lambda *args, **kwargs: original(*args, **kwargs, factory=CancellingValidation),
    )
    destination = tmp_path / "snapshot.sqlite3"
    with pytest.raises(BackupAdapterError, match="中断"):
        snapshot_database(f"sqlite+aiosqlite:///{source}", destination,
                          deadline=time.monotonic() + 2, stop_event=stop)
    assert not destination.exists()


def test_gpg_password_uses_stdin_and_own_agent_is_reaped(monkeypatch, tmp_path: Path):
    source = tmp_path / "plain"
    output = tmp_path / "encrypted.gpg"
    source.write_bytes(b"private database")
    homes = []
    secret = "secret-backup-password"

    def command(argv, **options):
        assert secret not in repr(argv) and secret not in repr(options.get("env"))
        home = Path(argv[argv.index("--homedir") + 1])
        assert home.stat().st_mode & 0o777 == 0o700
        if "--symmetric" in argv:
            assert options["input_bytes"] == secret.encode() + b"\n"
            assert "--no-symkey-cache" in argv and argv[argv.index("--cipher-algo") + 1] == "AES256"
            output.write_bytes(b"opaque ciphertext")
            homes.append(home)
        else:
            assert "--kill" in argv and "gpg-agent" in argv and home == homes[0]
        return b""

    monkeypatch.setattr("app.services.backups.crypto.run_command", command)
    encrypt_file(source, output, secret, deadline=time.monotonic() + 5, stop_event=threading.Event())
    assert not source.exists() and output.exists()
    assert not homes[0].exists()
    assert output.stat().st_mode & 0o777 == 0o600


def test_gpg_supports_long_backup_directory_without_exceeding_agent_socket_limit(monkeypatch, tmp_path: Path):
    directory = tmp_path / ("long-backup-path-" * 8)
    directory.mkdir()
    source = directory / "plain.sqlite3"
    output = directory / "encrypted.sqlite3.gpg"
    source.write_bytes(b"synthetic database")
    killed_homes = []

    def command(argv, **options):
        home = Path(argv[argv.index("--homedir") + 1])
        if "--symmetric" in argv:
            # GPG2.2在容器中使用Unix socket；真实隔离验收已复现长homedir无法启动agent。
            if len(os.fsencode(home / "S.gpg-agent")) >= 104:
                raise BackupAdapterError("tool_failed")
            output.write_bytes(b"synthetic encrypted database")
        else:
            killed_homes.append(home)
        return b""

    monkeypatch.setattr("app.services.backups.crypto.run_command", command)
    encrypt_file(source, output, "synthetic-backup-password", deadline=time.monotonic() + 2,
                 stop_event=threading.Event())
    assert output.read_bytes() == b"synthetic encrypted database"
    assert not source.exists()
    assert len(killed_homes) == 1 and not killed_homes[0].exists()


@pytest.mark.parametrize("tamper", ["marker", "symlink", "mode"])
def test_gpg_crash_home_recovery_checks_exact_ownership(monkeypatch, tmp_path: Path, tamper):
    source, destination, locator = tmp_path / "plain", tmp_path / "encrypted", tmp_path / "home-locator"
    source.write_bytes(b"synthetic database")
    instance, execution = str(uuid4()), str(uuid4())
    # 在实际GPG调用边界硬退出，确保没有finally帮助测试回收已持久化的home/marker。
    script = """
import os, sys, threading, time
from pathlib import Path
from app.services.backups import crypto
def hard_exit(argv, **kwargs):
    Path(sys.argv[3]).write_text(argv[argv.index('--homedir') + 1])
    os._exit(0)
crypto.run_command = hard_exit
crypto.encrypt_file(Path(sys.argv[1]), Path(sys.argv[2]), 'synthetic-backup-password',
                    deadline=time.monotonic()+2, stop_event=threading.Event(),
                    instance_key=sys.argv[4], execution_key=sys.argv[5])
"""
    result = subprocess.run([sys.executable, "-c", script, str(source), str(destination), str(locator),
                             instance, execution], capture_output=True, timeout=5)
    assert result.returncode == 0
    home = Path(locator.read_text())
    marker = home / ".ainovel-backup-owner"
    owner = marker.read_bytes()
    assert home.stat().st_mode & 0o777 == 0o700
    assert marker.stat().st_mode & 0o777 == 0o600
    killed = []
    monkeypatch.setattr("app.services.backups.crypto.run_command", lambda argv, **kwargs: killed.append(argv))
    held = home.with_name(home.name + "-held")
    victim = tmp_path / "unrelated"
    victim.mkdir()
    (victim / "keep").write_text("unrelated data")
    try:
        if tamper == "marker":
            marker.write_bytes(b"another instance")
        elif tamper == "symlink":
            home.rename(held)
            home.symlink_to(victim, target_is_directory=True)
        else:
            home.chmod(0o777)
        with pytest.raises(BackupAdapterError):
            cleanup_encryption_home(instance, execution, deadline=time.monotonic() + 2,
                                    stop_event=threading.Event())
        assert killed == []
        assert (victim / "keep").read_text() == "unrelated data"
    finally:
        if home.is_symlink():
            home.unlink()
            held.rename(home)
        home.chmod(0o700)
        marker.write_bytes(owner)
        cleanup_encryption_home(instance, execution, deadline=time.monotonic() + 2,
                                stop_event=threading.Event())
    assert not home.exists()
    assert len(killed) == 1 and killed[0][2] == str(home)
    cleanup_encryption_home(instance, execution, deadline=time.monotonic() + 2, stop_event=threading.Event())
    assert len(killed) == 1


@pytest.mark.parametrize("result", ["sent", "disconnect", "reject", "partial", "close_error"])
def test_smtp_tls_and_result_are_separate_from_backup(monkeypatch, result):
    messages = []
    closed = []
    config = MailConfig("smtp.example.test", 465, "ssl", "sender@example.test", ("admin@example.test",),
                        username="smtp-secret-user", password="smtp-secret-password")

    class SMTP:
        def __init__(self, host, port, *, timeout, context):
            assert context.check_hostname
            assert timeout > 0

        def login(self, username, password):
            assert username == "smtp-secret-user" and password == "smtp-secret-password"

        def send_message(self, message, **kwargs):
            messages.append(message)
            if result == "disconnect":
                raise smtplib.SMTPServerDisconnected("upstream-secret-message")
            if result == "reject":
                raise smtplib.SMTPDataError(550, b"upstream-secret-message")
            return {"admin@example.test": (550, b"upstream-secret-message")} if result == "partial" else {}

        def close(self):
            closed.append(True)
            if result == "close_error":
                raise OSError("upstream-secret-close-error")

    monkeypatch.setattr("app.services.backups.mail.smtplib.SMTP_SSL", SMTP)
    monkeypatch.setattr("app.services.backups.network._resolve", lambda host, port: ["93.184.216.34"])
    notice = FailureNotice(str(uuid4()), 7, "partial", datetime.now(UTC), ("s3",), True)
    output = send_failure_notification(config, notice, NetworkPolicy(), deadline=time.monotonic() + 5,
                                       stop_event=threading.Event())
    assert output.status == {
        "sent": "sent", "disconnect": "unknown", "reject": "failed", "partial": "unknown", "close_error": "sent",
    }[result]
    assert "secret" not in repr(output) and "secret" not in messages[0].as_string()
    assert messages[0]["Message-ID"] == f"<backup-{notice.execution_key}@ainovel.invalid>"
    assert closed == [True]
    assert "secret" not in repr(config)


@pytest.mark.parametrize("interrupt", ["deadline", "cancel"])
def test_smtp_interrupts_blocked_send_and_drains_connection(monkeypatch, interrupt):
    entered = threading.Event()
    stop = threading.Event()
    local, peer = socket.socketpair()
    # 旧实现的逐次读超时明显长于总deadline；救援写入保证失败测试也自行结束。
    local.settimeout(3)
    closed = []

    class SMTP:
        def __init__(self, host, port, *, timeout, context):
            self.sock = local

        def send_message(self, *args, **kwargs):
            entered.set()
            if not self.sock.recv(1):
                raise smtplib.SMTPServerDisconnected("synthetic disconnect")
            return {}

        def close(self):
            local.close()
            closed.append(True)

    def release_if_stuck():
        if entered.wait(2):
            if interrupt == "cancel":
                stop.set()
            if not finished.wait(1.2):
                try:
                    peer.sendall(b"eventual reply")
                except OSError:
                    pass

    finished = threading.Event()
    rescue = threading.Thread(target=release_if_stuck)
    rescue.start()
    monkeypatch.setattr("app.services.backups.mail.smtplib.SMTP_SSL", SMTP)
    monkeypatch.setattr("app.services.backups.network._resolve", lambda host, port: ["93.184.216.34"])
    config = MailConfig("smtp.example.test", 465, "ssl", "sender@example.test", ("admin@example.test",))
    notice = FailureNotice(str(uuid4()), 7, "failed", datetime.now(UTC))
    started = time.monotonic()
    try:
        result = send_failure_notification(config, notice, NetworkPolicy(),
                                           deadline=started + (0.2 if interrupt == "deadline" else 5),
                                           stop_event=stop)
        assert result.status == "unknown"
        assert time.monotonic() - started < 1
        assert closed == [True]
    finally:
        finished.set()
        rescue.join(3)
        local.close()
        peer.close()


@pytest.mark.parametrize("tls_failure", [False, True])
def test_smtp_starttls_never_authenticates_or_sends_before_verified_tls(monkeypatch, tls_failure):
    events = []

    class SMTP:
        def __init__(self, host, port, *, timeout):
            events.append("connected")

        def ehlo(self):
            events.append("ehlo")

        def starttls(self, *, context):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            if tls_failure:
                raise ssl.SSLError("synthetic certificate failure")
            events.append("tls_verified")

        def login(self, username, password):
            assert "tls_verified" in events
            events.append("authenticated")

        def send_message(self, *args, **kwargs):
            assert "tls_verified" in events
            events.append("sent")
            return {}

        def close(self):
            events.append("closed")

    monkeypatch.setattr("app.services.backups.mail.smtplib.SMTP", SMTP)
    monkeypatch.setattr("app.services.backups.network._resolve", lambda host, port: ["93.184.216.34"])
    config = MailConfig("smtp.example.test", 587, "starttls", "sender@example.test", ("admin@example.test",),
                        username="synthetic-user", password="synthetic-password")
    notice = FailureNotice(str(uuid4()), 7, "failed", datetime.now(UTC))
    result = send_failure_notification(config, notice, NetworkPolicy(), deadline=time.monotonic() + 2,
                                       stop_event=threading.Event())
    assert result.status == ("failed" if tls_failure else "sent")
    if tls_failure:
        assert events == ["connected", "ehlo", "closed"]
    else:
        assert events == ["connected", "ehlo", "tls_verified", "ehlo", "authenticated", "sent", "closed"]
