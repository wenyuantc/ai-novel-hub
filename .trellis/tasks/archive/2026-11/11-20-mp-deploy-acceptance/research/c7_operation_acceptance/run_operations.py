"""C7 AC7–AC9 隔离验收。真实应用进程、HTTP、数据库及本地模型；只操作本脚本创建的资源。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import runpy
import secrets
import socket
import sqlite3
import sys
import time
import traceback
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from uuid import uuid4

import asyncpg
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.helpers import create_ai_config, create_project, register, task_params
from tests.mcp_fakes import serve
from tests.test_inspiration import options_json

SCRIPT = Path(__file__).resolve()
TRELLIS = next(parent for parent in SCRIPT.parents if parent.name == ".trellis")
BACKEND = TRELLIS.parent / "backend"
C4 = runpy.run_path(str(TRELLIS / "tasks/archive/2026-11/11-20-mp-peer-channel-stream/research/process_stream.py"))
checked = C4["checked"]
API = "/api/v1"
SETTINGS = f"{API}/admin/system-settings"
DRAIN = SETTINGS + "/drain-status"
RUNS = f"{API}/admin/backups/runs"
MAINTENANCE = {"detail": "系统正在维护，暂不受理新任务，请稍后再试", "code": "task_admission_closed"}
ADMIN = "c7-operation-admin"
AUTHOR = "c7-operation-author"
RUN = {
    "phase": "初始化",
    "events": [],
    "processes": [],
    "commands": [],
    "worker_pids": [],
    "http_500": 0,
    "shape": None,
    "from_c6": False,
    "injected": False,
    "injected_command": False,
    "unhandled_async": [],
}
STARTED = time.monotonic()


class ControlledFailure(Exception):
    """仅用于验证故障报告不会保存秘密或正文。"""


class CommandFailure(Exception):
    """仅携带固定枚举分类，不携带子进程输出或参数。"""

    def __init__(self, category, program, exit_code, pid):
        super().__init__(category)
        self.category = category
        self.program = program
        self.exit_code = exit_code
        self.pid = pid


def mark(phase):
    RUN["phase"] = phase
    RUN["events"].append({"phase": phase, "elapsed_s": round(time.monotonic() - STARTED, 3)})


def safe_log_counts(directory):
    """只返回固定类别计数，不复制任何日志行或异常内容。"""
    workspace = directory / "workspace"
    patterns = {
        "error_lines": re.compile(r"\berror\b", re.IGNORECASE),
        "traceback_lines": re.compile(r"\btraceback\b", re.IGNORECASE),
        "sqlite_busy_lines": re.compile(r"database (?:table )?is locked|sqlite_busy|sqlite_locked", re.IGNORECASE),
        "startup_failed_lines": re.compile(r"application startup failed", re.IGNORECASE),
    }
    summaries = []
    for path in sorted(workspace.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            summaries.append({"file": path.name, "unavailable": True})
            continue
        summaries.append(
            {
                "file": path.name,
                **{name: sum(bool(pattern.search(line)) for line in lines) for name, pattern in patterns.items()},
            }
        )
    return summaries


def failure_category(exc):
    if isinstance(exc, ControlledFailure):
        return "controlled_failure"
    if isinstance(exc, CommandFailure):
        return exc.category
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "http_transport"
    if isinstance(exc, AssertionError):
        return "assertion"
    if isinstance(exc, OSError):
        return "os_io"
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    return "runtime"


def safe_loop_exception(_loop, context):
    """异步任务意外退出时只累计异常类别，避免默认处理器输出异常消息或请求内容。"""
    exc = context.get("exception")
    RUN["unhandled_async"].append(type(exc).__name__ if isinstance(exc, BaseException) else "Unknown")


def persist_failure(directory, destination, exc):
    """脱敏诊断仅存类别和位置；绝不存异常消息、环境变量、请求、模型输出或原始日志。"""
    frames = [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(exc.__traceback__)
    ]
    report = {
        "schema": 1,
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "elapsed_s": round(time.monotonic() - STARTED, 3),
        "shape": RUN["shape"],
        "from_c6": RUN["from_c6"],
        "phase": RUN["phase"],
        "category": failure_category(exc),
        "exception_type": type(exc).__name__,
        "frames": frames,
        "events": RUN["events"],
        "processes": RUN["processes"],
        "commands": RUN["commands"],
        "worker_pids": RUN["worker_pids"],
        "observed_http_500": RUN["http_500"],
        "unhandled_async_types": RUN["unhandled_async"],
        "log_summaries": safe_log_counts(directory),
    }
    if isinstance(exc, CommandFailure):
        report["command_failure"] = {"program": exc.program, "exit_code": exc.exit_code, "pid": exc.pid}
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = destination / f"{RUN['shape']}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


# 只在本脚本生成的临时应用入口中插入观察和等待屏障；业务服务、鉴权和数据写入均走正式代码。
PROBE_APP = r"""import asyncio
import os
import time
from pathlib import Path

from starlette.responses import JSONResponse

import app.main as application_module
from app.main import app as application
from app.services.agent import conversation_operations
from app.services.backups import database
from app.services.backups.process import check_running

root = Path(os.environ["C7_PROBE_DIR"])
original_snapshot = database.snapshot_database
original_claim = conversation_operations._claim_or_wait

