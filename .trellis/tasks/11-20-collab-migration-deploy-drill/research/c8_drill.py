"""C8: 用虚构数据和专属资源演练 0043 旧版升级及回滚。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import tarfile
import time
from contextlib import AsyncExitStack, asynccontextmanager, closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import httpx
import uvicorn
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

ROOT = next(path for path in Path(__file__).resolve().parents if path.name == ".trellis").parent
OLD = "4501f18da97502eb3e352bf85b1853bb0903d60e"
NEW = "b7b3d248a7462deedd0507275a0e6fd0a6ec8e0e"
API = "/api/v1"
PASSWORD = "c8-fictional-pass-123"
AUTHOR = "c8_author"
MEMBER = "c8_member"
ADMIN = "c8_admin"
MODEL_KEY = "c8-fictional-provider-key"
ANALYSIS = json.dumps(
    {
        "summary": "虚构章节摘要",
        "plot_points": [],
        "hooks": [],
        "conflict": {"level": 1, "types": []},
        "emotion": {"tone": "平静", "start": "平静", "middle": "平静", "end": "平静"},
        "character_changes": [],
        "relationship_changes": [],
        "career_changes": [],
        "organization_changes": [],
        "foreshadows": [],
        "scores": {"pacing": 7, "engagement": 7, "coherence": 7},
        "suggestions": [],
    },
    ensure_ascii=False,
)
STARTED = time.monotonic()
EVENTS: list[dict] = []

GUARDIAN_SOURCE = r"""import os, signal, socket, sys
fd = int(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
peer = socket.socket(fileno=fd)
stream = peer.makefile("rb")
token = stream.readline().rstrip(b"\n")
peer.sendall(str(os.getpid()).encode() + b" " + token + b"\n")
while True:
    line = stream.readline()
    if not line:
        break
    if line == b"ping " + token + b"\n":
        peer.sendall(b"ok\n")
    elif line == b"term " + token + b"\n":
        peer.sendall(b"term\n")
        os.killpg(os.getpgrp(), signal.SIGTERM)
    elif line == b"kill " + token + b"\n":
        peer.sendall(b"kill\n")
        os.killpg(os.getpgrp(), signal.SIGKILL)
    elif line == b"quit " + token + b"\n":
        peer.sendall(b"bye\n")
        break
    else:
        peer.sendall(b"denied\n")
"""

SPAWN_SOURCE = r"""import os, subprocess, sys
fd, guardian = int(sys.argv[1]), sys.argv[2]
subprocess.Popen([sys.executable, "-c", guardian, str(fd)], pass_fds=(fd,),
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.close(fd)
os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
"""


def mark(label: str, **details) -> None:
    item = {
        "phase": label,
        "timestamp_utc": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "elapsed_s": round(time.monotonic() - STARTED, 3),
        **details,
    }
    EVENTS.append(item)
    print(json.dumps(item, ensure_ascii=False), flush=True)


DOCKER_ENV = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "DOCKER_API_VERSION",
    "XDG_RUNTIME_DIR",
    "SSH_AUTH_SOCK",
)


def safe_env(*, docker: bool = False) -> dict[str, str]:
    names = ("PATH", "HOME", "TMPDIR", *DOCKER_ENV) if docker else ("PATH", "HOME", "TMPDIR")
    result = {key: os.environ[key] for key in names if key in os.environ}
    result.update(LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8", UV_NO_CONFIG="1", UV_NO_PROGRESS="1", NO_PROXY="*")
    return result


@dataclass
class OwnedProcess:
    """子进程句柄与唯一的私有守护通道；守护在同一新会话中固定进程组。"""

    raw: asyncio.subprocess.Process
    guardian_pid: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    token: str
    cleaned: bool = False

    @property
    def pid(self) -> int:
        return self.raw.pid

    @property
    def returncode(self) -> int | None:
        return self.raw.returncode

    def kill(self) -> None:
        self.raw.kill()

    def terminate(self) -> None:
        self.raw.terminate()

    async def wait(self) -> int:
        return await self.raw.wait()

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        return await self.raw.communicate(data)


async def spawn_owned_process(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    stdin=None,
    stdout=None,
    stderr=None,
    pass_fds: tuple[int, ...] = (),
    on_started=None,
) -> OwnedProcess:
    """仅向守护传私有 fd；目标进程经 exec 保留实际 PID，不继承守护凭证。"""
    parent, child = socket.socketpair()
    parent.setblocking(False)
    token = secrets.token_hex(32)

    async def start() -> OwnedProcess:
        raw = None
        writer = None
        try:
            raw = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                SPAWN_SOURCE,
                str(child.fileno()),
                GUARDIAN_SOURCE,
                *args,
                cwd=cwd,
                env=env if env is not None else safe_env(),
                stdin=stdin if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                pass_fds=(*pass_fds, child.fileno()),
                start_new_session=True,
            )
            child.close()
            if on_started is not None:
                on_started()
            reader, writer = await asyncio.open_connection(sock=parent)
            writer.write((token + "\n").encode())
            await writer.drain()
            hello = (await asyncio.wait_for(reader.readline(), 10)).decode().strip().split(" ")
            if len(hello) != 2 or not secrets.compare_digest(hello[1], token):
                raise RuntimeError("guardian_authentication_failed")
            guardian_pid = int(hello[0])
            if os.getpgid(guardian_pid) != raw.pid or os.getsid(guardian_pid) != raw.pid:
                raise RuntimeError("guardian_group_mismatch")
            return OwnedProcess(raw, guardian_pid, reader, writer, token)
        except BaseException:
            # 无法认证整个组时不能向 PGID 发送信号；只处理仍由此句柄拥有的组长。
            if raw is not None:
                mark("进程组清理未确认", pid=raw.pid, reason="guardian_start_failed")
                if raw.returncode is None:
                    try:
                        raw.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(raw.wait(), 2)
                except TimeoutError:
                    pass
            if writer is not None:
                writer.close()
            parent.close()
            child.close()
            raise

    task = asyncio.create_task(start())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 即使创建时被取消，也先取得真实句柄并按相同归属规则清理。
        try:
            process = await task
        except Exception:
            raise
        await stop_application_group(process, grace_s=0.5)
        raise


async def guardian_request(process: OwnedProcess, action: str, expected: bytes) -> None:
    try:
        if os.getpgid(process.guardian_pid) != process.pid or os.getsid(process.guardian_pid) != process.pid:
            raise RuntimeError("guardian_group_mismatch")
        process.writer.write(f"{action} {process.token}\n".encode())
        await asyncio.wait_for(process.writer.drain(), 3)
        response = await asyncio.wait_for(process.reader.readline(), 3)
        if response != expected:
            raise RuntimeError("guardian_authentication_failed")
    except (OSError, TimeoutError, ValueError) as exc:
        raise RuntimeError("guardian_unavailable") from exc


async def group_members(pgid: int) -> set[int]:
    """只读取 PID、PGID 与状态，不读取命令或进程环境。"""
    probe = await asyncio.create_subprocess_exec(
        "ps",
        "-A",
        "-o",
        "pid=",
        "-o",
        "pgid=",
        "-o",
        "stat=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=safe_env(),
    )
    try:
        output, _ = await asyncio.wait_for(probe.communicate(), 5)
    except BaseException:
        if probe.returncode is None:
            probe.kill()
            await asyncio.wait_for(probe.wait(), 2)
        raise
    if probe.returncode != 0:
        raise RuntimeError("process_group_query_failed")
    result = set()
    for line in output.decode().splitlines():
        parts = line.split()
        if len(parts) != 3:
            raise RuntimeError("process_group_query_invalid")
        if int(parts[1]) == pgid and not parts[2].startswith("Z"):
            result.add(int(parts[0]))
    return result


async def stop_application_group(process: OwnedProcess, *, grace_s: float = 30) -> None:
    """确认守护后，由守护自身安全地向本组发送信号；组长退出也继续清理。"""
    if process.cleaned:
        return
    try:
        await guardian_request(process, "ping", b"ok\n")
        await guardian_request(process, "term", b"term\n")
        deadline = time.monotonic() + grace_s
        while True:
            members = await group_members(process.pid)
            if not (members - {process.guardian_pid}):
                await guardian_request(process, "quit", b"bye\n")
                break
            if time.monotonic() >= deadline:
                await guardian_request(process, "ping", b"ok\n")
                await guardian_request(process, "kill", b"kill\n")
                break
            await asyncio.sleep(0.05)
        async with asyncio.timeout(10):
            while True:
                if not await group_members(process.pid):
                    break
                await asyncio.sleep(0.05)
        await asyncio.wait_for(process.raw.wait(), 10)
        process.writer.close()
        await process.writer.wait_closed()
        process.cleaned = True
    except BaseException:
        mark(
            "进程组清理未确认",
            pid=process.pid,
            guardian_pid=process.guardian_pid,
            reason="ownership_or_exit_unverified",
        )
        raise


async def cleanup_owned(process: OwnedProcess, *, grace_s: float = 30) -> None:
    task = asyncio.create_task(stop_application_group(process, grace_s=grace_s))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    input_data: bytes | None = None,
    allowed: tuple[int, ...] = (0,),
    limit_s: float = 180,
) -> tuple[int, bytes, bytes]:
    process = await spawn_owned_process(
        args,
        cwd=cwd,
        env=env if env is not None else safe_env(docker=Path(args[0]).name == "docker"),
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(input_data), limit_s)
    finally:
        await cleanup_owned(process, grace_s=0.5)
    if process.returncode not in allowed:
        raise RuntimeError(f"command_failed:{Path(args[0]).name}:{process.returncode}")
    return process.returncode, out, err


async def prepare(root: Path) -> dict[str, Path]:
    result = {}
    for label, commit in (("old", OLD), ("new", NEW)):
        directory = root / label
        directory.mkdir(mode=0o700)
        _, archive, _ = await command(["git", "archive", commit, "backend"], cwd=ROOT)
        archive_path = root / f"{label}.tar"
        archive_path.write_bytes(archive)
        with tarfile.open(archive_path) as source:
            source.extractall(directory, filter="data")
        backend = directory / "backend"
        assert not (backend / ".env").exists()
        await command(["uv", "sync", "--frozen", "--python", "3.12"], cwd=backend, env=safe_env(), limit_s=300)
        _, python_version, _ = await command([str(backend / ".venv/bin/python"), "--version"], env=safe_env())
        result[label] = backend
        mark(
            "隔离版本就绪",
            version=label,
            commit=commit,
            lock_sha256=hashlib.sha256((backend / "uv.lock").read_bytes()).hexdigest(),
            python_version=python_version.decode().strip(),
        )
    return result


OWNER_LABEL = "ainovel.c8.owner"


async def remove_postgres_container(name: str, owner: str, cid: str | None, *, run=None) -> None:
    """创建未返回 ID 时也按随机名称、专属标签查找并验证归属后清理。"""
    if run is None:
        run = command
    remove_exit = None
    creation_id_known = cid is not None

    async def listed() -> list[tuple[str, str]]:
        _, output, _ = await run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"], limit_s=30)
        result = []
        for line in output.decode().splitlines():
            parts = line.split("\t")
            if len(parts) != 2 or len(parts[0]) != 64 or any(ch not in "0123456789abcdef" for ch in parts[0]):
                raise ValueError("Docker 容器清单格式错误")
            result.append((parts[0], parts[1]))
        return result

    def failed(reason: str, ident: str | None) -> None:
        mark("专属数据库清理失败", container=name, container_id=ident, reason=reason, remove_exit=remove_exit)
        raise RuntimeError(f"container_cleanup_unverified:{name}:{ident or 'id_unknown'}")

    try:
        matches = []
        # docker run 中断时引擎可能已创建容器但尚未返回 ID；连续查询短暂窗口。
        attempts = 5 if cid is None else 1
        for attempt in range(attempts):
            rows = await listed()
            matches = [(ident, actual_name) for ident, actual_name in rows if actual_name == name or ident == cid]
            if matches or attempt == attempts - 1:
                break
            await asyncio.sleep(0.5)
    except Exception:
        failed("query_failed", cid)
    if len(matches) > 1 or (matches and cid is not None and matches[0][0] != cid):
        failed("identity_conflict", cid)
    if not matches and not creation_id_known:
        # 短时间未找到不能证明被取消的创建请求不会迟到，保留随机名称供人工核查。
        failed("creation_outcome_unconfirmed", None)
    if matches:
        actual_id = matches[0][0]
        try:
            _, label, _ = await run(
                ["docker", "inspect", "--format", '{{index .Config.Labels "ainovel.c8.owner"}}', actual_id], limit_s=30
            )
            label_value = label.decode().strip()
        except Exception:
            failed("label_query_failed", actual_id)
        if label_value != owner:
            failed("not_owned", actual_id)
        try:
            remove_exit, _, _ = await run(["docker", "rm", "-f", "-v", actual_id], allowed=(0, 1), limit_s=30)
        except Exception:
            pass
        try:
            rows = await listed()
        except Exception:
            failed("query_failed_after_remove", actual_id)
        if any(ident == actual_id or actual_name == name for ident, actual_name in rows):
            failed("still_exists", actual_id)
        cid = actual_id
    mark(
        "专属数据库清理完成",
        container=name,
        container_id=cid,
        remove_exit=remove_exit,
        verified_absent=True,
        creation_id_known=creation_id_known,
        absence_checks=attempt + 1,
    )


@asynccontextmanager
async def postgres():
    password = secrets.token_hex(16)
    name = f"ainovel-c8-{uuid4().hex}"
    owner = secrets.token_hex(32)
    cid = None
    try:
        code, output, _ = await command(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "-d",
                "--name",
                name,
                "--label",
                f"{OWNER_LABEL}={owner}",
                "--tmpfs",
                "/var/lib/postgresql/data:rw,size=1g",
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_USER=c8probe",
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-e",
                "POSTGRES_DB=c8base",
                "postgres:16-alpine",
            ]
        )
        assert code == 0
        candidate = output.decode().strip()
        assert len(candidate) == 64 and all(char in "0123456789abcdef" for char in candidate)
        cid = candidate
        _, mount_output, _ = await command(["docker", "inspect", "--format", "{{json .Mounts}}", cid])
        _, tmpfs_output, _ = await command(["docker", "inspect", "--format", "{{json .HostConfig.Tmpfs}}", cid])
        mounts, tmpfs = json.loads(mount_output), json.loads(tmpfs_output)
        assert isinstance(tmpfs, dict) and "/var/lib/postgresql/data" in tmpfs
        assert isinstance(mounts, list) and not any(mount.get("Type") == "volume" for mount in mounts)
        mark("专属数据库存储就绪", container=name, data_mount="tmpfs", anonymous_volumes=0)
        _, output, _ = await command(["docker", "port", cid, "5432/tcp"])
        port = int(output.decode().strip().rsplit(":", 1)[1])
        async with asyncio.timeout(60):
            while True:
                try:
                    conn = await asyncpg.connect(
                        host="127.0.0.1", port=port, user="c8probe", password=password, database="c8base", timeout=2
                    )
                    try:
                        if await conn.fetchval("SELECT 1") == 1:
                            break
                    finally:
                        await conn.close()
                except (OSError, asyncpg.PostgresError, TimeoutError):
                    await asyncio.sleep(0.3)
        _, version, _ = await command(["docker", "exec", cid, "postgres", "--version"])
        _, image_id, _ = await command(["docker", "inspect", "--format", "{{.Image}}", cid])
        actual_image = image_id.decode().strip()
        assert len(actual_image) == 71 and actual_image.startswith("sha256:")
        assert all(char in "0123456789abcdef" for char in actual_image.removeprefix("sha256:"))
        mark(
            "专属数据库就绪", engine="postgres", version=version.decode().strip(), container=name, image_id=actual_image
        )
        yield {"cid": cid, "password": password, "port": port, "database": "c8base"}
    finally:
        cleanup = asyncio.create_task(remove_postgres_container(name, owner, cid))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


def url_for(root: Path, shape: str, pg: dict | None, name: str) -> str:
    if shape == "sqlite":
        return f"sqlite+aiosqlite:///{root / (name + '.sqlite3')}"
    assert pg is not None and name.replace("_", "").isalnum()
    return f"postgresql+asyncpg://c8probe:{pg['password']}@127.0.0.1:{pg['port']}/{name}"


async def snapshot(root: Path, shape: str, pg: dict | None, source: str, name: str) -> Path:
    target = root / (name + (".sqlite3" if shape == "sqlite" else ".dump"))
    assert not target.exists()
    if shape == "sqlite":
        path = Path(source.removeprefix("sqlite+aiosqlite:///"))
        with closing(sqlite3.connect(path)) as original, closing(sqlite3.connect(target)) as copy:
            original.backup(copy)
            assert copy.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    else:
        assert pg is not None
        db = urlsplit(source).path.lstrip("/")
        _, output, _ = await command(
            [
                "docker",
                "exec",
                pg["cid"],
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -Fc "$1"',
                "sh",
                db,
            ]
        )
        target.write_bytes(output)
    assert target.stat().st_size > 0
    mark("快照", name=name, bytes=target.stat().st_size, sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    return target


async def restore(root: Path, shape: str, pg: dict | None, source: Path, name: str) -> str:
    url = url_for(root, shape, pg, name)
    if shape == "sqlite":
        target = Path(url.removeprefix("sqlite+aiosqlite:///"))

        def restore_file():
            assert not target.exists()
            shutil.copyfile(source, target)
            with closing(sqlite3.connect(target)) as conn:
                assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]

        await asyncio.to_thread(restore_file)
    else:
        assert pg is not None
        await command(
            [
                "docker",
                "exec",
                pg["cid"],
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" exec createdb -h 127.0.0.1 -U "$POSTGRES_USER" '
                '--maintenance-db=postgres --template=template0 -- "$1"',
                "sh",
                name,
            ]
        )
        await command(
            [
                "docker",
                "exec",
                "-i",
                pg["cid"],
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" '
                '--exit-on-error --no-owner --no-privileges --dbname="$1"',
                "sh",
                name,
            ],
            input_data=await asyncio.to_thread(source.read_bytes),
        )
    return url


async def query(url: str, sql: str, *args):
    if url.startswith("sqlite"):
        with closing(sqlite3.connect(Path(url.removeprefix("sqlite+aiosqlite:///")))) as conn:
            return conn.execute(sql, args).fetchall()
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return [tuple(row) for row in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


async def version(url: str) -> str:
    rows = await query(url, "SELECT version_num FROM alembic_version")
    assert len(rows) == 1
    return rows[0][0]


def app_env(root: Path, backend: Path, url: str, key: str, jwt: str) -> dict[str, str]:
    env = safe_env()
    env.update(
        PYTHONPATH=str(backend),
        DATABASE_URL=url,
        JWT_SECRET=jwt,
        ENCRYPTION_KEY=key,
        ADMIN_USERNAME=ADMIN,
        ADMIN_PASSWORD=PASSWORD,
        REGISTRATION_ENABLED="true",
        AUTO_ANALYZE_ENABLED="false",
        TASK_MAX_CONCURRENCY="2",
        TASK_MAX_PER_USER="2",
        ALLOW_PRIVATE_AI_ENDPOINTS="true",
        BACKUP_DIR=str(root / "backups"),
        FRONTEND_DIST_DIR=str(root / "no-frontend"),
        TASK_PEER_BIND_HOST="127.0.0.1",
        TASK_PEER_ADVERTISE_HOST="127.0.0.1",
    )
    return env


async def migrate(
    backend: Path, env: dict, direction: str = "upgrade", target: str = "head", allowed: tuple[int, ...] = (0,)
) -> tuple[int, bytes, bytes]:
    return await command(
        [str(backend / ".venv/bin/python"), "-m", "alembic", direction, target],
        cwd=backend,
        env=env,
        allowed=allowed,
        limit_s=240,
    )


@asynccontextmanager
async def application(root: Path, backend: Path, env: dict, label: str):
    with socket.socket() as sock, (root / f"{label}.log").open("w+") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        process = await spawn_owned_process(
            [
                str(backend / ".venv/bin/python"),
                "-m",
                "uvicorn",
                "app.main:app",
                "--fd",
                str(sock.fileno()),
                "--no-access-log",
            ],
            cwd=backend,
            env=env,
            stdout=log,
            stderr=log,
            pass_fds=(sock.fileno(),),
            on_started=sock.close,
        )
        try:
            async with asyncio.timeout(90):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError(f"application_start_failed:{label}:{process.returncode}")
                    try:
                        async with httpx.AsyncClient(base_url=base, timeout=1, trust_env=False) as client:
                            if (await client.get("/healthz")).status_code == 200:
                                break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
            mark("应用启动", version=label, pid=process.pid)
            yield base, process
        finally:
            await cleanup_owned(process)
            mark("应用退出", version=label, pid=process.pid, exit_code=process.returncode)


def chunk(delta: dict, finish: str | None = None) -> str:
    data = {
        "id": "c8-model",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "c8-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


class Call:
    def __init__(self, *, gated: bool = False, text: str = "虚构生成正文", tool: bool = False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.text = text
        self.tool = tool
        if not gated:
            self.release.set()


class Model:
    def __init__(self):
        self.queue: asyncio.Queue[Call] = asyncio.Queue()
        self.calls: list[Call] = []
        self.unexpected = 0
        self.valid_keys = []
        self.autoreply = False

    def expect(self, **kwargs) -> Call:
        call = Call(**kwargs)
        self.queue.put_nowait(call)
        self.calls.append(call)
        return call

    async def chat(self, request):
        self.valid_keys.append(request.headers.get("authorization") == f"Bearer {MODEL_KEY}")
        if self.queue.empty():
            if self.autoreply:
                self.expect()
            else:
                self.unexpected += 1
                return JSONResponse({"error": "unexpected"}, status_code=400)
        call = await self.queue.get()

        async def stream():
            try:
                call.entered.set()
                await call.release.wait()
                if call.tool:
                    tool = {
                        "index": 0,
                        "id": "c8-tool",
                        "type": "function",
                        "function": {"name": "create_character", "arguments": '{"name":"虚构角色"}'},
                    }
                    yield chunk({"tool_calls": [tool]}, "tool_calls")
                else:
                    yield chunk({"content": call.text})
                    yield chunk({}, "stop")
                yield "data: [DONE]\n\n"
            finally:
                call.closed.set()

        return StreamingResponse(stream(), media_type="text/event-stream")


@asynccontextmanager
async def fake_model(model: Model):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = Starlette(routes=[Route("/v1/chat/completions", model.chat, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(1000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("fake model server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        for call in model.calls:
            call.release.set()
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


def checked(response: httpx.Response, expected: int):
    if response.status_code != expected:
        raise RuntimeError(
            f"http_unexpected:{response.request.method}:{response.request.url.path}:"
            f"{response.status_code}:expected_{expected}"
        )
    return response.json() if response.content else None


async def login(base: str, username: str) -> httpx.AsyncClient:
    client = httpx.AsyncClient(base_url=base, timeout=30, trust_env=False)
    checked(await client.post(API + "/auth/login", json={"username": username, "password": PASSWORD}), 200)
    return client


async def task_state(
    client: httpx.AsyncClient, ident: int, statuses: set[str], limit_s: int = 45, task_key: str | None = None
) -> dict:
    async with asyncio.timeout(limit_s):
        while True:
            result = checked(
                await client.get(f"{API}/tasks/{ident}", params={"task_key": task_key} if task_key else {}), 200
            )
            if result["status"] in statuses:
                return result
            await asyncio.sleep(0.1)


async def register(client: httpx.AsyncClient, username: str) -> dict:
    return checked(await client.post(API + "/auth/register", json={"username": username, "password": PASSWORD}), 201)


async def project(client: httpx.AsyncClient, title: str) -> dict:
    return checked(
        await client.post(
            API + "/projects", json={"title": title, "genre": "科幻", "premise": "虚构演练作品", "style": "平实"}
        ),
        201,
    )


async def chapter(client: httpx.AsyncClient, pid: int, title: str, content: str = "") -> dict:
    return checked(await client.post(f"{API}/projects/{pid}/chapters", json={"title": title, "content": content}), 201)


async def configure(client: httpx.AsyncClient, model_url: str) -> dict:
    return checked(
        await client.post(
            API + "/ai-configs",
            json={
                "name": "C8 本地模型",
                "provider": "openai_compat",
                "base_url": model_url + "/v1/",
                "api_key": MODEL_KEY,
                "model": "c8-model",
            },
        ),
        201,
    )


async def generate(
    client: httpx.AsyncClient, model: Model, chapter_id: int, *, gated: bool = False
) -> tuple[dict, Call]:
    call = model.expect(gated=gated)
    result = checked(await client.post(f"{API}/chapters/{chapter_id}/generate", json={}), 202)
    await asyncio.wait_for(call.entered.wait(), 40)
    if not gated:
        result = await task_state(client, result["id"], {"succeeded", "failed"}, task_key=result.get("task_key"))
        assert result["status"] == "succeeded"
    return result, call


@asynccontextmanager
async def maintenance_proxy(target: str):
    """测试代理：普通业务写入返回 503，保留查询、登录和任务排空入口。"""
    upstream = httpx.AsyncClient(base_url=target, timeout=30, trust_env=False, follow_redirects=False)

    async def proxy(request):
        path = request.url.path
        allowed = (
            request.method in {"GET", "HEAD"}
            or path == API + "/auth/login"
            or path.startswith(API + "/admin/")
            or (path.startswith(API + "/tasks/") and path.endswith(("/cancel", "/finish")))
        )
        if not allowed:
            return JSONResponse({"detail": "C8 测试维护中"}, status_code=503)
        headers = {
            key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length"}
        }
        upstream_result = await upstream.request(
            request.method, request.url.path, params=request.query_params, content=await request.body(), headers=headers
        )
        from starlette.responses import Response

        excluded = {"content-length", "content-encoding", "transfer-encoding", "connection"}
        response = Response(upstream_result.content, status_code=upstream_result.status_code)
        response.raw_headers = [
            (key, value) for key, value in upstream_result.headers.raw if key.decode().lower() not in excluded
        ]
        return response

    app = Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(1000):
            if server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("maintenance proxy did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        await upstream.aclose()
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


async def seed_old(root: Path, backend: Path, env: dict, model: Model, model_url: str) -> dict:
    values = {}
    async with application(root, backend, env, "old-seed") as (base, _), AsyncExitStack() as stack:
        author = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=30, trust_env=False))
        member = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=30, trust_env=False))
        values["author"] = await register(author, AUTHOR)
        values["member"] = await register(member, MEMBER)
        values["author_config"] = await configure(author, model_url)
        values["member_config"] = await configure(member, model_url)
        values["project"] = await project(author, "C8 旧作者作品")
        values["member_project"] = await project(member, "C8 旧成员作品")
        values["knowledge_project"] = await project(author, "C8 旧知识库作品")
        p = values["project"]["id"]
        q = values["member_project"]["id"]
        values["finished_chapter"] = await chapter(author, p, "已完成的生成")
        values["finished_task"], _ = await generate(author, model, values["finished_chapter"]["id"])
        uploaded = checked(
            await author.post(
                f"{API}/projects/{p}/kb/documents",
                files={
                    "file": ("c8-fiction.md", "# 虚构设定\n\n这是仅供隔离演练使用的知识库。".encode(), "text/markdown")
                },
            ),
            202,
        )
        values["kb"] = uploaded["document"]
        kb = await task_state(author, uploaded["task"]["id"], {"succeeded", "failed"})
        assert kb["status"] == "succeeded"
        agent_base = f"{API}/projects/{p}/agent"
        values["conversation"] = checked(await author.post(agent_base + "/conversations", json={}), 201)
        agent_url = agent_base + f"/conversations/{values['conversation']['id']}"
        proposal_call = model.expect(tool=True)
        sent = checked(await author.post(agent_url + "/messages", json={"content": "请提出一个虚构角色"}), 202)
        await asyncio.wait_for(proposal_call.entered.wait(), 40)
        values["agent_task"] = await task_state(author, sent["task"]["id"], {"paused", "failed"})
        assert values["agent_task"]["status"] == "paused"
        conversation = checked(await author.get(agent_url), 200)
        assert conversation["changes"] and conversation["changes"][0]["status"] == "proposed"
        values["proposal"] = conversation["changes"][0]
        values["batch_first"] = await chapter(author, p, "批量第一章")
        values["batch_second"] = await chapter(author, p, "批量第二章")
        batch_call = model.expect()
        batch = checked(
            await author.post(
                f"{API}/projects/{p}/batch-generate",
                json={
                    "start_chapter_id": values["batch_first"]["id"],
                    "end_chapter_id": values["batch_second"]["id"],
                    "auto_continue": False,
                    "review_every": 1,
                },
            ),
            202,
        )
        await asyncio.wait_for(batch_call.entered.wait(), 40)
        values["paused_batch"] = await task_state(author, batch["id"], {"paused", "failed"})
        assert values["paused_batch"]["status"] == "paused"
        values["interrupted_chapter"] = await chapter(member, q, "将被中断的生成")
        values["interrupted_task"], interrupted_call = await generate(
            member, model, values["interrupted_chapter"]["id"], gated=True
        )
        assert (await task_state(member, values["interrupted_task"]["id"], {"running"}))["status"] == "running"
        mark(
            "旧版业务数据创建",
            users=2,
            projects=3,
            finished=1,
            agent_proposals=len(conversation["changes"]),
            knowledge_document=1,
            paused_batch=1,
            interrupted_inflight=1,
        )
    await asyncio.wait_for(interrupted_call.closed.wait(), 10)
    return values


async def active_old_snapshot(
    root: Path, shape: str, pg: dict | None, url: str, backend: Path, env: dict, model: Model, data: dict
) -> tuple[Path, Path]:
    async with application(root, backend, env, "old-drain") as (base, _), AsyncExitStack() as stack:
        author, member, admin = [await login(base, name) for name in (AUTHOR, MEMBER, ADMIN)]
        for client in (author, member, admin):
            stack.push_async_callback(client.aclose)
        interrupted = await task_state(member, data["interrupted_task"]["id"], {"interrupted", "failed"})
        assert interrupted["status"] == "interrupted"
        p, q = data["project"]["id"], data["member_project"]["id"]
        data["analysis_first"] = await chapter(author, p, "分析一", "虚构的第一段已保存正文。")
        data["analysis_second"] = await chapter(author, p, "分析二", "虚构的第二段已保存正文。")
        analysis_call = model.expect(gated=True, text=ANALYSIS)
        data["analysis_running"] = checked(
            await author.post(f"{API}/chapters/{data['analysis_first']['id']}/analyze"), 202
        )
        await asyncio.wait_for(analysis_call.entered.wait(), 40)
        data["analysis_queued"] = checked(
            await author.post(f"{API}/chapters/{data['analysis_second']['id']}/analyze"), 202
        )
        assert (await task_state(author, data["analysis_queued"]["id"], {"queued"}))["status"] == "queued"
        data["generating_chapter"] = await chapter(member, q, "运行中的章节")
        data["queued_chapter"] = await chapter(member, q, "排队中的章节")
        data["generating_task"], generation_call = await generate(
            member, model, data["generating_chapter"]["id"], gated=True
        )
        assert (await task_state(member, data["generating_task"]["id"], {"running"}))["status"] == "running"
        data["queued_generation"] = checked(
            await member.post(f"{API}/chapters/{data['queued_chapter']['id']}/generate", json={}), 202
        )
        assert (await task_state(member, data["queued_generation"]["id"], {"queued"}))["status"] == "queued"
        queued_kb = checked(
            await author.post(
                f"{API}/projects/{data['knowledge_project']['id']}/kb/documents",
                files={"file": ("queued.md", "# 虚构排队知识库\n\n这是本地演练数据。".encode(), "text/markdown")},
            ),
            202,
        )
        data["kb_queued"], data["kb_queued_document"] = queued_kb["task"], queued_kb["document"]
        assert (await task_state(author, data["kb_queued"]["id"], {"queued"}))["status"] == "queued"
        overview = checked(await admin.get(API + "/admin/tasks"), 200)
        mark(
            "未排空旧版清单",
            counts=overview["counts"],
            task_ids={
                "analysis_running": data["analysis_running"]["id"],
                "analysis_queued": data["analysis_queued"]["id"],
                "generating": data["generating_task"]["id"],
                "paused_batch": data["paused_batch"]["id"],
                "queued_generation": data["queued_generation"]["id"],
                "paused_agent": data["agent_task"]["id"],
                "interrupted": data["interrupted_task"]["id"],
                "kb_queued": data["kb_queued"]["id"],
            },
        )
        undrained = await snapshot(root, shape, pg, url, "undrained")
        undrained_clone = await restore(root, shape, pg, undrained, "undrained_verify")
        assert await version(undrained_clone) == "0043"
        async with maintenance_proxy(base) as proxy_base, AsyncExitStack() as proxy_stack:
            writer, operator, member_proxy = [await login(proxy_base, name) for name in (AUTHOR, ADMIN, MEMBER)]
            for client in (writer, operator, member_proxy):
                proxy_stack.push_async_callback(client.aclose)
            denied = await writer.post(f"{API}/chapters/{data['analysis_second']['id']}/generate", json={})
            assert denied.status_code == 503
            settings = checked(await operator.get(API + "/admin/backups/settings"), 200)
            settings = checked(
                await operator.patch(
                    API + "/admin/backups/settings",
                    json={"revision": settings["revision"], "changes": {"schedule_enabled": False}},
                ),
                200,
            )
            assert settings["schedule_enabled"] is False
            backup = checked(await operator.get(API + "/admin/backups"), 200)
            assert backup["active_run"] is None
            cancelled = checked(await member_proxy.post(f"{API}/tasks/{data['queued_generation']['id']}/cancel"), 200)
            assert cancelled["status"] == "cancelled"
            cancelled_kb = checked(await writer.post(f"{API}/tasks/{data['kb_queued']['id']}/cancel"), 200)
            assert cancelled_kb["status"] == "cancelled"
            # 释放先前已受理任务，继续等待第二个同键任务。
            next_call = model.expect(text=ANALYSIS)
            analysis_call.release.set()
            await asyncio.wait_for(next_call.entered.wait(), 40)
            for task_name in ("analysis_running", "analysis_queued"):
                finished = await task_state(author, data[task_name]["id"], {"succeeded", "failed"})
                mark("旧版任务收尾", kind=task_name, status=finished["status"])
                assert finished["status"] == "succeeded"
            generation_call.release.set()
            for task_name in ("generating_task",):
                finished = await task_state(member, data[task_name]["id"], {"succeeded", "failed"})
                mark("旧版任务收尾", kind=task_name, status=finished["status"])
                assert finished["status"] == "succeeded"
            for key in ("paused_batch", "agent_task"):
                checked(await writer.post(f"{API}/tasks/{data[key]['id']}/cancel"), 200)
            async with asyncio.timeout(40):
                while True:
                    listing = checked(await operator.get(API + "/admin/tasks"), 200)
                    counts = listing["counts"]
                    if all(counts.get(status, 0) == 0 for status in ("queued", "running", "paused")):
                        break
                    await asyncio.sleep(0.1)
            before_stop = await snapshot(root, shape, pg, url, "before_stop")
            before_verify = await restore(root, shape, pg, before_stop, "before_stop_verify")
            assert await version(before_verify) == "0043"
            mark(
                "旧版已排空",
                counts=counts,
                scheduled_backup=False,
                active_backup=False,
                maintenance_proxy_denied_new_write=True,
            )
    final_snapshot = await snapshot(root, shape, pg, url, "final_old")
    restored = await restore(root, shape, pg, final_snapshot, "final_old_verify")
    assert await version(restored) == "0043"
    return undrained, final_snapshot


async def fingerprint(url: str, pg: dict | None) -> dict:
    """比较完整应用表的结构、行数与行内容摘要，不输出业务正文。"""
    if url.startswith("sqlite"):
        with closing(sqlite3.connect(Path(url.removeprefix("sqlite+aiosqlite:///")))) as conn:
            schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            tables = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            ]
            rows = {}
            for table in tables:
                safe = table.replace('"', '""')
                values = conn.execute(f'SELECT * FROM "{safe}"').fetchall()
                hashes = sorted(hashlib.sha256(repr(row).encode()).hexdigest() for row in values)
                rows[table] = [len(values), hashlib.sha256("".join(hashes).encode()).hexdigest()]
            revision = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        return {"revision": revision, "schema": hashlib.sha256(repr(schema).encode()).hexdigest(), "tables": rows}
    assert pg is not None
    name = urlsplit(url).path.lstrip("/")
    _, schema, _ = await command(
        [
            "docker",
            "exec",
            pg["cid"],
            "sh",
            "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" '
            '--schema-only --no-owner --no-privileges --restrict-key=c8drill "$1"',
            "sh",
            name,
        ]
    )
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        names = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        rows = {}
        for row in names:
            table = row["tablename"]
            safe = table.replace('"', '""')
            count, checksum = await conn.fetchrow(
                f"SELECT count(*), md5(coalesce(string_agg(md5(row_to_json(t)::text), '' "
                f"ORDER BY md5(row_to_json(t)::text)), '')) FROM \"{safe}\" t"
            )
            rows[table] = [count, checksum]
        revision = await conn.fetchval("SELECT version_num FROM alembic_version")
        return {"revision": revision, "schema": hashlib.sha256(schema).hexdigest(), "tables": rows}
    finally:
        await conn.close()


async def cold_restore(root: Path, backend: Path, env: dict, snapshot_file: Path, name: str) -> str:
    target = root / (name + ".sqlite3")
    assert not target.exists()
    await command(
        [
            str(backend / ".venv/bin/python"),
            "-m",
            "app.services.backups.sqlite_restore",
            str(snapshot_file),
            str(target),
        ],
        cwd=backend,
        env=env,
    )
    return f"sqlite+aiosqlite:///{target}"


def guard_reason(error: bytes) -> str:
    message = error.decode(errors="replace")
    candidates = (
        "仍有排空设置覆盖",
        "仍有排空设置审计",
        "尚有活动操作或待核对的归属",
        "尚有工作或待核对的归属",
        "仍有未停止的执行者",
        "任务占用的归属尚未确认",
        "仍有 AI 用量、价格或预算数据",
        "仍有缓存命中定价或用量记录",
        "仍有带实例身份的生成记录",
        "存在分析索引去重收据",
        "仍有未完成的分析或索引任务",
        "仍有模型实例绑定或未决执行事实",
        "仍有智能体提案身份",
        "仍有生成中章节或章节占用身份",
        "仍有协作授权或审计数据",
    )
    return next((reason for reason in candidates if reason in message), "其他保护")


async def upgrade_clean(
    root: Path,
    shape: str,
    pg: dict | None,
    url: str,
    old: Path,
    new: Path,
    key: str,
    jwt: str,
    model: Model,
    data: dict,
    clean: Path,
) -> None:
    new_env = app_env(root, new, url, key, jwt)
    await migrate(new, new_env)
    assert await version(url) == "0070"
    old_log = await query(
        url,
        "SELECT count(*) FROM generation_logs g JOIN projects p ON p.id=g.project_id "
        "WHERE g.actor_user_id IS NULL OR g.actor_user_id<>p.user_id",
    )
    proposals = await query(
        url, "SELECT count(*),sum(CASE WHEN change_key IS NULL OR change_key='' THEN 1 ELSE 0 END) FROM agent_changes"
    )
    configurations = await query(
        url, "SELECT count(*),sum(CASE WHEN instance_key IS NULL OR instance_key='' THEN 1 ELSE 0 END) FROM ai_configs"
    )
    assert old_log[0][0] == 0 and proposals[0][0] >= 1 and proposals[0][1] == 0
    assert configurations[0][0] >= 2 and configurations[0][1] == 0
    mark(
        "旧数据迁移后核对",
        revision="0070",
        legacy_non_owner_logs=old_log[0][0],
        agent_proposals=proposals[0][0],
        model_configs=configurations[0][0],
    )
    async with application(root, new, new_env, "new-clean") as (base, _), AsyncExitStack() as stack:
        author, member = [await login(base, name) for name in (AUTHOR, MEMBER)]
        for client in (author, member):
            stack.push_async_callback(client.aclose)
        p = data["project"]["id"]
        current = checked(await author.get(f"{API}/projects/{p}"), 200)
        assert current["title"] == "C8 旧作者作品"
        old_chapter = checked(await author.get(f"{API}/projects/{p}/chapters/{data['finished_chapter']['id']}"), 200)
        assert old_chapter["content"] == "虚构生成正文"
        shared_chapter = await chapter(author, p, "C8 新版协作章节")
        invite = checked(
            await author.post(
                f"{API}/projects/{p}/collaborators",
                json={"username": MEMBER, "role": "editor", "allow_owner_ai": False},
            ),
            201,
        )
        checked(
            await member.post(
                f"{API}/collaboration/invitations/project/{invite['invitation_id']}/accept",
                json={"expected_revision": invite["revision"]},
            ),
            200,
        )
        own_call, own_model = await generate(author, model, shared_chapter["id"])
        guest_call, guest_model = await generate(member, model, shared_chapter["id"])
        log_url = f"{API}/projects/{p}/chapters/{shared_chapter['id']}/generation-logs"
        author_logs = checked(await author.get(log_url), 200)
        member_logs = checked(await member.get(log_url), 200)
        assert len(author_logs) == 1 and len(member_logs) == 1
        assert author_logs[0]["id"] != member_logs[0]["id"]
        data["shared_chapter"] = shared_chapter
        data["guest_log"] = member_logs[0]["id"]
        data["marker"] = await project(author, "C8 升级后新增作品")
        mark(
            "新版业务验证",
            author_login=True,
            old_project=True,
            old_chapter=True,
            invitation=True,
            author_task=own_call["status"],
            collaborator_task=guest_call["status"],
            separate_personal_logs=True,
            author_log_http=200,
            member_log_http=200,
            project_id=p,
            chapter_id=shared_chapter["id"],
            author_id=data["author"]["id"],
            member_id=data["member"]["id"],
            author_log_ids=[item["id"] for item in author_logs],
            member_log_ids=[item["id"] for item in member_logs],
            real_model_calls=own_model.entered.is_set() and guest_model.entered.is_set(),
        )
    new_snapshot = await snapshot(root, shape, pg, url, "after_upgrade")
    if shape == "sqlite":
        protected = await cold_restore(root, new, new_env, new_snapshot, "protected")
    else:
        protected = await restore(root, shape, pg, new_snapshot, "protected")
    protected_env = app_env(root, new, protected, key, jwt)
    before = await fingerprint(protected, pg)
    code, _, error = await migrate(new, protected_env, "downgrade", "0043", allowed=(0, 1))
    after = await fingerprint(protected, pg)
    if before != after:
        changed = sorted(
            name
            for name in before["tables"].keys() | after["tables"].keys()
            if before["tables"].get(name) != after["tables"].get(name)
        )
        mark(
            "降级对照差异",
            schema_equal=before["schema"] == after["schema"],
            changed_tables=changed,
            exit_code=code,
            revision=await version(protected),
        )
    guard = guard_reason(error)
    protected_revision = await version(protected)
    mark(
        "新版数据降级保护",
        rejected=code != 0,
        first_guard=guard,
        revision=protected_revision,
        schema_unchanged=before["schema"] == after["schema"],
        table_count=len(before["tables"]),
        all_rows_unchanged=before["tables"] == after["tables"],
        before=before,
        after=after,
    )
    assert code != 0 and before == after and protected_revision == "0070"
    # 新旧代码不能混跑；这里仅在新版退出后对新库克隆执行故意的反例。
    if shape == "sqlite":
        incompatible = await cold_restore(root, new, new_env, new_snapshot, "incompatible")
    else:
        incompatible = await restore(root, shape, pg, new_snapshot, "incompatible")
    old_env = app_env(root, old, incompatible, key, jwt)
    observation = {"started": False, "author_login": False, "old_owner_sees_guest_log": None}
    try:
        async with application(root, old, old_env, "old-on-new") as (base, _):
            observation["started"] = True
            author = await login(base, AUTHOR)
            try:
                observation["author_login"] = True
                response = await author.get(
                    f"{API}/projects/{data['project']['id']}/chapters/{data['shared_chapter']['id']}/generation-logs"
                )
                observation["log_http"] = response.status_code
                if response.status_code == 200:
                    observation["old_owner_log_ids"] = [item["id"] for item in response.json()]
                    observation["old_owner_sees_guest_log"] = any(
                        item["id"] == data["guest_log"] for item in response.json()
                    )
            finally:
                await author.aclose()
    except (RuntimeError, httpx.HTTPError, TimeoutError):
        pass
    mark("旧代码直连新版库反例", **observation)
    if shape == "sqlite":
        main_file = root / "c8base.sqlite3"
        saved = root / "sqlite-new-group"

        def preserve_sqlite_files():
            saved.mkdir()
            for path in (main_file, Path(str(main_file) + ".leases.sqlite3"), Path(str(main_file) + ".leases.lock")):
                for suffix in ("", "-wal", "-shm", "-journal"):
                    candidate = Path(str(path) + suffix)
                    if candidate.exists():
                        candidate.rename(saved / candidate.name)
            previous = saved / (main_file.name + ".leases.lock")
            assert (saved / main_file.name).exists() and previous.exists()
            return previous

        previous_lock = await asyncio.to_thread(preserve_sqlite_files)
        await command(
            [
                str(new / ".venv/bin/python"),
                "-m",
                "app.services.backups.sqlite_restore",
                str(clean),
                str(main_file),
                "--previous-lock",
                str(previous_lock),
            ],
            cwd=new,
            env=new_env,
        )
        rolled_back = url
    else:
        rolled_back = await restore(root, shape, pg, clean, "rollback_old")
    assert await version(rolled_back) == "0043"
    old_env = app_env(root, old, rolled_back, key, jwt)
    async with application(root, old, old_env, "old-restored") as (base, _), AsyncExitStack() as stack:
        author = await login(base, AUTHOR)
        stack.push_async_callback(author.aclose)
        restored = checked(await author.get(f"{API}/projects/{data['project']['id']}"), 200)
        assert restored["title"] == "C8 旧作者作品"
        rows = await query(rolled_back, "SELECT count(*) FROM projects WHERE title='C8 升级后新增作品'")
        assert rows[0][0] == 0
        restored_chapter = await chapter(author, data["project"]["id"], "C8 回滚后任务")
        restored_task, _ = await generate(author, model, restored_chapter["id"])
        assert model.valid_keys[-1]
        mark(
            "快照回滚旧版验证",
            revision="0043",
            old_login=True,
            old_project=True,
            new_project_absent=True,
            original_encryption_key_and_model_credential=True,
            task=restored_task["status"],
        )


async def tasks_by_id(client: httpx.AsyncClient) -> dict[int, dict]:
    tasks = checked(await client.get(API + "/tasks", params={"limit": 200}), 200)
    return {task["id"]: task for task in tasks}


async def legacy_scenarios(
    root: Path, shape: str, pg: dict | None, new: Path, key: str, jwt: str, model: Model, data: dict, undrained: Path
) -> None:
    for action in (
        "observe",
        "resume",
        "finish",
        "owner_cancel",
        "chapter_owner_cancel",
        "admin_cancel",
        "retry_interrupted",
        "kb_reindex",
    ):
        url = await restore(root, shape, pg, undrained, f"legacy_{action}")
        env = app_env(root, new, url, key, jwt)
        await migrate(new, env)
        calls_before = len(model.valid_keys)
        async with application(root, new, env, "legacy-" + action) as (base, _), AsyncExitStack() as stack:
            author, member, admin = [await login(base, name) for name in (AUTHOR, MEMBER, ADMIN)]
            for client in (author, member, admin):
                stack.push_async_callback(client.aclose)
            await asyncio.sleep(2)
            owner_tasks, member_tasks = await tasks_by_id(author), await tasks_by_id(member)
            ids = {
                name: data[name]["id"]
                for name in (
                    "analysis_running",
                    "analysis_queued",
                    "generating_task",
                    "queued_generation",
                    "paused_batch",
                    "kb_queued",
                )
            }
            actual = {
                name: (
                    owner_tasks
                    if name in {"analysis_running", "analysis_queued", "paused_batch", "kb_queued"}
                    else member_tasks
                )[ident]["status"]
                for name, ident in ids.items()
            }
            chapters = await query(
                url,
                f"SELECT id,status FROM chapters WHERE id IN ({data['generating_chapter']['id']},"
                f"{data['queued_chapter']['id']}) ORDER BY id",
            )
            kb_state = await query(url, f"SELECT status FROM kb_documents WHERE id={data['kb_queued_document']['id']}")
            facts = {
                "scenario": action,
                "states": actual,
                "chapter_states": chapters,
                "kb_document_status": kb_state[0][0],
                "legacy_model_calls": len(model.valid_keys) - calls_before,
            }
            if action == "observe":
                probe_chapter = await chapter(author, data["project"]["id"], "C8 同串行键后继", "虚构正文。")
                probe = model.expect(gated=True, text=ANALYSIS)
                response = await author.post(f"{API}/chapters/{probe_chapter['id']}/analyze")
                facts["successor_http"] = response.status_code
                if response.status_code == 202:
                    task = response.json()
                    await asyncio.sleep(8)
                    current = (await tasks_by_id(author))[task["id"]]
                    facts["successor_status_after_8s"] = current["status"]
                    facts["successor_model_called"] = probe.entered.is_set()
                    latest = await tasks_by_id(author)
                    facts["analysis_running_after_8s"] = latest[ids["analysis_running"]]["status"]
                    facts["analysis_queued_after_8s"] = latest[ids["analysis_queued"]]["status"]
                if probe.entered.is_set():
                    probe.release.set()
                    await asyncio.wait_for(probe.closed.wait(), 15)
                else:
                    assert model.queue.get_nowait() is probe
            elif action == "resume":
                task = owner_tasks[ids["paused_batch"]]
                response = await author.post(f"{API}/tasks/{task['id']}/resume", params={"task_key": task["task_key"]})
                await asyncio.sleep(3)
                body = response.json()
                body = body if isinstance(body, dict) else {}
                error_code = body.get("code")
                if not isinstance(error_code, str) or not error_code.replace("_", "").isalnum():
                    error_code = None
                known_details = (
                    "批量模型绑定已变化",
                    "模型配置已失效，请重新选择",
                    "项目向量模型已变化，请重新选择",
                    "批量生成结果尚未核对，不能重试",
                    "原认证身份无法验证",
                    "模型绑定无效或已失效",
                )
                error_summary = next((detail for detail in known_details if body.get("detail") == detail), "其他")
                facts.update(
                    action_http=response.status_code,
                    error_code=error_code,
                    error_summary=error_summary,
                    after_status=(await tasks_by_id(author))[task["id"]]["status"],
                    model_calls_after=len(model.valid_keys) - calls_before,
                )
            elif action == "owner_cancel":
                task = owner_tasks[ids["analysis_queued"]]
                response = await author.post(f"{API}/tasks/{task['id']}/cancel", params={"task_key": task["task_key"]})
                await asyncio.sleep(2)
                facts.update(
                    action_http=response.status_code, after_status=(await tasks_by_id(author))[task["id"]]["status"]
                )
            elif action == "finish":
                task = owner_tasks[ids["paused_batch"]]
                response = await author.post(f"{API}/tasks/{task['id']}/finish", params={"task_key": task["task_key"]})
                await asyncio.sleep(1)
                facts.update(
                    action_http=response.status_code,
                    after_status=(await tasks_by_id(author))[task["id"]]["status"],
                    model_calls_after=len(model.valid_keys) - calls_before,
                )
            elif action == "retry_interrupted":
                task = member_tasks[data["interrupted_task"]["id"]]
                response = await member.post(f"{API}/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
                body = response.json()
                facts.update(
                    action_http=response.status_code,
                    error_code=body.get("code") if isinstance(body, dict) else None,
                    after_status=(await tasks_by_id(member))[task["id"]]["status"],
                )
            elif action == "kb_reindex":
                doc_id, project_id = data["kb_queued_document"]["id"], data["knowledge_project"]["id"]
                response = await author.post(f"{API}/projects/{project_id}/kb/documents/{doc_id}/reindex")
                facts["reindex_http"] = response.status_code
                if response.status_code == 202:
                    created = response.json()
                    task = created.get("task", created)
                    final = await task_state(author, task["id"], {"succeeded", "failed"}, task_key=task.get("task_key"))
                    facts["reindex_task_status"] = final["status"]
            else:
                task = member_tasks[ids["queued_generation"]]
                if action == "admin_cancel":
                    response = await admin.post(
                        f"{API}/admin/tasks/{task['task_key']}/cancel", json={"confirm": True, "reason": "C8 隔离演练"}
                    )
                    await asyncio.sleep(12)
                else:
                    response = await member.post(
                        f"{API}/tasks/{task['id']}/cancel", params={"task_key": task["task_key"]}
                    )
                    await asyncio.sleep(2)
                current = (await tasks_by_id(member))[task["id"]]
                cstate = await query(url, f"SELECT status FROM chapters WHERE id={data['queued_chapter']['id']}")
                facts.update(
                    action_http=response.status_code,
                    after_status=current["status"],
                    observation_seconds=12 if action == "admin_cancel" else 2,
                    chapter_after_cancel=cstate[0][0],
                )
                probe = model.expect(gated=True)
                retried = await member.post(f"{API}/chapters/{data['queued_chapter']['id']}/generate", json={})
                facts["restart_http"] = retried.status_code
                if retried.status_code == 202:
                    await asyncio.sleep(4)
                    facts["restart_status_after_4s"] = (await tasks_by_id(member))[retried.json()["id"]]["status"]
                    facts["restart_model_called"] = probe.entered.is_set()
                if probe.entered.is_set():
                    probe.release.set()
                    await asyncio.wait_for(probe.closed.wait(), 15)
                else:
                    assert model.queue.get_nowait() is probe
            mark("未排空旧任务实测", **facts)
        if action == "observe":
            calls_before_restart = len(model.valid_keys)
            async with application(root, new, env, "legacy-observe-restart") as (base, _):
                author = await login(base, AUTHOR)
                try:
                    await asyncio.sleep(8)
                    latest = await tasks_by_id(author)
                    mark(
                        "旧任务重启后观察",
                        observation_seconds=8,
                        analysis_running=latest[data["analysis_running"]["id"]]["status"],
                        analysis_queued=latest[data["analysis_queued"]["id"]]["status"],
                        model_calls=len(model.valid_keys) - calls_before_restart,
                    )
                finally:
                    await author.aclose()


async def concurrent_startup(
    root: Path, shape: str, pg: dict | None, new: Path, key: str, jwt: str, clean: Path
) -> None:
    url = await restore(root, shape, pg, clean, "concurrent_startup")
    assert await version(url) == "0043"
    env = app_env(root, new, url, key, jwt)

    async def business_fingerprint():
        queries = {
            "users": "SELECT id,username,is_admin FROM users ORDER BY id",
            "projects": "SELECT id,user_id,title FROM projects ORDER BY id",
            "chapters": "SELECT id,project_id,title,content,status FROM chapters ORDER BY id",
            "generation_logs": "SELECT id,project_id,chapter_id,kind,status FROM generation_logs ORDER BY id",
        }
        values = {}
        for table, sql in queries.items():
            rows = await query(url, sql)
            values[table] = [len(rows), hashlib.sha256(repr(rows).encode()).hexdigest()]
        return values

    business_before = await business_fingerprint()
    project_rows = await query(
        url, "SELECT p.id FROM projects p JOIN users u ON u.id=p.user_id WHERE u.username='c8_author' ORDER BY p.id"
    )
    assert project_rows
    project_id = project_rows[0][0]
    wrapper = (
        "import os,subprocess,sys\n"
        "job=subprocess.Popen([sys.executable,'-m','alembic','upgrade','head'])\n"
        "print('C8_MIGRATION_PID='+str(job.pid),flush=True)\n"
        "code=job.wait()\n"
        "print('C8_MIGRATION_EXIT='+str(code),flush=True)\n"
        "if code: raise SystemExit(code)\n"
        "os.execv(sys.executable,[sys.executable,'-m','uvicorn','app.main:app','--fd',sys.argv[1],"
        "'--no-access-log'])\n"
    )
    processes = []
    sockets = []
    addresses = []
    handles = []
    launch_time = time.monotonic()
    sqlite_guard = None
    pg_guard = None
    lock_key = 0x41494E4F56454C
    try:
        if shape == "sqlite":
            sqlite_guard = sqlite3.connect(Path(url.removeprefix("sqlite+aiosqlite:///")), isolation_level=None)
            sqlite_guard.execute("BEGIN IMMEDIATE")
        else:
            pg_guard = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
            await pg_guard.execute("SELECT pg_advisory_lock($1)", lock_key)
        held_since = time.monotonic()
        for index in range(2):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            sockets.append(sock)
            addresses.append(f"http://127.0.0.1:{sock.getsockname()[1]}")
            handle = (root / f"concurrent-{index}.log").open("w+")
            handles.append(handle)
            process = await spawn_owned_process(
                [str(new / ".venv/bin/python"), "-c", wrapper, str(sock.fileno())],
                cwd=new,
                env=env,
                stdout=handle,
                stderr=handle,
                pass_fds=(sock.fileno(),),
                on_started=sock.close,
            )
            processes.append(process)

        async with asyncio.timeout(45):
            while True:
                if any(process.returncode is not None for process in processes):
                    raise RuntimeError("并发迁移实例在屏障放行前退出")
                if shape == "sqlite":
                    barrier_logs = [
                        (root / f"concurrent-{index}.log").read_text(errors="replace") for index in range(2)
                    ]
                    waiters = sum("等待 SQLite 迁移写锁" in value for value in barrier_logs)
                else:
                    assert pg_guard is not None
                    waiters = await pg_guard.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND wait_event='advisory' "
                        "AND query LIKE 'SELECT pg_advisory_lock(%'"
                    )
                if waiters == 2:
                    break
                await asyncio.sleep(0.2)
        before_release = await version(url)
        assert before_release == "0043"
        mark(
            "两实例等待测试迁移锁",
            barrier="test_third_connection",
            lock="sqlite_begin_immediate" if shape == "sqlite" else "postgres_advisory",
            waiters=waiters,
            observation_seconds=round(time.monotonic() - held_since, 3),
            revision=before_release,
        )
        if sqlite_guard is not None:
            if sqlite_guard.in_transaction:
                sqlite_guard.execute("ROLLBACK")
            sqlite_guard.close()
            sqlite_guard = None
        if pg_guard is not None:
            assert await pg_guard.fetchval("SELECT pg_advisory_unlock($1)", lock_key)
            await pg_guard.close()
            pg_guard = None

        async def ready(index):
            address = addresses[index]
            async with httpx.AsyncClient(base_url=address, timeout=2, trust_env=False) as client:
                async with asyncio.timeout(120):
                    while True:
                        if processes[index].returncode is not None:
                            raise RuntimeError(f"application_start_failed:concurrent:{processes[index].returncode}")
                        try:
                            if (await client.get("/healthz")).status_code == 200:
                                checked(await client.get(API + "/auth/config"), 200)
                                checked(
                                    await client.post(
                                        API + "/auth/login", json={"username": AUTHOR, "password": PASSWORD}
                                    ),
                                    200,
                                )
                                checked(await client.get(f"{API}/projects/{project_id}"), 200)
                                return round(time.monotonic() - launch_time, 3)
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.1)

        ready_times = await asyncio.gather(*(ready(index) for index in range(2)))
        assert await version(url) == "0070"
        for handle in handles:
            handle.flush()
        logs = [(root / f"concurrent-{index}.log").read_text(errors="replace") for index in range(2)]
        migrations = [log.count("Running upgrade") for log in logs]
        exits = [re.findall(r"^C8_MIGRATION_EXIT=(-?\d+)$", log, re.MULTILINE) for log in logs]
        assert exits == [["0"], ["0"]]
        assert sum(migrations) == 27
        business_after = await business_fingerprint()
        assert business_before == business_after
        mark(
            "两个实例并发启动迁移",
            process_ids=[process.pid for process in processes],
            revision="0070",
            migrations_each=migrations,
            migration_exit_codes=[int(value[0]) for value in exits],
            ready_seconds=ready_times,
            business_database_reads=2,
            business_before=business_before,
            business_after=business_after,
        )
    finally:
        if sqlite_guard is not None:
            if sqlite_guard.in_transaction:
                sqlite_guard.execute("ROLLBACK")
            sqlite_guard.close()
        if pg_guard is not None:
            await pg_guard.close()
        for process in processes:
            await cleanup_owned(process)
        for sock in sockets:
            sock.close()
        for handle in handles:
            handle.close()


async def legacy_downgrades(
    root: Path,
    shape: str,
    pg: dict | None,
    old: Path,
    new: Path,
    key: str,
    jwt: str,
    model: Model,
    model_url: str,
    clean: Path,
    undrained: Path,
) -> None:
    name = "simple_old"
    if shape == "postgres":
        assert pg is not None
        await command(
            [
                "docker",
                "exec",
                pg["cid"],
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" exec createdb -h 127.0.0.1 -U "$POSTGRES_USER" '
                '--maintenance-db=postgres --template=template0 -- "$1"',
                "sh",
                name,
            ]
        )
    simple = url_for(root, shape, pg, name)
    old_env = app_env(root, old, simple, key, jwt)
    await migrate(old, old_env)
    async with application(root, old, old_env, "old-minimal") as (base, _), AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(base_url=base, timeout=30, trust_env=False))
        await register(client, AUTHOR)
        await configure(client, model_url)
        book = await project(client, "C8 无新增语义数据基线")
        ch = await chapter(client, book["id"], "旧版完成任务")
        await generate(client, model, ch["id"])
        minimal = await snapshot(root, shape, pg, simple, "minimal_old")
        agent_base = f"{API}/projects/{book['id']}/agent"
        conversation = checked(await client.post(agent_base + "/conversations", json={}), 201)
        agent_url = agent_base + f"/conversations/{conversation['id']}"
        response = model.expect(tool=True)
        task = checked(await client.post(agent_url + "/messages", json={"content": "请提出虚构角色"}), 202)["task"]
        await asyncio.wait_for(response.entered.wait(), 40)
        task = await task_state(client, task["id"], {"paused", "failed"})
        assert task["status"] == "paused"
        checked(await client.post(f"{API}/tasks/{task['id']}/cancel"), 200)
    proposal = await snapshot(root, shape, pg, simple, "proposal_old")
    for label, source in (("simple", minimal), ("proposal", proposal), ("interrupted", clean)):
        url = await restore(root, shape, pg, source, f"downgrade_{label}")
        env = app_env(root, new, url, key, jwt)
        await migrate(new, env)
        assert await version(url) == "0070"
        before = await fingerprint(url, pg)
        code, _, error = await migrate(new, env, "downgrade", "0043", allowed=(0, 1))
        if code == 0:
            after_revision = await version(url)
            mark(
                "无新版写入的降级",
                case=label,
                accepted=True,
                revision_before=before["revision"],
                revision=after_revision,
            )
            assert after_revision == "0043"
        else:
            after = await fingerprint(url, pg)
            after_revision = await version(url)
            mark(
                "无新版写入的降级",
                case=label,
                accepted=False,
                first_guard=guard_reason(error),
                revision=after_revision,
                schema_unchanged=before["schema"] == after["schema"],
                all_rows_unchanged=before["tables"] == after["tables"],
                before=before,
                after=after,
            )
            assert before == after and after_revision == "0070"
    for label, revision, target in (("generating_chapter", "0046", "0045"), ("unfinished_analysis", "0051", "0050")):
        url = await restore(root, shape, pg, undrained, f"boundary_{label}")
        env = app_env(root, new, url, key, jwt)
        await migrate(new, env, target=revision)
        assert await version(url) == revision
        before = await fingerprint(url, pg)
        code, _, error = await migrate(new, env, "downgrade", target, allowed=(0, 1))
        after = await fingerprint(url, pg)
        actual_revision = await version(url)
        mark(
            "旧数据单迁移保护",
            case=label,
            from_revision=revision,
            to_revision=target,
            first_guard=guard_reason(error),
            schema_unchanged=before["schema"] == after["schema"],
            all_rows_unchanged=before["tables"] == after["tables"],
            before=before,
            after=after,
        )
        assert code != 0 and before == after and actual_revision == revision


async def run(shape: str, root: Path, pg: dict | None) -> None:
    versions = await prepare(root)
    old, new = versions["old"], versions["new"]
    url = url_for(root, shape, pg, "c8base")
    key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
    old_env = app_env(root, old, url, key, jwt)
    await migrate(old, old_env)
    assert await version(url) == "0043"
    mark("旧版迁移", revision="0043", engine=shape)
    model = Model()
    async with fake_model(model) as model_url:
        data = await seed_old(root, old, old_env, model, model_url)
        undrained, clean = await active_old_snapshot(root, shape, pg, url, old, old_env, model, data)
        mark(
            "旧版源库准备完毕",
            undrained=undrained.name,
            drained=clean.name,
            calls=len(model.valid_keys),
            authenticated=all(model.valid_keys),
            unexpected=model.unexpected,
        )
        await upgrade_clean(root, shape, pg, url, old, new, key, jwt, model, data, clean)
        await legacy_scenarios(root, shape, pg, new, key, jwt, model, data, undrained)
        await concurrent_startup(root, shape, pg, new, key, jwt, clean)
        await legacy_downgrades(root, shape, pg, old, new, key, jwt, model, model_url, clean, undrained)
        assert all(model.valid_keys) and model.unexpected == 0 and model.queue.empty()
        mark("脚本场景完成", engine=shape, model_calls=len(model.valid_keys), unexpected_model_calls=model.unexpected)


async def main(shape: str, root: Path) -> None:
    if shape == "sqlite":
        await run(shape, root, None)
    else:
        async with postgres() as pg:
            await run(shape, root, pg)


async def fixture(shape: str, root: Path, pg: dict | None) -> None:
    """仅准备虚构旧版环境；退出信号由 finish 文件给出。"""
    versions = await prepare(root)
    old, new = versions["old"], versions["new"]
    url = url_for(root, shape, pg, "c8base")
    key, jwt = Fernet.generate_key().decode(), secrets.token_hex(32)
    old_env = app_env(root, old, url, key, jwt)
    await migrate(old, old_env)
    assert await version(url) == "0043"
    model = Model()
    async with fake_model(model) as model_url:
        data = await seed_old(root, old, old_env, model, model_url)
        model.autoreply = True
        async with application(root, old, old_env, "old-independent-fixture") as (base, process):
            info = {
                "root": str(root),
                "shape": shape,
                "old_backend": str(old),
                "new_backend": str(new),
                "database_url": url,
                "encryption_key": key,
                "jwt_secret": jwt,
                "admin_username": ADMIN,
                "author_username": AUTHOR,
                "member_username": MEMBER,
                "password": PASSWORD,
                "model_base_url": model_url,
                "model_key": MODEL_KEY,
                "old_app_url": base,
                "old_app_pid": process.pid,
                "project_id": data["project"]["id"],
                "container_id": pg["cid"] if pg else None,
            }
            metadata = root / "fixture.json"
            descriptor = os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(info, stream, ensure_ascii=False, indent=2)
            mark("独立复核环境已准备", fixture=str(metadata), finish_marker=str(root / "finish"))

            def wait_for_finish():
                while not (root / "finish").exists():
                    time.sleep(0.5)

            await asyncio.to_thread(wait_for_finish)


async def main_fixture(shape: str, root: Path) -> None:
    if shape == "sqlite":
        await fixture(shape, root, None)
    else:
        async with postgres() as pg:
            await fixture(shape, root, pg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", required=True, choices=("sqlite", "postgres"))
    parser.add_argument("--prepare-only", action="store_true", help="仅准备独立复核环境并等待 finish 文件")
    args = parser.parse_args()
    mark("脚本版本", sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    with TemporaryDirectory(prefix="ainovel-c8-drill-") as temporary:
        root = Path(temporary).resolve()
        try:
            asyncio.run((main_fixture if args.prepare_only else main)(args.shape, root))
        except BaseException as exc:
            import traceback

            frames = traceback.extract_tb(exc.__traceback__)
            frame = frames[-1] if frames else None
            message = (
                str(exc)
                if isinstance(exc, RuntimeError)
                and str(exc).startswith(
                    (
                        "command_failed:",
                        "http_unexpected:",
                        "application_start_failed:",
                        "container_cleanup_unverified:",
                        "container_cleanup_incomplete:",
                    )
                )
                else ""
            )
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "type": type(exc).__name__,
                        "message": message,
                        "file": Path(frame.filename).name if frame else "",
                        "line": frame.lineno if frame else 0,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1) from None
