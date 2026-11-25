"""C7 实际多进程验收。只创建临时库、临时应用进程和专属 PostgreSQL 容器。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import runpy
import secrets
import signal
import socket
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import asyncpg
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from tests.helpers import analysis_json, create_ai_config, create_project, parse_sse, register, task_params
from tests.mcp_fakes import serve
from tests.test_agent_entries_e4id import _invite
from tests.test_agent_providers import openai_chunk
from tests.test_collaboration_stream_agent import DENIED

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "backend/alembic.ini").is_file())
BACKEND = ROOT / "backend"
ARCHIVE = ROOT / ".trellis/tasks/archive/2026-11"
DEFAULT_DIAGNOSTICS = Path(__file__).resolve().parent / "diagnostics"
STREAM = runpy.run_path(str(ARCHIVE / "11-20-mp-peer-channel-stream/research/process_stream.py"))
Model = STREAM["Model"]


@dataclass
class ToolTicket:
    entered: asyncio.Event
    release: asyncio.Event


class AcceptanceModel(Model):
    def __init__(self):
        super().__init__()
        self.world = None
        self.owners = {}
        self.release = asyncio.Event()
        self.started = []
        self.finished = []
        self.active = 0
        self.peak = 0
        self.active_by_owner = {"A": 0, "B": 0}
        self.peak_by_owner = {"A": 0, "B": 0}
        self.pending_tool: ToolTicket | None = None

    def expect_tool(self):
        assert self.pending_tool is None
        self.pending_tool = ToolTicket(asyncio.Event(), asyncio.Event())
        return self.pending_tool

    def reset_schedule(self):
        assert self.active == 0
        self.release = asyncio.Event()
        self.started = []
        self.finished = []
        self.peak = 0
        self.peak_by_owner = {"A": 0, "B": 0}

    async def chat(self, request):
        body = await request.json()
        if body.get("tools") and self.pending_tool:
            ticket = self.pending_tool
            self.pending_tool = None

            async def tools():
                ticket.entered.set()
                await ticket.release.wait()
                chunk = openai_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c7-call",
                                "type": "function",
                                "function": {"name": "mcp__dict__send", "arguments": '{"to":"测试编辑部"}'},
                            }
                        ]
                    },
                    "tool_calls",
                )
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"

            return StreamingResponse(tools(), media_type="text/event-stream")
        tags = set(re.findall(r"C7MARK-([A-Z][0-9]+)", json.dumps(body.get("messages"), ensure_ascii=False)))
        if not tags:
            return await super().chat(request)
        assert len(tags) == 1, tags
        tag = tags.pop()
        assert tag in self.owners and self.world is not None, tag
        rows = await self.world.rows(
            "SELECT t.id,w.pid FROM tasks t JOIN chapters c ON c.id=t.chapter_id "
            "JOIN task_workers w ON w.worker_key=t.worker_key "
            "WHERE c.title=:title AND t.kind='chapter.analyze' AND t.status='running'",
            {"title": tag},
        )
        assert len(rows) == 1, (tag, rows)
        owner = self.owners[tag]
        self.started.append((tag, rows[0][0], rows[0][1]))
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.active_by_owner[owner] += 1
        self.peak_by_owner[owner] = max(self.peak_by_owner[owner], self.active_by_owner[owner])

        async def chunks():
            try:
                await self.release.wait()
                await asyncio.sleep(0.15)
                chunk = openai_chunk({"content": analysis_json(summary=f"完成 {tag}")}, "stop")
                yield f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                self.finished.append(tag)
            finally:
                self.active -= 1
                self.active_by_owner[owner] -= 1

        return StreamingResponse(chunks(), media_type="text/event-stream")


class McpProbe:
    """真实 HTTP JSON-RPC 替身；只控制外部服务何时返回工具结果。"""

    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def rpc(self, request):
        msg = await request.json()
        method = msg.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "c7", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "send",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "to": {"type": "string"},
                            },
                            "required": ["to"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            self.calls.append(msg["params"])

            async def body():
                self.entered.set()
                await self.release.wait()
                yield json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {
                            "content": [{"type": "text", "text": "C7 外部调用完成"}],
                            "isError": False,
                        },
                    },
                    ensure_ascii=False,
                )

            return StreamingResponse(body(), media_type="application/json")
        else:
            return Response(status_code=202)
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result})


# 测试专用包装器：记录实际接入 PID；指定 PID 不符时在进入产品应用之前返回，
# 由脚手架重新连接。生产路由、授权、调度和 SSE 都未改动。
WRAPPER = """import json
import os
import time
from app.main import app as real_app

async def app(scope, receive, send):
    if scope["type"] != "http":
        return await real_app(scope, receive, send)
    headers = dict(scope.get("headers", []))
    target = headers.get(b"x-c7-target-pid")
    if target and target != str(os.getpid()).encode():
        await send({"type": "http.response.start", "status": 421,
                    "headers": [(b"x-c7-target-mismatch", b"1"), (b"content-length", b"0")]})
        return await send({"type": "http.response.body", "body": b""})
    if scope["path"] == "/__c7__/peer-off" and headers.get(b"x-c7-control") == os.environ["C7_CONTROL_TOKEN"].encode():
        await real_app.state.task_engine.peer.stop()
        await send({"type": "http.response.start", "status": 204,
                    "headers": [(b"x-c7-pid", str(os.getpid()).encode())]})
        return await send({"type": "http.response.body", "body": b""})
    async def tagged(message):
        if message["type"] == "http.response.start":
            if message["status"] >= 500:
                record = {"pid": os.getpid(), "status": message["status"],
                          "time": round(time.time(), 3)}
                with open(os.environ["C7_HTTP_ERRORS"], "a", encoding="utf-8") as output:
                    output.write(json.dumps(record, ensure_ascii=False) + "\\n")
            message["headers"] = list(message.get("headers", [])) + [(b"x-c7-pid", str(os.getpid()).encode())]
        await send(message)
    await real_app(scope, receive, tagged)