def paused_snapshot(url, output, *, deadline, stop_event, **kwargs):
    with (root / "snapshots.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    (root / "snapshot-entered").write_text(f"{os.getpid()}\n{output.parent}\n", encoding="utf-8")
    while not (root / "snapshot-release").exists():
        check_running(deadline, stop_event)
        time.sleep(0.025)
    return original_snapshot(url, output, deadline=deadline, stop_event=stop_event, **kwargs)

async def paused_claim(claim, user_id, project_id):
    await original_claim(claim, user_id, project_id)
    hold = root / "agent-hold-pid"
    if hold.exists() and hold.read_text() == str(os.getpid()):
        (root / "agent-entered").write_text(str(os.getpid()), encoding="utf-8")
        while not (root / "agent-release").exists():
            await asyncio.sleep(0.025)

database.snapshot_database = paused_snapshot
conversation_operations._claim_or_wait = paused_claim

async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"] == "/__c7_probe__":
        engine = application.state.task_engine
        body = {"pid": os.getpid(), "source": str(Path(application_module.__file__).resolve()),
                "request_live": engine.request_worker.valid,
                "scheduler_live": engine.lease.valid,
                "task_max_concurrency": application.state.settings.task_max_concurrency,
                "task_max_per_user": application.state.settings.task_max_per_user}
        return await JSONResponse(body, headers={"x-c7-pid": str(os.getpid())})(scope, receive, send)

    async def tagged(message):
        if message["type"] == "http.response.start":
            message.setdefault("headers", []).append((b"x-c7-pid", str(os.getpid()).encode()))
        await send(message)

    await application(scope, receive, tagged)
"""


async def until(callback, *, within=30, interval=0.1):
    async with asyncio.timeout(within):
        while True:
            result = await callback()
            if result:
                return result
            await asyncio.sleep(interval)


def safe_command_category(stderr):
    """只做固定字串分类；原始 stderr 不进入持久报告或控制台。"""
    text = stderr.decode(errors="replace").lower()
    for category, phrases in (
        ("connection_refused", ("connection refused", "connectionrefusederror", "connect call failed")),
        ("postgres_starting", ("database system is starting up", "database system is in recovery")),
        ("authentication_failed", ("authentication failed", "password authentication failed")),
        ("database_missing", ("database does not exist", "invalidcatalognameerror")),
        ("sqlite_busy", ("database is locked", "database table is locked", "sqlite_busy", "sqlite_locked")),
        ("disk_full", ("no space left on device", "disk full")),
        ("permission_denied", ("permission denied",)),
    ):
        if any(phrase in text for phrase in phrases):
            return category
    return "command_failed"


async def command(*args, cwd=None, env=None, limit_s=90):
    basename = Path(args[0]).name
    program = (
        "python" if basename.startswith("python") else basename if basename in {"docker", "git", "tar"} else "other"
    )
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    record = {
        "program": program,
        "phase": RUN["phase"],
        "pid": process.pid,
        "started_s": round(time.monotonic() - STARTED, 3),
    }
    RUN["commands"].append(record)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), limit_s)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        record.update(exit_code=process.returncode, ended_s=round(time.monotonic() - STARTED, 3))
        raise
    record.update(exit_code=process.returncode, ended_s=round(time.monotonic() - STARTED, 3))
    if process.returncode:
        raise CommandFailure(safe_command_category(stderr), program, process.returncode, process.pid)
    return stdout.decode()


@asynccontextmanager
async def postgres(directory: Path):
    password = secrets.token_hex(16)
    cid = None
    try:
        cid = (
            await command(
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "-d",
                "-p",
                "127.0.0.1::5432",
                "-e",
                "POSTGRES_USER=c7probe",
                "-e",
                f"POSTGRES_PASSWORD={password}",
                "-e",
                "POSTGRES_DB=c7probe",
                "postgres:16-alpine",
            )
        ).strip()

        async def ready():
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                cid,
                "pg_isready",
                "-U",
                "c7probe",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0

        await until(ready, within=45, interval=0.3)
        mapped = (await command("docker", "port", cid, "5432/tcp")).strip()
        port = int(mapped.rsplit(":", 1)[1])

        async def externally_ready():
            # 容器内 pg_isready 可能在临时初始化服务阶段成功；实际迁移走宿主端口。
            try:
                connection = await asyncpg.connect(
                    host="127.0.0.1", port=port, user="c7probe", password=password, database="c7probe", timeout=2
                )
                try:
                    return await connection.fetchval("SELECT 1") == 1
                finally:
                    await connection.close()
            except (OSError, asyncpg.PostgresError, TimeoutError):
                return False

        await until(externally_ready, within=45, interval=0.3)
        dump = directory / "pg_dump_c7"
        # 应用仍执行原生 pg_dump 调用。适配器只把标准输出从隔离 PG 容器送到指定的临时文件。
        dump.write_text(
            f"#!{sys.executable}\n"
            "import os, subprocess, sys\n"
            f"cid={cid!r}\npassword={password!r}\n"
            f"docker_home={str(Path.home())!r}\n"
            "args=sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    raise SystemExit(subprocess.call(['docker','exec',cid,'pg_dump','--version'],"
            "env={**os.environ,'HOME':docker_home}))\n"
            "clean=[]; destination=None; i=0\n"
            "while i < len(args):\n"
            "    if args[i] in ('--host','--port','--file'):\n"
            "        if args[i] == '--file': destination=args[i+1]\n"
            "        i += 2; continue\n"
            "    clean.append(args[i]); i += 1\n"
            "assert destination is not None\n"
            "env={**os.environ,'HOME':docker_home,'PGPASSWORD':password}\n"
            "with open(destination,'wb') as out:\n"
            "    raise SystemExit(subprocess.call(['docker','exec','-e','PGPASSWORD',cid,'pg_dump',"
            "'--host','127.0.0.1','--port','5432',*clean],stdout=out,env=env))\n",
            encoding="utf-8",
        )
        dump.chmod(0o700)
        version = (await command("docker", "exec", cid, "postgres", "--version")).strip()
        yield f"postgresql+asyncpg://c7probe:{password}@127.0.0.1:{port}/c7probe", dump, version, cid
    finally:
        if cid:
            await command("docker", "rm", "-f", cid, limit_s=20)


@asynccontextmanager
async def app_process(directory: Path, env: dict[str, str], name: str, *, workers=1):
    with socket.socket() as sock, (directory / f"{name}.log").open("w+", encoding="utf-8") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "c7_probe_app:app",
            "--fd",
            str(sock.fileno()),
            "--workers",
            str(workers),
            "--timeout-keep-alive",
            "300",
            "--no-access-log",
            pass_fds=(sock.fileno(),),
            cwd=directory,
            env=env,
            stdout=log,
            stderr=log,
        )
        process_fact = {
            "name": name,
            "pid": process.pid,
            "workers": workers,
            "started_s": round(time.monotonic() - STARTED, 3),
        }
        RUN["processes"].append(process_fact)
        try:

            async def healthy():
                if process.returncode is not None:
                    raise RuntimeError((directory / f"{name}.log").read_text()[-3000:])
                try:
                    async with httpx.AsyncClient(base_url=url, timeout=1, trust_env=False) as client:
                        return (await client.get("/healthz")).status_code == 200
                except httpx.TransportError:
                    return False

            await until(healthy, within=60)
            yield url, process
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            process_fact.update(exit_code=process.returncode, ended_s=round(time.monotonic() - STARTED, 3))


async def clients_by_pid(stack, services, expected):
    found = {}

    async def probe():
        for url, _ in services:
            candidate = httpx.AsyncClient(
                base_url=url, timeout=20, trust_env=False, limits=httpx.Limits(keepalive_expiry=300)
            )
            try:
                response = await candidate.get("/__c7_probe__")
                data = checked(response, 200)
                pid = data["pid"]
                if pid not in found and data["request_live"] and data["scheduler_live"]:

                    async def verify(result, *, expected_pid=pid):
                        if result.headers.get("x-c7-pid") != str(expected_pid):
                            raise AssertionError("测试连接切换到另一个 worker")
                        if result.status_code == 500:
                            RUN["http_500"] += 1
                            raise AssertionError(f"应用返回 500: {result.request.method} {result.request.url.path}")

                    candidate.event_hooks["response"].append(verify)
                    stack.push_async_callback(candidate.aclose)
                    found[pid] = candidate
                else:
                    await candidate.aclose()
            except BaseException:
                await candidate.aclose()
                raise
        return len(found) >= expected

    await until(probe, within=60, interval=0.05)
    return dict(list(found.items())[:expected])


async def pinned_client(stack, url, wanted):
    async def probe():
        client = httpx.AsyncClient(base_url=url, timeout=30, trust_env=False, limits=httpx.Limits(keepalive_expiry=300))
        try:
            response = await client.get("/__c7_probe__")
            if checked(response, 200)["pid"] != wanted:
                await client.aclose()
                return None

            async def verify(result):
                if result.headers.get("x-c7-pid") != str(wanted):
                    raise AssertionError("测试连接切换到另一个 worker")
                if result.status_code == 500:
                    RUN["http_500"] += 1
                    raise AssertionError(f"应用返回 500: {result.request.method} {result.request.url.path}")

            client.event_hooks["response"].append(verify)
            stack.push_async_callback(client.aclose)
            return client
        except BaseException:
            await client.aclose()
            raise

    return await until(probe, within=30, interval=0.02)


async def sql(database_url: str, statement: str, parameters=None):
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement), parameters or {})).all()
    finally:
        await engine.dispose()


async def save_settings(client, **changes):
    revision = checked(await client.get(SETTINGS), 200)["revision"]
    return checked(await client.patch(SETTINGS, json={"revision": revision, "changes": changes}), 200)


async def task_state(client, task, statuses, *, within=40):
    async def read():
        item = checked(await client.get(f"{API}/tasks/{task['id']}", params=task_params(task)), 200)
        if item["status"] in statuses:
            return item
        if item["status"] in {"failed", "cancelled", "interrupted"} and item["status"] not in statuses:
            raise AssertionError((task["id"], item["status"], item.get("error")))
        return None

    return await until(read, within=within)


async def wait_backup(client, run_id, expected, *, within=35):
    async def read():
        item = checked(await client.get(f"{RUNS}/{run_id}"), 200)
        if item["status"] == expected:
            return item
        if item["status"] not in {"queued", "running"}:
            raise AssertionError((item["status"], expected))
        return None

    return await until(read, within=within)


async def add_chapter(client, project_id, title):
    return checked(
        await client.post(f"{API}/projects/{project_id}/chapters", json={"title": title, "content": ""}), 201
    )["id"]


async def settings_scenario(admin, author_a, author_b, model_url, controls):
    plugin = checked(
        await author_a.post(f"{API}/mcp-plugins", json={"name": "c7-probe", "url": model_url + "/mcp"}), 201
    )
    started = time.monotonic()
    await save_settings(
        admin, registration_enabled=False, mcp_enabled=False, task_max_concurrency=3, task_max_per_user=2
    )
    # 使用固定在对端 worker 的连接，显式移除 Cookie；响应钩子核实处理该请求的 PID。
    registration = await author_b.post(
        f"{API}/auth/register",
        json={"username": "c7-closed", "password": "secret123"},
        headers={"cookie": ""},
    )
    assert registration.status_code == 403 and "set-cookie" not in registration.headers
    plugin_list = await author_b.get(f"{API}/mcp-plugins")
    refresh = await author_b.post(f"{API}/mcp-plugins/{plugin['id']}/refresh")
    trial = await author_b.post(f"{API}/mcp-plugins/{plugin['id']}/call", json={"tool": "lookup", "arguments": {}})
    assert [plugin_list.status_code, refresh.status_code, trial.status_code] == [403, 403, 403]
    assert controls["mcp_hits"] == 0
    observed = round(time.monotonic() - started, 3)

    async def runtime_updated():
        observations = [checked(await client.get("/__c7_probe__"), 200) for client in (author_a, author_b)]
        return (
            observations
            if all((item["task_max_concurrency"], item["task_max_per_user"]) == (3, 2) for item in observations)
            else None
        )

    actual = await until(runtime_updated, within=10, interval=0.05)
    elapsed = round(time.monotonic() - started, 3)
    assert elapsed <= 10
    await save_settings(admin, registration_enabled=True, mcp_enabled=True)
    return {
        "registration": 403,
        "mcp_list_refresh_trial": [403, 403, 403],
        "mcp_network_calls": 0,
        "checked_s": observed,
        "limits": [3, 2],
        "limits_converged_s": elapsed,
        "worker_pids": [x["pid"] for x in actual],
    }


async def verify_artifact(directory, database_url, admin, run_id, pg_container, *, label):
    artifacts = await sql(
        database_url,
        "SELECT checksum_sha256,size_bytes FROM backup_artifacts WHERE run_id=:id AND status='ready'",
        {"id": run_id},
    )
    assert len(artifacts) == 1
    download = await admin.get(f"{RUNS}/{run_id}/download", params={"destination": "local"})
    assert download.status_code == 200, download.text[:200]
    data = download.content
    assert (hashlib.sha256(data).hexdigest(), len(data)) == tuple(artifacts[0])
    if pg_container:
        db_name = f"c7verify_{label}"
        await command("docker", "exec", pg_container, "createdb", "-U", "c7probe", db_name)
        restore = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            pg_container,
            "pg_restore",
            "-U",
            "c7probe",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
            "-d",
            db_name,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, errors = await asyncio.wait_for(restore.communicate(data), 90)
        assert restore.returncode == 0, errors.decode()[-1000:]
        verified = await command(
            "docker",
            "exec",
            pg_container,
            "psql",
            "-U",
            "c7probe",
            "-d",
            db_name,
            "-At",
            "-c",
            "SELECT version_num FROM alembic_version; SELECT count(*) FROM users; SELECT count(*) FROM projects",
        )
        revision, users, projects = verified.splitlines()
    else:

        def check_snapshot():
            path = directory / f"verified-{label}.sqlite3"
            path.write_bytes(data)
            with sqlite3.connect(path) as db:
                assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
                revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                users = db.execute("SELECT count(*) FROM users").fetchone()[0]
                projects = db.execute("SELECT count(*) FROM projects").fetchone()[0]
                return revision, users, projects

        revision, users, projects = await asyncio.to_thread(check_snapshot)
    assert revision == "0070" and int(users) >= 2
    return {
        "download_checksum_verified": True,
        "restored_revision": revision,
        "restored_users": int(users),
        "restored_projects": int(projects),
    }


async def backup_scenario(directory, env, database_url, admin_a, admin_b, worker_pids, pg_container):
    settings = checked(
        await admin_a.patch(
            f"{API}/admin/backups/settings",
            json={
                "revision": 0,
                "changes": {"encryption_enabled": False},
            },
        ),
        200,
    )
    run_id = checked(
        await admin_a.post(
            RUNS,
            json={
                "request_id": str(uuid4()),
                "settings_revision": settings["revision"],
            },
        ),
        202,
    )["id"]

    async def entered():
        path = directory / "snapshot-entered"
        if not path.exists():
            return None
        fields = path.read_text(encoding="utf-8").splitlines()
        return fields if len(fields) == 2 else None

    pid_text, output_dir = await until(entered, within=30)
    backup_pid = int(pid_text)
    assert backup_pid in worker_pids and await asyncio.to_thread(Path(output_dir).is_dir)
    # 在执行者仍持有备份时启动一个额外的真实实例，检查启动恢复不会伤害该备份。
    async with app_process(directory, env, "startup-during-backup") as (extra_url, extra_process):
        async with httpx.AsyncClient(base_url=extra_url, timeout=10, trust_env=False, cookies=admin_a.cookies) as extra:
            extra_info = checked(await extra.get("/__c7_probe__"), 200)
            assert extra_info["pid"] not in worker_pids and extra_info["pid"] == extra_process.pid
            assert checked(await extra.get(f"{RUNS}/{run_id}"), 200)["status"] == "running"
            assert checked(await admin_b.get(f"{RUNS}/{run_id}"), 200)["status"] == "running"
            assert await asyncio.to_thread(Path(output_dir).is_dir)
            (directory / "snapshot-release").touch()
            await wait_backup(admin_b, run_id, "succeeded", within=60)
    executions = (directory / "snapshots.log").read_text(encoding="utf-8").splitlines()
    assert executions == [str(backup_pid)]
    verification = await verify_artifact(directory, database_url, admin_b, run_id, pg_container, label="running")
    assert not await asyncio.to_thread(Path(output_dir).exists)
    return {
        "run_id": run_id,
        "executor_pid": backup_pid,
        "late_start_pid": extra_process.pid,
        "snapshot_calls": len(executions),
        "ready_artifacts": 1,
        **verification,
        "status": "succeeded",
    }


async def backup_after_drain(directory, database_url, admin, pg_container):
    before = checked(await admin.get(DRAIN), 200)
    assert before["enabled"] and all(
        before[key] == 0 for key in ("queued", "running", "paused", "finalizing", "active_backups")
    )
    settings = checked(await admin.get(f"{API}/admin/backups/settings"), 200)
    run = checked(
        await admin.post(
            RUNS,
            json={
                "request_id": str(uuid4()),
                "settings_revision": settings["revision"],
            },
        ),
        202,
    )
    await wait_backup(admin, run["id"], "succeeded", within=60)
    verification = await verify_artifact(directory, database_url, admin, run["id"], pg_container, label="drain")
    assert verification["restored_projects"] >= 1
    after = checked(await admin.get(DRAIN), 200)
    assert after["enabled"] and after["active_backups"] == 0
    return {"run_id": run["id"], "drain_enabled": True, "active_before": 0, "status": "succeeded", **verification}


async def import_inspiration_scenario(author_a, author_b, model, model_url):
    source = await create_project(author_a, title="C7 跨进程导入")
    exported = await author_a.get(f"{API}/projects/{source['id']}/backup")
    assert exported.status_code == 200
    preview = checked(
        await author_a.post(
            f"{API}/projects/import/preview",
            files={
                "file": ("project.json", exported.content, "application/json"),
            },
        ),
        200,
    )
    imported = checked(await author_b.post(f"{API}/projects/import", json={"token": preview["token"]}), 201)
    assert imported["project"]["id"] != source["id"]
    assert (await author_a.post(f"{API}/projects/import", json={"token": preview["token"]})).status_code == 400

    await create_ai_config(author_a, base_url=f"{model_url}/v1/")
    session_id = checked(await author_a.post(f"{API}/inspiration/sessions", json={"idea": "C7 跨进程灵感"}), 201)["id"]
    session = f"{API}/inspiration/sessions/{session_id}"
    generation = f"{session}/steps/title/generate"
    call = model.expect()
    running = asyncio.create_task(author_a.post(generation, json={}))
    try:
        await asyncio.wait_for(call.entered.wait(), 15)
        duplicate = checked(await author_b.post(generation, json={}), 409)
        assert "正在生成" in duplicate["detail"]
    finally:
        call.send(options_json(["山海", "白昼", "长夜", "雨声"]))
        call.finish()
    response = await asyncio.wait_for(running, 30)
    assert response.status_code == 200 and "event: done" in response.text and model.unexpected == 0
    state = checked(await author_b.get(session), 200)
    assert len(state["state"]["steps"]["title"]["rounds"]) == 1
    return {
        "preview_pid": int((await author_a.get("/__c7_probe__")).headers["x-c7-pid"]),
        "import_pid": int((await author_b.get("/__c7_probe__")).headers["x-c7-pid"]),
        "imported_once": True,
        "inspiration_conflict": 409,
        "inspiration_rounds": 1,
    }


async def agent_scenario(directory, database_url, author_a, author_b, model, pid_a):
    project = await create_project(author_a, title="C7 智能体会话互斥")
    base = f"{API}/projects/{project['id']}/agent"
    conv = checked(await author_a.post(base + "/conversations", json={}), 201)
    path = base + f"/conversations/{conv['id']}"
    (directory / "agent-hold-pid").write_text(str(pid_a), encoding="utf-8")
    call = model.expect()
    first = asyncio.create_task(author_a.post(path + "/messages", json={"content": "请回答"}))

    async def entered():
        return (directory / "agent-entered").exists()

    second = None
    try:
        await until(entered, within=20)
        owner = await sql(
            database_url,
            """
            SELECT w.pid FROM agent_conversation_operations o JOIN task_workers w ON w.worker_key=o.worker_key
            WHERE o.conversation_id=:id
        """,
            {"id": conv["id"]},
        )
        assert owner == [(pid_a,)]
        second = asyncio.create_task(author_b.post(path + "/messages", json={"content": "重复请求"}))
        await asyncio.sleep(0.5)
        assert not second.done(), "对端应等待原会话操作释放"
    finally:
        (directory / "agent-release").touch()
    first_response = await asyncio.wait_for(first, 20)
    assert first_response.status_code == 202, first_response.text
    task = first_response.json()["task"]
    other = await asyncio.wait_for(second, 20)
    assert other.status_code == 409, other.text
    await asyncio.wait_for(call.entered.wait(), 20)
    call.send("这个会话只执行一次。")
    call.finish()
    await task_state(author_b, task, {"succeeded"})
    messages = (
        await sql(
            database_url,
            "SELECT count(*) FROM agent_messages WHERE conversation_id=:id AND role='user'",
            {"id": conv["id"]},
        )
    )[0][0]
    assert messages == 1 and model.unexpected == 0
    (directory / "agent-hold-pid").unlink(missing_ok=True)
    return {
        "first_pid": pid_a,
        "second_pid": int((await author_b.get("/__c7_probe__")).headers["x-c7-pid"]),
        "second_waited": True,
        "first_status": 202,
        "second_status": 409,
        "user_messages": messages,
    }


async def drain_scenario(author_a, author_b, admin_a, admin_b, model, controls, worker_pids):
    await save_settings(admin_a, task_max_concurrency=1, task_max_per_user=1)
    project = (await create_project(author_a, title="C7 排空"))["id"]
    checked(await author_a.patch(f"{API}/projects/{project}", json={"auto_analyze": False}), 200)
    chapters = [await add_chapter(author_a, project, f"章节 {i}") for i in range(6)]
    controls["fail_next"] = True
    failed = checked(await author_a.post(f"{API}/chapters/{chapters[0]}/generate", json={}), 202)
    failed = await task_state(author_a, failed, {"failed"})

    batch_call = model.expect()
    batch_call.send("排空前已生成的第一章。")
    batch_call.finish()
    batch = checked(
        await author_a.post(
            f"{API}/projects/{project}/batch-generate",
            json={
                "start_chapter_id": chapters[1],
                "end_chapter_id": chapters[2],
                "review_every": 1,
                "auto_continue": False,
            },
        ),
        202,
    )
    paused = await task_state(author_a, batch, {"paused"}, within=35)

    running_call = model.expect()
    running = checked(await author_a.post(f"{API}/chapters/{chapters[3]}/generate", json={}), 202)
    await asyncio.wait_for(running_call.entered.wait(), 20)
    queued_call = model.expect()
    queued_call.send("排空前排队的任务继续完成。")
    queued_call.finish()
    queued = checked(await author_b.post(f"{API}/chapters/{chapters[5]}/generate", json={}), 202)
    await task_state(author_b, queued, {"queued"})
    try:
        await save_settings(admin_a, task_admission_closed=True)
        for user, admin in ((author_a, admin_a), (author_b, admin_b)):
            for response in (
                await user.post(f"{API}/chapters/{chapters[4]}/generate", json={}),
                await user.post(f"{API}/tasks/{failed['id']}/retry", params=task_params(failed)),
                await user.post(f"{API}/tasks/{paused['id']}/resume", params=task_params(paused)),
            ):
                assert response.status_code == 503 and response.json() == MAINTENANCE, (
                    response.status_code,
                    response.text,
                )
            status = checked(await admin.get(DRAIN), 200)
            assert status["enabled"] and status["queued"] >= 1 and status["running"] >= 1 and status["paused"] >= 1
            assert worker_pids <= {row["pid"] for row in status["workers"]}
        assert (await task_state(author_b, failed, {"failed"}))["status"] == "failed"
        assert (await task_state(author_b, paused, {"paused"}))["status"] == "paused"
        untouched = checked(await author_b.get(f"{API}/projects/{project}/chapters/{chapters[4]}"), 200)
        assert untouched["status"] == "draft" and untouched["content"] == ""
        cancelled = await author_b.post(f"{API}/tasks/{paused['id']}/cancel", params=task_params(paused))
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
        running_call.send("排空前的任务继续完成。")
        running_call.finish()
        await task_state(author_b, running, {"succeeded"})
        await task_state(author_b, queued, {"succeeded"})

        async def zero():
            status = checked(await admin_b.get(DRAIN), 200)
            return (
                status
                if all(status[key] == 0 for key in ("queued", "running", "paused", "finalizing", "active_backups"))
                else None
            )

        await until(zero, within=40)
    finally:
        running_call.finish()
    return {
        "both_workers_rejected_create_retry_resume": True,
        "running_finished": True,
        "queued_finished": True,
        "paused_cancelled": True,
        "active_count": 0,
        "admission_still_closed": True,
    }


async def run_shape(shape, directory, database_url, dump, db_version, old_backend=None, pg_container=None):
    directory.mkdir(mode=0o700)
    (directory / "c7_probe_app.py").write_text(PROBE_APP, encoding="utf-8")
    password = secrets.token_urlsafe(24)
    jwt_secret = secrets.token_hex(32)
    encryption_key = Fernet.generate_key().decode()
    if RUN["injected"]:
        password = os.environ.get("C7_DIAG_TEST_PASSWORD", password)
        jwt_secret = os.environ.get("C7_DIAG_TEST_JWT", jwt_secret)
        encryption_key = os.environ.get("C7_DIAG_TEST_CIPHER", encryption_key)
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": f"{directory}{os.pathsep}{BACKEND}",
        "NO_PROXY": "*",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "DATABASE_URL": database_url,
        "JWT_SECRET": jwt_secret,
        "ENCRYPTION_KEY": encryption_key,
        "ADMIN_USERNAME": ADMIN,
        "ADMIN_PASSWORD": password,
        "REGISTRATION_ENABLED": "true",
        "AUTO_ANALYZE_ENABLED": "false",
        "TASK_MAX_CONCURRENCY": "2",
        "TASK_MAX_PER_USER": "2",
        "ALLOW_PRIVATE_AI_ENDPOINTS": "true",
        "BACKUP_DIR": str(directory / "backups"),
        "FRONTEND_DIST_DIR": str(directory / "no-dist"),
        "C7_PROBE_DIR": str(directory),
    }
    if dump:
        env["BACKUP_PG_DUMP_PATH"] = str(dump)
    if shape == "pg-instances":
        env.update(TASK_PEER_BIND_HOST="127.0.0.1", TASK_PEER_ADVERTISE_HOST="127.0.0.1")
    initial_backend = old_backend or BACKEND
    initial_env = dict(env, PYTHONPATH=f"{directory}{os.pathsep}{initial_backend}")

    async def migrate(backend, current_env):
        await command(
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(backend / "alembic.ini"),
            "upgrade",
            "head",
            cwd=directory,
            env=current_env,
            limit_s=180,
        )

    mark("初始数据库迁移")
    await migrate(initial_backend, initial_env)
    model = C4["Model"]()
    controls = {"fail_next": False, "mcp_hits": 0, "unexpected_requests": []}

    async def chat(request):
        if controls["fail_next"]:
            controls["fail_next"] = False
            return JSONResponse(
                {"error": {"message": "controlled failure", "type": "invalid_request_error"}}, status_code=400
            )
        if not model.waiting.qsize():
            payload = await request.json()
            controls["unexpected_requests"].append(
                {
                    "stream": payload.get("stream"),
                    "roles": [message.get("role") for message in payload.get("messages", [])],
                }
            )
        return await model.chat(request)

    async def mcp(request):
        controls["mcp_hits"] += 1
        return JSONResponse({"error": "unexpected MCP call"}, status_code=500)

    fake = Starlette(
        routes=[
            Route("/v1/chat/completions", chat, methods=["POST"]),
            Route("/mcp", mcp, methods=["GET", "POST", "DELETE"]),
        ]
    )

    try:
        async with AsyncExitStack() as outer:
            model_url = await outer.enter_async_context(serve(fake))
            apps = await outer.enter_async_context(AsyncExitStack())
            mark("启动初始应用进程")
            if shape == "pg-workers":
                services = [await apps.enter_async_context(app_process(directory, initial_env, "workers", workers=2))]
            else:
                services = [
                    await apps.enter_async_context(app_process(directory, initial_env, name)) for name in ("A", "B")
                ]
            pairs = await clients_by_pid(apps, services, 2)
            pids = list(pairs)
            RUN["worker_pids"].extend(pids)
            mark("登录及核对执行者")
            admin_a, admin_b = pairs.values()
            for admin in (admin_a, admin_b):
                checked(await admin.post(f"{API}/auth/login", json={"username": ADMIN, "password": password}), 200)
            author_a = await pinned_client(apps, str(admin_a.base_url), pids[0])
            author_b = await pinned_client(apps, str(admin_b.base_url), pids[1])
            await register(author_a, AUTHOR)
            author_b.cookies.update(author_a.cookies)
            assert checked(await author_b.get(f"{API}/auth/me"), 200)["username"] == AUTHOR

            initial_sources = [
                checked(await client.get("/__c7_probe__"), 200)["source"] for client in (admin_a, admin_b)
            ]
            assert all(Path(source).is_relative_to(initial_backend) for source in initial_sources)
            initial_revision = (await sql(database_url, "SELECT version_num FROM alembic_version"))[0][0]
            result = {
                "shape": shape,
                "database": db_version,
                "initial_pids": pids,
                "initial_revision": initial_revision,
                "initial_source": "C6 d9d234c" if old_backend else "C7 工作区",
            }
            if RUN["injected"]:
                mark("受控故障")
                sentinels = [
                    password,
                    jwt_secret,
                    encryption_key,
                    os.environ.get("C7_DIAG_TEST_TOKEN", "c7-dummy-token"),
                    os.environ.get("C7_DIAG_TEST_REQUEST", "c7-dummy-request"),
                    os.environ.get("C7_DIAG_TEST_MODEL", "c7-dummy-model"),
                ]
                # 模拟日志及异常含敏感值；故障报告只保留类别计数，临时日志会被清理。
                (directory / "controlled-fault.log").write_text("ERROR " + " ".join(sentinels), encoding="utf-8")
                raise ControlledFailure(" ".join(sentinels))
            mark("并发备份及恢复检查")
            result["backup"] = await backup_scenario(
                directory,
                initial_env,
                database_url,
                admin_a,
                admin_b,
                set(pids),
                pg_container,
            )
            print(f"{shape} backup: {json.dumps(result['backup'], ensure_ascii=False)}", flush=True)
            mark("跨进程设置")
            result["settings"] = await settings_scenario(admin_a, author_a, author_b, model_url, controls)
            print(f"{shape} settings: {json.dumps(result['settings'], ensure_ascii=False)}", flush=True)
            mark("跨进程导入与灵感")
            result["import_inspiration"] = await import_inspiration_scenario(author_a, author_b, model, model_url)
            print(
                f"{shape} import_inspiration: {json.dumps(result['import_inspiration'], ensure_ascii=False)}",
                flush=True,
            )
            mark("智能体会话互斥")
            result["agent"] = await agent_scenario(directory, database_url, author_a, author_b, model, pids[0])
            print(f"{shape} agent: {json.dumps(result['agent'], ensure_ascii=False)}", flush=True)
            mark("任务排空")
            result["drain"] = await drain_scenario(author_a, author_b, admin_a, admin_b, model, controls, set(pids))
            print(f"{shape} drain: {json.dumps(result['drain'], ensure_ascii=False)}", flush=True)
            assert model.unexpected == 0, f"排空阶段出现未安排的模型调用: {controls['unexpected_requests']}"
            mark("排空后备份及恢复检查")
            result["drained_backup"] = await backup_after_drain(directory, database_url, admin_b, pg_container)
            print(f"{shape} drained_backup: {json.dumps(result['drained_backup'], ensure_ascii=False)}", flush=True)

            # 已排空且完成手动备份，再停止所有实例、迁移并恢复实例，验证排空状态持久。
            mark("停服及升级迁移")
            await apps.aclose()
            await migrate(BACKEND, env)
            resumed = await outer.enter_async_context(AsyncExitStack())
            mark("启动升级后应用")
            if shape == "pg-workers":
                services = [await resumed.enter_async_context(app_process(directory, env, "workers-after", workers=2))]
            else:
                services = [
                    await resumed.enter_async_context(app_process(directory, env, name))
                    for name in ("A-after", "B-after")
                ]
            after = await clients_by_pid(resumed, services, 2)
            RUN["worker_pids"].extend(pid for pid in after if pid not in RUN["worker_pids"])
            new_admin = next(iter(after.values()))
            after_sources = [checked(await client.get("/__c7_probe__"), 200)["source"] for client in after.values()]
            assert all(Path(source).is_relative_to(BACKEND) for source in after_sources)
            checked(await new_admin.post(f"{API}/auth/login", json={"username": ADMIN, "password": password}), 200)
            status = checked(await new_admin.get(DRAIN), 200)
            assert status["enabled"] is True and all(
                status[key] == 0 for key in ("queued", "running", "paused", "finalizing", "active_backups")
            )
            await save_settings(new_admin, task_admission_closed=False)
            mark("恢复任务受理")
            new_author = await pinned_client(resumed, str(new_admin.base_url), next(iter(after)))
            checked(await new_author.post(f"{API}/auth/login", json={"username": AUTHOR, "password": "secret123"}), 200)
            project = (await create_project(new_author, title="C7 恢复受理"))["id"]
            checked(await new_author.patch(f"{API}/projects/{project}", json={"auto_analyze": False}), 200)
            chapter = await add_chapter(new_author, project, "重新开放后")
            call = model.expect()
            call.send("恢复之后的新任务正常完成。")
            call.finish()
            task = checked(await new_author.post(f"{API}/chapters/{chapter}/generate", json={}), 202)
            await task_state(new_author, task, {"succeeded"})
            result["restart"] = {
                "stopped_all": True,
                "migration": "head",
                "final_revision": (await sql(database_url, "SELECT version_num FROM alembic_version"))[0][0],
                "final_source": "C7 工作区",
                "after_pids": list(after),
                "drain_preserved": True,
                "reopened_task": "succeeded",
            }
            assert model.unexpected == 0, f"重启后出现未安排的模型调用: {controls['unexpected_requests']}"
            mark("检查应用日志")
            await resumed.aclose()
            if shape == "sqlite":
                busy_pattern = re.compile(r"database (?:table )?is locked|sqlite_busy|sqlite_locked", re.IGNORECASE)
                app_logs = [path for path in directory.glob("*.log") if path.name != "snapshots.log"]
                busy_lines = [
                    line for path in app_logs for line in path.read_text().splitlines() if busy_pattern.search(line)
                ]
                result["sqlite_contention"] = {
                    "scope": "本脚本 AC7–AC9 及排空重启请求",
                    "observed_http_500": 0,
                    "busy_log_lines": len(busy_lines),
                    "examined_app_logs": len(app_logs),
                }
            return result
    except BaseException:
        (directory / "snapshot-release").touch()
        (directory / "agent-release").touch()
        for call in model.calls:
            call.finish()
        raise


async def archive_c6(directory):
    source = directory / "c6-source"
    source.mkdir(mode=0o700)
    archive = directory / "c6-backend.tar"
    await command("git", "archive", "--format=tar", f"--output={archive}", "d9d234c", "backend", cwd=TRELLIS.parent)
    await command("tar", "-xf", str(archive), "-C", str(source))
    backend = source / "backend"
    assert (backend / "app/main.py").is_file()
    old_hash = hashlib.sha256((backend / "alembic/env.py").read_bytes()).hexdigest()
    new_hash = hashlib.sha256((BACKEND / "alembic/env.py").read_bytes()).hexdigest()
    assert old_hash != new_hash, "需对照已实现的 C7 迁移代码"
    print(json.dumps({"c6_alembic_sha256": old_hash, "c7_alembic_sha256": new_hash}), flush=True)
    return backend


async def main(shape, directory, from_c6):
    asyncio.get_running_loop().set_exception_handler(safe_loop_exception)
    mark("准备隔离环境")
    if RUN["injected_command"]:
        mark("受控子进程故障")
        code = (
            "import os, sys; "
            "sys.stderr.write('connection refused ' + ' '.join("
            "os.environ[name] for name in os.environ if name.startswith('C7_DIAG_TEST_'))); "
            "sys.exit(17)"
        )
        await command(sys.executable, "-c", code)
    old_backend = await archive_c6(directory) if from_c6 else None
    if shape == "sqlite":
        database = directory / "database.sqlite3"
        return await run_shape(
            shape,
            directory / "workspace",
            f"sqlite+aiosqlite:///{database}",
            None,
            "SQLite",
            old_backend,
        )
    async with postgres(directory) as (url, dump, version, pg_container):
        return await run_shape(shape, directory / "workspace", url, dump, version, old_backend, pg_container)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", choices=("sqlite", "pg-workers", "pg-instances"), required=True)
    parser.add_argument("--from-c6", action="store_true", help="用 d9d234c 旧代码启动，停服后升级到当前 C7 代码")
    parser.add_argument("--diagnostics-dir", type=Path, default=SCRIPT.parent / "failures", help="故障脱敏诊断保存目录")
    parser.add_argument("--inject-failure", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--inject-command-failure", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    RUN.update(
        shape=args.shape,
        from_c6=args.from_c6,
        injected=args.inject_failure,
        injected_command=args.inject_command_failure,
    )
    exit_code = 0
    with TemporaryDirectory(prefix="ainovel-c7-operations-") as temp:
        directory = Path(temp).resolve()
        try:
            result = asyncio.run(main(args.shape, directory, args.from_c6))
            if RUN["unhandled_async"]:
                raise RuntimeError("验收期间出现未处理的异步任务异常")
        except BaseException as exc:
            path = None
            for destination in (args.diagnostics_dir, Path(gettempdir()) / "ainovel-c7-diagnostics"):
                try:
                    path = persist_failure(directory, destination, exc)
                    break
                except Exception:
                    continue
            if path is None:
                print("验收失败，脱敏诊断无法写入指定目录或系统临时目录。", file=sys.stderr, flush=True)
                exit_code = 2
            else:
                print(f"验收失败，脱敏诊断已保存：{path}", file=sys.stderr, flush=True)
                exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    if directory.exists():
        print("隔离临时目录未清理，验收不能标为成功。", file=sys.stderr, flush=True)
        raise SystemExit(2)
    if exit_code:
        print("隔离临时资源已清理。", file=sys.stderr, flush=True)
        raise SystemExit(exit_code)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
