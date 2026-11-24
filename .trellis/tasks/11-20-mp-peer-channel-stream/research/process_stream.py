"""两个独立 Uvicorn 进程共享临时 SQLite，公开 HTTP 订阅连接真实模型适配器。"""

import argparse
import asyncio
import json
import os
import runpy
import secrets
import signal
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

# isort: split
from tests.helpers import create_ai_config, create_project, register, task_params
from tests.mcp_fakes import serve
from tests.test_agent_entries_e4id import _invite, _revoke
from tests.test_agent_providers import openai_chunk
from tests.test_collaboration_stream_agent import DENIED, _ProdStream

TRELLIS = next(path for path in Path(__file__).resolve().parents if path.name == ".trellis")
COMMON = runpy.run_path(
    str(TRELLIS / "tasks/archive/2026-11/11-20-mp-scheduling-constraints/research/sqlite_http_processes.py")
)
service, facts, checked = COMMON["service"], COMMON["facts"], COMMON["checked"]


@dataclass
class ModelCall:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    pieces: asyncio.Queue = field(default_factory=asyncio.Queue)

    def send(self, text):
        self.pieces.put_nowait(text)

    def finish(self):
        self.pieces.put_nowait(None)


class Model:
    """仅脚本化外部模型服务。任务领取、发布、投影、权限和 HTTP 发送均使用应用原路径。"""

    def __init__(self):
        self.waiting = asyncio.Queue()
        self.calls = []
        self.unexpected = 0

    def expect(self):
        call = ModelCall()
        self.waiting.put_nowait(call)
        self.calls.append(call)
        return call

    async def chat(self, request):
        body = await request.json()
        if not self.waiting.qsize() or not body.get("stream") or not body.get("messages"):
            self.unexpected += 1
            return JSONResponse({"error": {"message": "unexpected test model request"}}, status_code=400)
        call = self.waiting.get_nowait()

        async def chunks():
            try:
                call.entered.set()
                while True:
                    text = await call.pieces.get()
                    if text is None:
                        yield f"data: {json.dumps(openai_chunk({}, 'stop'))}\n\ndata: [DONE]\n\n"
                        return
                    yield f"data: {json.dumps(openai_chunk({'content': text}), ensure_ascii=False)}\n\n"
            finally:
                call.closed.set()

        return StreamingResponse(chunks(), media_type="text/event-stream")


@dataclass
class Environment:
    services: list
    model: Model
    model_url: str
    database: Path
    transport: str


@asynccontextmanager
async def environment(backend, directory, transport="uds", *, frontend=None, wal=False):
    database = directory / "stream.sqlite3"
    secret = secrets.token_hex(32)
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(backend),
        "NO_PROXY": "*",
        "LANG": "en_US.UTF-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "JWT_SECRET": secret,
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "REGISTRATION_ENABLED": "true",
        "ADMIN_USERNAME": "probe_admin",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "AUTO_ANALYZE_ENABLED": "false",
        "ALLOW_PRIVATE_AI_ENDPOINTS": "true",
        "BACKUP_DIR": str(directory / "backups"),
        "FRONTEND_DIST_DIR": str(frontend or directory / "no-dist"),
    }
    if transport == "tcp":
        env.update(TASK_PEER_BIND_HOST="127.0.0.1", TASK_PEER_ADVERTISE_HOST="127.0.0.1")
    migration = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(backend / "alembic.ini"),
        "upgrade",
        "head",
        cwd=directory,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, errors = await migration.communicate()
    if migration.returncode:
        raise RuntimeError(errors.decode()[-1500:])
    if wal:
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    model = Model()
    fake_api = Starlette(routes=[Route("/v1/chat/completions", model.chat, methods=["POST"])])
    async with AsyncExitStack() as stack:
        model_url = await stack.enter_async_context(serve(fake_api))
        services = [await stack.enter_async_context(service(directory, env, name)) for name in ("A", "B")]
        try:
            yield Environment(services, model, model_url, database, transport)
        finally:
            for call in model.calls:
                call.finish()


@dataclass
class Book:
    project_id: int
    chapter_id: int
    name: str
    owner: httpx.AsyncClient
    actor: httpx.AsyncClient
    member_id: int | None
    revision: int | None


async def create_book(env, stack, name, *, collaborator=False):
    async def account(username):
        client = await stack.enter_async_context(
            httpx.AsyncClient(base_url=env.services[0][0], timeout=25, trust_env=False),
        )
        user = await register(client, username)
        return client, user

    owner, _ = await account(f"{name}-owner")
    await create_ai_config(owner, base_url=f"{env.model_url}/v1/")
    project = await create_project(owner, title=f"跨进程实时输出 {name}")
    project_id = project["id"]
    chapter_id = checked(
        await owner.post(f"/api/v1/projects/{project_id}/chapters", json={"title": "第一章", "content": ""}), 201
    )["id"]
    actor, member_id, revision = owner, None, None
    if collaborator:
        actor, member = await account(f"{name}-member")
        member_id = member["id"]
        accepted = await _invite(owner, actor, project_id, f"{name}-member", allow_owner_ai=True)
        revision = accepted["revision"]
    return Book(project_id, chapter_id, name, owner, actor, member_id, revision)