"""


def checked(response, expected):
    assert response.status_code == expected, (response.request.url.path, response.status_code, response.text[:500])
    return response.json() if response.content else None


async def wait_until(predicate, *, seconds=30):
    async with asyncio.timeout(seconds):
        while True:
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.1)


@asynccontextmanager
async def postgres():
    password = secrets.token_hex(16)
    result = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--pull=never",
        "--rm",
        "-d",
        "-p",
        "127.0.0.1::5432",
        "-e",
        "POSTGRES_USER=c7_probe",
        "-e",
        f"POSTGRES_PASSWORD={password}",
        "-e",
        "POSTGRES_DB=c7_probe",
        "postgres:16-alpine",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await result.communicate()
    if result.returncode:
        raise RuntimeError(f"临时 PostgreSQL 容器启动失败: {error.decode()[-1000:]}")
    container = output.decode().strip()
    try:

        async def ready():
            probe = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                container,
                "pg_isready",
                "-U",
                "c7_probe",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await probe.wait() == 0

        await wait_until(ready, seconds=60)
        port = await asyncio.create_subprocess_exec(
            "docker",
            "port",
            container,
            "5432/tcp",
            stdout=asyncio.subprocess.PIPE,
        )
        address, _ = await port.communicate()
        assert port.returncode == 0
        host_port = int(address.decode().strip().rsplit(":", 1)[1])

        async def tcp_ready():
            try:
                conn = await asyncpg.connect(
                    host="127.0.0.1",
                    port=host_port,
                    user="c7_probe",
                    password=password,
                    database="c7_probe",
                    timeout=1,
                    ssl=False,
                )
                try:
                    return await conn.fetchval("SELECT 1") == 1
                finally:
                    await conn.close()
            except (OSError, TimeoutError, asyncpg.PostgresError, ConnectionError):
                return False

        await wait_until(tcp_ready, seconds=60)
        yield f"postgresql+asyncpg://c7_probe:{password}@127.0.0.1:{host_port}/c7_probe"
    finally:
        cleanup = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            container,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await cleanup.wait()


@dataclass
class Process:
    url: str
    process: asyncio.subprocess.Process
    name: str


def record_process_event(directory, kind, pid, returncode=None):
    event = {"kind": kind, "pid": pid, "returncode": returncode, "time": round(time.time(), 3)}
    with (directory / "process-events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(event) + "\n")


@asynccontextmanager
async def service(directory, env, name, workers=1):
    with socket.socket() as sock, (directory / f"{name}.log").open("w+") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "c7_asgi:app",
            "--fd",
            str(sock.fileno()),
            "--workers",
            str(workers),
            "--no-access-log",
            pass_fds=(sock.fileno(),),
            cwd=directory,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            await asyncio.to_thread(record_process_event, directory, "uvicorn_start", process.pid)

            async def ready():
                if process.returncode is not None:
                    log.seek(0)
                    raise RuntimeError(f"{name} 启动失败\n{log.read()[-3000:]}")
                try:
                    async with httpx.AsyncClient(base_url=url, timeout=1, trust_env=False) as client:
                        return (await client.get("/healthz")).status_code == 200
                except httpx.TransportError:
                    return False

            await wait_until(ready, seconds=60)
            yield Process(url, process, name)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            await asyncio.to_thread(record_process_event, directory, "uvicorn_stop", process.pid, process.returncode)


class Stream:
    def __init__(self, url, cookies, task, target):
        self.text = ""
        self.status = None
        self.pid = None
        self.closed = asyncio.Event()
        self.client = httpx.AsyncClient(
            base_url=url,
            cookies=cookies,
            timeout=35,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self.task = asyncio.create_task(self.run(task, target))

    async def run(self, task, target):
        try:
            async with asyncio.timeout(30):
                while True:
                    request = self.client.build_request(
                        "GET",
                        f"/api/v1/tasks/{task['id']}/stream",
                        params=task_params(task),
                        headers={"X-C7-Target-Pid": str(target)},
                    )
                    response = await self.client.send(request, stream=True)
                    if response.headers.get("x-c7-target-mismatch"):
                        await response.aclose()
                        await asyncio.sleep(0.01)
                        continue
                    break
            self.status = response.status_code
            self.pid = int(response.headers["x-c7-pid"])
            assert self.pid == target and self.status == 200, (self.pid, target, self.status)
            try:
                async for chunk in response.aiter_text():
                    self.text += chunk
            finally:
                await response.aclose()
        finally:
            self.closed.set()

    def events(self):
        end = self.text.rfind("\n\n")
        return parse_sse(self.text[: end + 2]) if end >= 0 else []

    async def until(self, predicate, seconds=15):
        async def found():
            return predicate(self.events()) or self.closed.is_set()

        await wait_until(found, seconds=seconds)
        events = self.events()
        assert predicate(events), (self.status, self.text)
        return events

    async def finish(self, seconds=70):
        await asyncio.wait_for(self.closed.wait(), seconds)
        await self.task
        return self.events()

    async def close(self):
        if not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.client.aclose()


@dataclass
class World:
    topology: str
    directory: Path
    env: dict
    database: object
    model: object
    model_url: str
    processes: list[Process]
    stack: AsyncExitStack

    def entry_url(self):
        return next(p.url for p in self.processes if p.process.returncode is None)

    async def rows(self, sql, values=None):
        async with self.database.connect() as connection:
            return (await connection.execute(text(sql), values or {})).all()

    async def pids(self):
        rows = await self.rows("SELECT DISTINCT pid FROM task_workers WHERE stopped_at IS NULL AND fenced_at IS NULL")
        return {row[0] for row in rows}

    async def owns_pid(self, pid):
        if self.topology != "pg-workers":
            return any(p.process.pid == pid and p.process.returncode is None for p in self.processes)
        supervisor = self.processes[0].process
        if supervisor.returncode is not None:
            return False
        probe = await asyncio.create_subprocess_exec(
            "ps",
            "-o",
            "ppid=",
            "-p",
            str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await probe.communicate()
        return probe.returncode == 0 and out.decode().strip() == str(supervisor.pid)

    async def owner(self, task):
        rows = await self.rows(
            "SELECT w.pid FROM tasks t JOIN task_workers w ON w.worker_key=t.worker_key "
            "WHERE t.id=:id AND t.statistics_key=:key AND t.status='running'",
            task,
        )
        assert len(rows) == 1, rows
        return rows[0][0]

    async def target_url(self, pid):
        if self.topology == "pg-workers":
            return self.processes[0].url
        for process in self.processes:
            if process.process.pid == pid and process.process.returncode is None:
                return process.url
        raise AssertionError((pid, [(p.name, p.process.pid, p.process.returncode) for p in self.processes]))

    async def request(self, client, pid, method, path, **kwargs):
        url = await self.target_url(pid)
        headers = dict(kwargs.pop("headers", {}), **{"X-C7-Target-Pid": str(pid)})
        async with asyncio.timeout(30):
            while True:
                response = await client.request(method, url + path, headers=headers, **kwargs)
                if not response.headers.get("x-c7-target-mismatch"):
                    assert int(response.headers["x-c7-pid"]) == pid
                    return response
                await asyncio.sleep(0.01)

    async def remote(self, pid):
        async def available():
            candidates = (await self.pids()) - {pid}
            for candidate in candidates:
                try:
                    async with httpx.AsyncClient(
                        timeout=2, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
                    ) as client:
                        response = await asyncio.wait_for(self.request(client, candidate, "GET", "/healthz"), 3)
                    if response.status_code == 200:
                        return candidate
                except (AssertionError, TimeoutError, httpx.TransportError):
                    pass
            return None

        return await wait_until(available, seconds=60)

    async def restart_killed(self, pid):
        previous = await self.pids()
        assert await self.owns_pid(pid), f"拒绝向不属于本次验收的进程 {pid} 发送信号"
        os.kill(pid, signal.SIGKILL)
        if self.topology != "pg-workers":
            proc = next(p for p in self.processes if p.process.pid == pid)
            await asyncio.wait_for(proc.process.wait(), 10)
            replacement = await self.stack.enter_async_context(service(self.directory, self.env, proc.name + "-new"))
            self.processes.append(replacement)
            return replacement.process.pid

        async def new_worker():
            difference = (await self.pids()) - previous
            return next(iter(difference)) if difference else None

        return await wait_until(new_worker, seconds=60)


@asynccontextmanager
async def world(directory, topology):
    async with AsyncExitStack() as stack:
        database_url = (
            f"sqlite+aiosqlite:///{directory / 'c7.sqlite3'}"
            if topology == "sqlite"
            else await stack.enter_async_context(postgres())
        )
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": f"{directory}:{BACKEND}",
            "NO_PROXY": "*",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "DATABASE_URL": database_url,
            "JWT_SECRET": secrets.token_hex(32),
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "ADMIN_USERNAME": "c7_admin",
            "ADMIN_PASSWORD": secrets.token_urlsafe(24),
            "REGISTRATION_ENABLED": "true",
            "AUTO_ANALYZE_ENABLED": "false",
            "ALLOW_PRIVATE_AI_ENDPOINTS": "true",
            "BACKUP_DIR": str(directory / "backups"),
            "FRONTEND_DIST_DIR": str(directory / "no-dist"),
            "C7_CONTROL_TOKEN": secrets.token_hex(24),
            "C7_HTTP_ERRORS": str(directory / "http-errors.jsonl"),
        }
        if topology == "sqlite":
            # Unix socket 路径长度受限；专用短目录放在 /tmp，并随当前隔离环境清理。
            env["TASK_PEER_SOCKET_DIR"] = stack.enter_context(TemporaryDirectory(prefix="c7-peer-", dir="/tmp"))
        else:
            env.update(TASK_PEER_BIND_HOST="127.0.0.1", TASK_PEER_ADVERTISE_HOST="127.0.0.1")
        (directory / "c7_asgi.py").write_text(WRAPPER)
        migration = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(BACKEND / "alembic.ini"),
            "upgrade",
            "head",
            cwd=directory,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.to_thread(record_process_event, directory, "migration_start", migration.pid)
        _, errors = await migration.communicate()
        await asyncio.to_thread(record_process_event, directory, "migration_stop", migration.pid, migration.returncode)
        if migration.returncode:
            raise RuntimeError(errors.decode()[-2000:])
        database = create_async_engine(database_url, poolclass=NullPool)
        stack.push_async_callback(database.dispose)
        model = AcceptanceModel()
        model_url = await stack.enter_async_context(
            serve(
                Starlette(
                    routes=[
                        Route("/v1/chat/completions", model.chat, methods=["POST"]),
                    ]
                )
            )
        )
        processes = []
        if topology == "pg-workers":
            processes.append(await stack.enter_async_context(service(directory, env, "multi", workers=2)))
        else:
            for name in ("A", "B"):
                processes.append(await stack.enter_async_context(service(directory, env, name)))
        value = World(topology, directory, env, database, model, model_url, processes, stack)
        model.world = value
        await wait_until(lambda: enough_pids(value), seconds=60)
        try:
            yield value
        finally:
            for call in model.calls:
                call.finish()


async def enough_pids(value):
    return len(await value.pids()) >= 2


@dataclass
class Book:
    owner: httpx.AsyncClient
    actor: httpx.AsyncClient
    project_id: int
    chapter_id: int
    member_id: int | None = None
    revision: int | None = None


async def create_book(world, stack, name, *, collaborator=False):
    async def account(username):
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=world.entry_url(),
                timeout=30,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        )
        user = await register(client, username)
        return client, user

    owner, _ = await account(f"{name}-owner")
    await create_ai_config(owner, base_url=f"{world.model_url}/v1/")
    project = await create_project(owner, title=f"C7 {name}")
    chapter = checked(
        await owner.post(
            f"/api/v1/projects/{project['id']}/chapters",
            json={"title": "第一章", "content": ""},
        ),
        201,
    )
    actor, member_id, revision = owner, None, None
    if collaborator:
        actor, member = await account(f"{name}-member")
        accepted = await _invite(owner, actor, project["id"], f"{name}-member", allow_owner_ai=True)
        member_id, revision = member["id"], accepted["revision"]
    return Book(owner, actor, project["id"], chapter["id"], member_id, revision)


async def begin(world, book):
    call = world.model.expect()
    task = checked(
        await book.actor.post(f"/api/v1/chapters/{book.chapter_id}/generate", json={"auto_continue": False}), 202
    )
    await asyncio.wait_for(call.entered.wait(), 40)
    owner = await world.owner({"id": task["id"], "key": task["task_key"]})
    remote = await world.remote(owner)
    return task, call, owner, remote


async def status(world, task):
    rows = await world.rows(
        "SELECT status,execution_key,worker_key FROM tasks WHERE id=:id AND statistics_key=:key",
        {
            "id": task["id"],
            "key": task["task_key"],
        },
    )
    assert len(rows) == 1, rows
    return rows[0]


async def final_status(world, task, expected, *, seconds=60):
    async def done():
        row = await status(world, task)
        return row if row[0] == expected else None

    return await wait_until(done, seconds=seconds)


async def no_claim(world, book, task):
    slots = await world.rows("SELECT count(*) FROM task_execution_slots WHERE task_id=:id", {"id": task["id"]})
    chapter = await world.rows("SELECT status,generation_claim_key FROM chapters WHERE id=:id", {"id": book.chapter_id})
    assert slots == [(0,)] and chapter == [("draft", None)], (slots, chapter)


async def agent_setup(world, stack, name="structured"):
    """真实模型/插件 HTTP 与公开 Agent 路由；返回待确认的外部提案。"""
    client = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=world.entry_url(),
            timeout=30,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
    )
    await register(client, f"{name}-owner")
    await create_ai_config(client, base_url=f"{world.model_url}/v1/")
    project = await create_project(client, title=f"C7 {name}")
    base = f"/api/v1/projects/{project['id']}/agent"
    conversation = checked(await client.post(base + "/conversations", json={}), 201)
    conversation_url = base + f"/conversations/{conversation['id']}"
    remote = McpProbe()
    endpoint = await stack.enter_async_context(serve(Starlette(routes=[Route("/mcp", remote.rpc, methods=["POST"])])))
    # 退出时先放行在途工具调用，再关闭 MCP 服务。
    stack.callback(remote.release.set)
    plugin = checked(await client.post("/api/v1/mcp-plugins", json={"name": "dict", "url": endpoint + "/mcp"}), 201)
    checked(await client.post(f"/api/v1/mcp-plugins/{plugin['id']}/refresh"), 200)
    checked(await client.patch(conversation_url, json={"mcp_plugin_ids": [plugin["id"]]}), 200)
    ticket = world.model.expect_tool()
    stack.callback(ticket.release.set)
    response = checked(await client.post(conversation_url + "/messages", json={"content": "请准备发送到编辑部"}), 202)
    task = response["task"]
    await asyncio.wait_for(ticket.entered.wait(), 30)
    owner = await world.owner({"id": task["id"], "key": task["task_key"]})
    other = await world.remote(owner)
    streams = []
    for pid in (owner, other):
        stream = Stream(await world.target_url(pid), client.cookies, task, pid)
        stack.push_async_callback(stream.close)
        streams.append(stream)
    await asyncio.gather(*(s.until(lambda fs: any(event == "snapshot" for event, _ in fs)) for s in streams))
    ticket.release.set()
    direct, forwarded = await asyncio.gather(*(stream.finish() for stream in streams))
    assert direct == forwarded, (direct, forwarded)
    names = [event for event, _ in forwarded]
    assert "tool_start" in names and "proposal" in names and names[-1] == "paused", names
    detail = checked(await client.get(conversation_url), 200)
    changes = [change for change in detail["changes"] if change["kind"] == "external"]
    assert len(changes) == 1 and changes[0]["status"] == "proposed", changes
    return {
        "client": client,
        "project": project,
        "base": base,
        "conversation_url": conversation_url,
        "task": task,
        "change": changes[0],
        "executor_pid": owner,
        "other_pid": other,
        "remote": remote,
        "events": names,
    }


async def scheduling(world):
    async with AsyncExitStack() as stack:
        clients = []
        for name in ("schedule-a", "schedule-b", None):
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=world.entry_url(),
                    timeout=30,
                    trust_env=False,
                    limits=httpx.Limits(max_keepalive_connections=0),
                )
            )
            if name:
                await register(client, name)
                await create_ai_config(client, base_url=f"{world.model_url}/v1/")
            else:
                checked(
                    await client.post(
                        "/api/v1/auth/login",
                        json={
                            "username": world.env["ADMIN_USERNAME"],
                            "password": world.env["ADMIN_PASSWORD"],
                        },
                    ),
                    200,
                )
            clients.append(client)
        author_a, author_b, admin = clients
        settings = "/api/v1/admin/system-settings"

        async def configure(total, per_user):
            current = checked(await admin.get(settings), 200)
            checked(
                await admin.patch(
                    settings,
                    json={
                        "revision": current["revision"],
                        "changes": {"task_max_concurrency": total, "task_max_per_user": per_user},
                    },
                ),
                200,
            )

        async def project(client, tags):
            row = await create_project(client, title=f"C7 并发 {tags[0]}")
            result = {}
            for tag in tags:
                chapter = checked(
                    await client.post(
                        f"/api/v1/projects/{row['id']}/chapters",
                        json={
                            "title": tag,
                            "content": f"这段正文标记为 C7MARK-{tag}。",
                        },
                    ),
                    201,
                )
                result[tag] = chapter["id"]
            return result

        request_pids = {}

        async def enqueue(client, chapter, tag):
            response = await client.post(f"/api/v1/chapters/{chapter}/analyze")
            request_pids[tag] = int(response.headers["x-c7-pid"])
            return checked(response, 202)

        async def observed(tag):
            async def entered():
                return any(row[0] == tag for row in world.model.started)

            await wait_until(entered, seconds=35)

        barrier_seconds = []

        async def enqueue_on_other(client, chapter_id, first_tag, next_tag):
            first_pid = next(row[2] for row in world.model.started if row[0] == first_tag)
            other_pid = await world.remote(first_pid)
            assert await world.owns_pid(first_pid), f"拒绝暂停不属于本次验收的进程 {first_pid}"
            target_url = await world.target_url(other_pid)
            pinned = None
            async with asyncio.timeout(15):
                while pinned is None:
                    candidate = httpx.AsyncClient(
                        base_url=target_url,
                        cookies=client.cookies,
                        timeout=2,
                        trust_env=False,
                        limits=httpx.Limits(max_keepalive_connections=1),
                    )
                    try:
                        probe = await candidate.get("/healthz")
                        if probe.status_code == 200 and int(probe.headers["x-c7-pid"]) == other_pid:
                            pinned = candidate
                            break
                    except httpx.TransportError:
                        pass
                    finally:
                        if pinned is None:
                            await candidate.aclose()
                    await asyncio.sleep(0.01)
            stopped_at = time.monotonic()
            os.kill(first_pid, signal.SIGSTOP)
            try:
                async with asyncio.timeout(4.5):
                    response = await pinned.post(
                        f"/api/v1/chapters/{chapter_id}/analyze",
                        headers={"X-C7-Target-Pid": str(other_pid)},
                    )
                    assert int(response.headers["x-c7-pid"]) == other_pid
                    request_pids[next_tag] = int(response.headers["x-c7-pid"])
                    task = checked(response, 202)
                    await observed(next_tag)
            finally:
                os.kill(first_pid, signal.SIGCONT)
                barrier_seconds.append(round(time.monotonic() - stopped_at, 3))
                await pinned.aclose()
            assert barrier_seconds[-1] < 5, barrier_seconds
            actual_pid = next(row[2] for row in world.model.started if row[0] == next_tag)
            assert actual_pid == other_pid, world.model.started
            return task

        async def all_done(tasks):
            for task in tasks.values():
                await final_status(world, task, "succeeded", seconds=75)

        await configure(2, 6)
        world.model.reset_schedule()
        world.model.owners.update({"X1": "A", "X2": "A", "X3": "A", "Y1": "B", "Y2": "B"})
        chapter = await project(author_a, ["X1", "X2", "X3"])
        chapter.update(await project(author_b, ["Y1"]))
        chapter.update(await project(author_b, ["Y2"]))
        tasks = {"X1": await enqueue(author_a, chapter["X1"], "X1")}
        await observed("X1")
        # 短暂暂停首个执行进程，并从另一进程提交第二个任务，确保两个进程均实际领取；
        # 取得第二个模型请求后立即恢复，不改写应用的租约或调度逻辑。
        tasks["Y1"] = await enqueue_on_other(author_b, chapter["Y1"], "X1", "Y1")
        for tag, client in (("X2", author_a), ("X3", author_a), ("Y2", author_b)):
            tasks[tag] = await enqueue(client, chapter[tag], tag)
        await asyncio.sleep(5.25)
        assert [row[0] for row in world.model.started] == ["X1", "Y1"], world.model.started
        assert world.model.peak == 2
        first_wave = list(world.model.started)
        world.model.release.set()
        await all_done(tasks)
        starts = [row[0] for row in world.model.started]
        assert [tag for tag in starts if tag.startswith("X")] == ["X1", "X2", "X3"], starts
        assert [tag for tag in world.model.finished if tag.startswith("X")] == ["X1", "X2", "X3"]
        global_result = {"peak": world.model.peak, "first_wave": first_wave, "starts": world.model.started}

        await configure(6, 2)
        world.model.reset_schedule()
        world.model.owners.update({"A1": "A", "A2": "A", "A3": "A", "B1": "B"})
        chapter = {}
        for tag in ("A1", "A2", "A3"):
            chapter.update(await project(author_a, [tag]))
        chapter.update(await project(author_b, ["B1"]))
        tasks = {"A1": await enqueue(author_a, chapter["A1"], "A1")}
        await observed("A1")
        tasks["A2"] = await enqueue_on_other(author_a, chapter["A2"], "A1", "A2")
        tasks["B1"] = await enqueue(author_b, chapter["B1"], "B1")
        await observed("B1")
        tasks["A3"] = await enqueue(author_a, chapter["A3"], "A3")
        await asyncio.sleep(5.25)
        assert [row[0] for row in world.model.started] == ["A1", "A2", "B1"], world.model.started
        assert world.model.peak == 3 and world.model.peak_by_owner == {"A": 2, "B": 1}
        world.model.release.set()
        await all_done(tasks)
        user_result = {
            "peak": world.model.peak,
            "peak_by_user": world.model.peak_by_owner,
            "starts": world.model.started,
        }
        observed_pids = {row[2] for row in global_result["starts"] + user_result["starts"]}
        assert len(observed_pids) >= 2, observed_pids
        return {
            "case": "scheduling",
            "barrier_seconds": barrier_seconds,
            "global": global_result,
            "per_user": user_result,
            "request_pids": request_pids,
        }


async def kill_serial(world):
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=world.entry_url(),
                timeout=30,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        )
        await register(client, "serial-kill-owner")
        await create_ai_config(client, base_url=f"{world.model_url}/v1/")
        project = await create_project(client, title="C7 串行占用恢复")
        world.model.reset_schedule()
        world.model.owners.update({"K1": "A", "K2": "A"})
        stack.callback(world.model.release.set)
        chapters = []
        for tag in ("K1", "K2"):
            row = checked(
                await client.post(
                    f"/api/v1/projects/{project['id']}/chapters",
                    json={
                        "title": tag,
                        "content": f"这段正文标记为 C7MARK-{tag}。",
                    },
                ),
                201,
            )
            chapters.append(row["id"])
        first = checked(await client.post(f"/api/v1/chapters/{chapters[0]}/analyze"), 202)

        async def began(tag):
            async def entered():
                return any(row[0] == tag for row in world.model.started)

            await wait_until(entered, seconds=45)

        await began("K1")
        second = checked(await client.post(f"/api/v1/chapters/{chapters[1]}/analyze"), 202)
        assert (await status(world, second))[0] == "queued"
        keys = await world.rows(
            "SELECT serial_key FROM tasks WHERE id IN (:first,:second) ORDER BY id",
            {
                "first": first["id"],
                "second": second["id"],
            },
        )
        assert len(keys) == 2 and keys[0][0] == keys[1][0] == f"analysis:{project['id']}", keys
        owner = await world.owner({"id": first["id"], "key": first["task_key"]})
        remote = await world.remote(owner)
        started = time.monotonic()
        replacement = await world.restart_killed(owner)

        async def uncertain():
            rows = await world.rows(
                "SELECT status,recovered_at,worker_key FROM tasks WHERE id=:id AND statistics_key=:key",
                {"id": first["id"], "key": first["task_key"]},
            )
            return rows[0] if rows and rows[0][0] == "running" and rows[0][1] and rows[0][2] is None else None

        await wait_until(uncertain, seconds=75)
        recovery_s = time.monotonic() - started
        assert recovery_s <= 35, ("接管超过租约加一个轮询周期", recovery_s)
        detail = checked(
            await world.request(
                client,
                remote,
                "GET",
                f"/api/v1/tasks/{first['id']}",
                params=task_params(first),
            ),
            200,
        )
        assert detail["stage"] == "结果待核对", detail
        retry = await world.request(
            client, remote, "POST", f"/api/v1/tasks/{first['id']}/retry", params=task_params(first)
        )
        assert retry.status_code == 409, retry.text
        await began("K2")
        world.model.release.set()
        await final_status(world, second, "succeeded", seconds=60)
        slots = await world.rows("SELECT count(*) FROM task_execution_slots WHERE task_id=:id", {"id": first["id"]})
        assert slots == [(0,)] and [row[0] for row in world.model.started] == ["K1", "K2"]
        return {
            "case": "kill-serial",
            "executor_pid": owner,
            "other_pid": remote,
            "replacement_pid": replacement,
            "recovery_s": round(recovery_s, 3),
            "original_status": "结果待核对",
            "original_retry": retry.status_code,
            "serial_order": [row[0] for row in world.model.started],
        }


async def exercise(world, case):
    if case == "scheduling":
        return await scheduling(world)
    if case == "kill-serial":
        return await kill_serial(world)
    if case == "agent-structured":
        async with AsyncExitStack() as stack:
            scene = await agent_setup(world, stack)
            return {key: scene[key] for key in ("executor_pid", "other_pid", "events")} | {"case": case}
    async with AsyncExitStack() as stack:
        book = await create_book(world, stack, case, collaborator=(case in {"revoke", "downgrade", "collab-off"}))
        task, model, owner, remote = await begin(world, book)
        stack.callback(model.finish)
        result = {"case": case, "executor_pid": owner, "other_pid": remote}
        if case == "restart":
            before = await status(world, task)
            snapshots = []
            monitoring = asyncio.Event()

            async def observe():
                while not monitoring.is_set():
                    row = await status(world, task)
                    snapshots.append(row)
                    assert row == before, ("启动另一进程期间任务状态变化", row, before)
                    await asyncio.sleep(0.1)

            watcher = asyncio.create_task(observe())
            try:
                if world.topology == "pg-workers":
                    replacement = await world.restart_killed(remote)
                else:
                    extra = await world.stack.enter_async_context(service(world.directory, world.env, "late"))
                    world.processes.append(extra)
                    replacement = extra.process.pid
            finally:
                monitoring.set()
                await watcher
            assert (await status(world, task)) == before
            assert snapshots, "启动期间没有观察到任务状态"
            model.send("C7 已有任务继续运行。")
            model.finish()
            await final_status(world, task, "succeeded")
            result.update(new_pid=replacement, running_samples=len(snapshots))
        elif case in {"forward", "revoke", "downgrade", "author-off", "collab-off"}:
            streams = []
            for pid in (owner, remote):
                stream = Stream(await world.target_url(pid), book.actor.cookies, task, pid)
                stack.push_async_callback(stream.close)
                streams.append(stream)
            await asyncio.gather(*(s.until(lambda fs: any(name == "snapshot" for name, _ in fs)) for s in streams))
            model.send("C7 第一段实时正文。")
            await asyncio.gather(
                *(s.until(lambda fs: ("delta", {"text": "C7 第一段实时正文。"}) in fs) for s in streams)
            )
            if case in {"revoke", "downgrade"}:
                member_path = f"/api/v1/projects/{book.project_id}/collaborators/{book.member_id}"
                if case == "revoke":
                    response = await world.request(
                        book.owner,
                        remote,
                        "DELETE",
                        member_path,
                        params={"expected_revision": book.revision},
                    )
                    assert response.status_code == 204, response.text
                else:
                    response = await world.request(
                        book.owner,
                        remote,
                        "PATCH",
                        member_path,
                        json={
                            "expected_revision": book.revision,
                            "role": "viewer",
                        },
                    )
                    assert response.status_code == 200, response.text
                model.send("C7 撤权后禁止发送。")
                model.finish()
                for stream in streams:
                    frames = await stream.finish()
                    assert frames[-1] == ("error", DENIED) and "撤权后禁止发送" not in stream.text
            elif case in {"author-off", "collab-off"}:
                response = await world.request(
                    book.owner,
                    owner,
                    "POST",
                    "/__c7__/peer-off",
                    headers={"X-C7-Control": world.env["C7_CONTROL_TOKEN"]},
                )
                assert response.status_code == 204
                marker = "C7 通道关闭后的正文。"
                model.send(marker)
                if case == "author-off":
                    await streams[1].until(
                        lambda fs: any(
                            name == "snapshot" and marker in json.dumps(data, ensure_ascii=False) for name, data in fs
                        ),
                        seconds=20,
                    )
                    assert (await status(world, task))[0] == "running"
                    model.finish()
                    frames = await streams[1].finish()
                    assert frames[-1][0] == "done" and frames[-1][1]["task"]["status"] == "succeeded", frames
                else:
                    frames = await streams[1].finish(seconds=20)
                    assert frames[-1] == ("error", DENIED), frames
                    assert marker not in streams[1].text
                    model.finish()
                    await final_status(world, task, "succeeded")
                    replay = Stream(await world.target_url(remote), book.actor.cookies, task, remote)
                    stack.push_async_callback(replay.close)
                    replay_frames = await replay.finish(seconds=20)
                    assert replay_frames[-1][0] == "done" and replay_frames[-1][1]["task"]["status"] == "succeeded"
                    result["replay_events"] = [name for name, _ in replay_frames]
                result["remote_events"] = [name for name, _ in frames]
            else:
                model.send("C7 第二段实时正文。")
                await asyncio.gather(
                    *(s.until(lambda fs: ("delta", {"text": "C7 第二段实时正文。"}) in fs) for s in streams)
                )
                model.finish()
                first, second = await asyncio.gather(*(s.finish() for s in streams))
                assert first == second and first[-1][0] == "done", (first, second)
                result["events"] = [name for name, _ in first]
        elif case in {"cancel", "admin-cancel", "delete"}:
            started = time.monotonic()
            if case == "cancel":
                response = await world.request(
                    book.owner, remote, "POST", f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task)
                )
                assert response.status_code == 200, response.text
            elif case == "admin-cancel":
                admin = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=world.entry_url(),
                        timeout=30,
                        trust_env=False,
                        limits=httpx.Limits(max_keepalive_connections=0),
                    )
                )
                checked(
                    await admin.post(
                        "/api/v1/auth/login",
                        json={"username": world.env["ADMIN_USERNAME"], "password": world.env["ADMIN_PASSWORD"]},
                    ),
                    200,
                )
                response = await world.request(
                    admin,
                    remote,
                    "POST",
                    f"/api/v1/admin/tasks/{task['task_key']}/cancel",
                    json={"confirm": True, "reason": "C7 验收"},
                )
                assert response.status_code == 202, response.text
            else:
                response = await world.request(book.owner, remote, "DELETE", f"/api/v1/projects/{book.project_id}")
                assert response.status_code == 204, response.text
                assert model.closed.is_set(), "作品删除返回时原模型连接仍未退出"
            await asyncio.wait_for(model.closed.wait(), 20)
            if case != "delete":
                await final_status(world, task, "cancelled")
                await no_claim(world, book, task)
            result["elapsed_s"] = round(time.monotonic() - started, 3)
        elif case == "kill":
            started = time.monotonic()
            replacement = await world.restart_killed(owner)
            row = await final_status(world, task, "interrupted", seconds=75)
            recovery_s = time.monotonic() - started
            assert recovery_s <= 35, ("接管超过租约加一个轮询周期", recovery_s)
            assert row[1] is None, row
            await no_claim(world, book, task)

            async def settled_evidence():
                return (
                    await world.rows("SELECT status,execution_key,finished_at FROM tasks WHERE id=:id", {
                        "id": task["id"],
                    }),
                    await world.rows("SELECT status,finished_event_at FROM admin_task_statistics WHERE key=:key", {
                        "key": task["task_key"],
                    }),
                    await world.rows("SELECT count(*) FROM generation_logs WHERE task_id=:id", {"id": task["id"]}),
                )

            settled = await settled_evidence()
            assert settled[1] and settled[1][0][0] == "interrupted", settled
            await asyncio.sleep(5.2)
            assert await settled_evidence() == settled, "重启后重复修改了原任务的收尾事实"
            retry_model = world.model.expect()
            retry = checked(
                await world.request(
                    book.owner, remote, "POST", f"/api/v1/tasks/{task['id']}/retry", params=task_params(task)
                ),
                202,
            )
            assert retry["retry_of_id"] == task["id"]
            await asyncio.wait_for(retry_model.entered.wait(), 40)
            retry_model.send("C7 接管后重新执行。")
            retry_model.finish()
            await final_status(world, retry, "succeeded")
            result.update(
                replacement_pid=replacement,
                recovery_s=round(recovery_s, 3),
                status=row[0],
                retry_status=retry["status"],
                retry_final="succeeded",
            )
        else:
            raise ValueError(case)
        return result


CASES = (
    "restart", "forward", "revoke", "downgrade", "cancel", "admin-cancel", "delete", "kill", "kill-serial",
    "agent-structured", "scheduling", "author-off", "collab-off",
)
TOPOLOGIES = ("sqlite", "pg-instances", "pg-workers")
ERROR_CATEGORIES = {
    "AssertionError", "CancelledError", "ConnectionError", "ConnectError", "FileNotFoundError", "OSError",
    "PermissionError", "ReadError", "RuntimeError", "TimeoutError", "ValueError",
}
LOG_PATTERNS = {
    "sqlite_busy": r"database(?: table| schema)? is (?:locked|busy)|sqlite_busy|sqlite_locked",
    "traceback": r"Traceback \(most recent call last\):",
    "timeout": r"\b(?:TimeoutError|timed out)\b",
    "connection_error": r"\b(?:ConnectionError|ConnectError|ConnectionRefusedError)\b",
}


def error_category(exc):
    name = type(exc).__name__
    return name if name in ERROR_CATEGORIES else "OtherError"


def numeric_time(value):
    return value if isinstance(value, (int, float)) and 0 < value < 10**11 else None


def read_json_lines(path):
    if not path.exists():
        return [], 0
    values, invalid = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            invalid += 1
            continue
        if isinstance(value, dict):
            values.append(value)
        else:
            invalid += 1
    return values, invalid


def read_diagnostics(directory):
    errors = []
    http_records, invalid_http = read_json_lines(directory / "http-errors.jsonl")
    for item in http_records:
        pid, status, occurred = item.get("pid"), item.get("status"), numeric_time(item.get("time"))
        if type(pid) is int and pid > 0 and type(status) is int and 500 <= status <= 599 and occurred:
            errors.append({"pid": pid, "status": status, "time": occurred})
        else:
            invalid_http += 1

    processes = []
    process_records, invalid_process = read_json_lines(directory / "process-events.jsonl")
    for item in process_records:
        kind, pid, occurred = item.get("kind"), item.get("pid"), numeric_time(item.get("time"))
        returncode = item.get("returncode")
        if (
            kind in {"migration_start", "migration_stop", "uvicorn_start", "uvicorn_stop"}
            and type(pid) is int and pid > 0 and occurred
            and (returncode is None or type(returncode) is int)
        ):
            processes.append({"kind": kind, "pid": pid, "returncode": returncode, "time": occurred})
        else:
            invalid_process += 1

    logs = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in directory.glob("*.log"))
    log_counts = {name: len(re.findall(pattern, logs, flags=re.IGNORECASE)) for name, pattern in LOG_PATTERNS.items()}
    pid_matches = re.findall(r"(?:Started|Finished) (?:server|parent) process \[(\d+)\]", logs)
    log_pids = sorted({int(pid) for pid in pid_matches})
    return {
        "http_5xx": errors,
        "invalid_http_records": invalid_http,
        "log_counts": log_counts,
        "process_events": processes,
        "invalid_process_records": invalid_process,
        "log_pids": log_pids,
    }


def safe_traceback(exc):
    frames = []
    tb = exc.__traceback__
    while tb:
        filename = Path(tb.tb_frame.f_code.co_filename).name
        function = tb.tb_frame.f_code.co_name
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", filename):
            filename = "unknown"
        if not re.fullmatch(r"[A-Za-z0-9_<>]{1,100}", function):
            function = "unknown"
        frames.append({"file": filename, "function": function, "line": tb.tb_lineno})
        tb = tb.tb_next
    return frames


def write_failure_report(destination, report):
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = datetime.now(UTC).strftime("c7-failure-%Y%m%dT%H%M%S-") + secrets.token_hex(4) + ".json"
    path = destination / filename
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    return path


def print_diagnostics(topology, diagnostics):
    print(
        json.dumps(
            {"topology": topology, "diagnostics": {
                "http_5xx": diagnostics["http_5xx"],
                "invalid_http_records": diagnostics["invalid_http_records"],
                "sqlite_busy_log_hits": diagnostics["log_counts"]["sqlite_busy"],
            }},
            ensure_ascii=False,
        ),
        flush=True,
    )


async def main(topology, cases, *, diagnostics_dir=None, _case_hook=None, _result_hook=None, _diagnostics_hook=None):
    destination = Path(diagnostics_dir) if diagnostics_dir else DEFAULT_DIAGNOSTICS
    started = datetime.now(UTC).isoformat(timespec="milliseconds")
    phase, case = "setup", None
    workers, completed = set(), []
    directory, reported_exc = None, None

    async def preserve_failure(exc):
        try:
            if directory is None:
                diagnostics = {"collection_error": "TemporaryDirectoryNotAvailable"}
            else:
                diagnostics = await asyncio.to_thread(read_diagnostics, directory)
                print_diagnostics(topology if topology in TOPOLOGIES else "other", diagnostics)
        except Exception as collection_exc:
            diagnostics = {"collection_error": error_category(collection_exc)}
        report = {
            "format_version": 1,
            "topology": topology if topology in TOPOLOGIES else "other",
            "phase": phase,
            "scenario": case if case in CASES else None,
            "completed_scenarios": [item for item in completed if item in CASES],
            "started_at_utc": started,
            "failed_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "runner_pid": os.getpid(),
            "observed_worker_pids": sorted(pid for pid in workers if type(pid) is int and pid > 0),
            "error_category": error_category(exc),
            "traceback": safe_traceback(exc),
            "diagnostics": diagnostics,
        }
        try:
            path = await asyncio.to_thread(write_failure_report, destination, report)
            print(
                json.dumps({"failure_report": str(path), "error_category": report["error_category"]}),
                file=sys.stderr,
                flush=True,
            )
        except Exception as report_exc:
            print(
                json.dumps({"failure_report_write_error": error_category(report_exc)}),
                file=sys.stderr,
                flush=True,
            )

    try:
        with TemporaryDirectory(prefix="ainovel-c7-tasks-") as temporary:
            directory = Path(temporary)
            try:
                async with world(directory, topology) as value:
                    workers.update(await value.pids())
                    results = []
                    for case in cases:
                        phase = "scenario"
                        if _case_hook:
                            await _case_hook(value, case)
                        result = await exercise(value, case)
                        if _result_hook:
                            await _result_hook(value, result)
                        results.append(result)
                        completed.append(case)
                        print(json.dumps({"topology": topology, **result}, ensure_ascii=False), flush=True)
                    phase = "assertions"
                    assert value.model.unexpected == 0
                    diagnostics = await asyncio.to_thread(read_diagnostics, directory)
                    print_diagnostics(topology, diagnostics)
                    assert not diagnostics["http_5xx"] and not diagnostics["invalid_http_records"]
                    assert not diagnostics["invalid_process_records"]
                    if _diagnostics_hook:
                        _diagnostics_hook(diagnostics)
                    phase = "teardown"
                phase = "temporary_cleanup"
                return results
            except BaseException as exc:
                await preserve_failure(exc)
                reported_exc = exc
                raise
    except BaseException as exc:
        if exc is not reported_exc:
            await preserve_failure(exc)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", required=True, choices=TOPOLOGIES)
    parser.add_argument("--diagnostics-dir", type=Path, help="失败时保存脱敏诊断的目录；默认为脚本同级 diagnostics")
    parser.add_argument(
        "--cases",
        nargs="+",
        default=["restart", "forward", "revoke", "cancel", "admin-cancel", "delete", "kill"],
        choices=CASES,
    )
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases) or any(case.endswith("-off") for case in args.cases[:-1]):
        parser.error("每个场景只执行一次，关闭内部通道的场景必须最后单独运行")
    try:
        asyncio.run(main(args.topology, args.cases, diagnostics_dir=args.diagnostics_dir))
    except Exception:
        # 异常正文可能含请求、模型内容或密钥；诊断文件仅含白名单字段。
        sys.exit(1)