async def begin(env, book):
    call = env.model.expect()
    created = await book.actor.post(f"/api/v1/chapters/{book.chapter_id}/generate", json={"auto_continue": False})
    task = checked(created, 202)
    await asyncio.wait_for(call.entered.wait(), 25)
    [(pid, address)] = facts(
        env.database,
        """
        SELECT w.pid,w.peer_url FROM tasks t JOIN task_workers w ON w.worker_key=t.worker_key
        WHERE t.id=? AND t.statistics_key=? AND t.status='running'
    """,
        (task["id"], task["task_key"]),
    )
    direct = next(s for s in env.services if s[1] == pid)
    remote = next(s for s in env.services if s[1] != pid)
    assert address and (("127.0.0.1" in address) == (env.transport == "tcp")), address
    return task, call, direct, remote


async def exercise(env, mode):
    async with AsyncExitStack() as stack:
        book = await create_book(env, stack, mode, collaborator=(mode == "revoke"))
        task, model, (a_url, a_pid), (b_url, b_pid) = await begin(env, book)
        stack.callback(model.finish)
        remote = _ProdStream(b_url, book.actor, task)
        stack.push_async_callback(remote.aclose)
        direct = None
        if mode != "kill":
            direct = _ProdStream(a_url, book.actor, task)
            stack.push_async_callback(direct.aclose)
        streams = [s for s in (direct, remote) if s is not None]
        await asyncio.gather(*(s.wait_for(lambda fs: any(k == "snapshot" for k, _ in fs)) for s in streams))
        model.send("第一段真实模型输出。")
        await asyncio.gather(
            *(s.wait_for(lambda fs: ("delta", {"text": "第一段真实模型输出。"}) in fs) for s in streams)
        )
        if mode == "revoke":
            await _revoke(book.owner, book.project_id, book.member_id, book.revision)
            model.send("撤权提交后的标记。")
            model.finish()
            for stream in streams:
                frames = await stream.finish()
                assert frames[-1] == ("error", DENIED)
                assert "撤权提交后的标记" not in stream.text
        elif mode == "kill":
            # 只能操作本次实际启动的两个进程之一。库中的租约和进程 PID 在下列只读查询中再次交叉核对。
            assert (a_url, a_pid) in env.services and a_pid != b_pid
            assert facts(
                env.database,
                """
                SELECT w.pid FROM tasks t JOIN task_workers w ON w.worker_key=t.worker_key
                WHERE t.id=? AND t.statistics_key=? AND t.status='running'
            """,
                (task["id"], task["task_key"]),
            ) == [(a_pid,)]
            os.kill(a_pid, signal.SIGKILL)
            frames = await remote.finish(within=70)
            ends = [(name, data) for name, data in frames if name in {"done", "error", "paused"}]
            assert len(ends) == 1 and ends[0][0] == "error" and ends[0][1]["task"]["status"] == "interrupted", ends
            replay = _ProdStream(b_url, book.actor, task)
            stack.push_async_callback(replay.aclose)
            replay_frames = await replay.finish()
            assert replay_frames == frames[-2:]
            async with httpx.AsyncClient(base_url=b_url, cookies=book.actor.cookies) as surviving:
                detail = checked(await surviving.get(f"/api/v1/tasks/{task['id']}", params=task_params(task)), 200)
                assert detail["status"] == "interrupted"
        else:
            model.send("第二段真实模型输出。")
            await asyncio.gather(
                *(s.wait_for(lambda fs: ("delta", {"text": "第二段真实模型输出。"}) in fs) for s in streams)
            )
            model.finish()
            first, frames = await asyncio.gather(direct.finish(), remote.finish())
            assert first == frames
            assert frames[-1][0] == "done" and frames[-1][1]["task"]["status"] == "succeeded"
        return {
            "case": mode,
            "executor_pid": a_pid,
            "entry_pid": b_pid,
            "public_ports": [int(url.rsplit(":", 1)[1]) for url in (a_url, b_url)],
            "events": [name for name, _ in frames],
            "terminal": frames[-1][0],
        }


async def main(backend, directory, transport, wal, cases):
    async with environment(backend, directory, transport, wal=wal) as env:
        result = [await exercise(env, case) for case in cases]
        assert env.model.unexpected == 0 and len(env.model.calls) == len(cases)
        print(
            json.dumps(
                {
                    "transport": transport,
                    "sqlite_mode": facts(env.database, "PRAGMA journal_mode")[0][0],
                    "cases": result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--transport", choices=["uds", "tcp"], default="uds")
    parser.add_argument("--wal", action="store_true")
    parser.add_argument(
        "--cases", nargs="+", choices=["forward", "revoke", "kill"], default=["forward", "revoke", "kill"]
    )
    args = parser.parse_args()
    if "kill" in args.cases[:-1] or len(set(args.cases)) != len(args.cases):
        parser.error("每个用例仅运行一次，强杀用例必须在最后")
    with TemporaryDirectory(prefix="ainovel-c4-stream-") as directory_name:
        asyncio.run(main(args.backend.resolve(strict=True), Path(directory_name), args.transport, args.wal, args.cases))
