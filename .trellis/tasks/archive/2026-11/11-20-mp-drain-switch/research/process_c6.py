"""用两个独立 Uvicorn 进程、临时 SQLite 和本地模型验收排空。"""

import argparse
import asyncio
import json
import os
import runpy
import secrets
import socket
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route

# isort: split
from tests.helpers import analysis_json, create_ai_config, create_project, register, task_params
from tests.mcp_fakes import serve
from tests.rbac_helpers import assign_roles, create_role
from tests.test_agent_providers import openai_chunk

SCRIPT = Path(__file__).resolve()
TRELLIS = next(path for path in SCRIPT.parents if path.name == ".trellis")
BACKEND = TRELLIS.parent / "backend"
C4 = runpy.run_path(
    str(TRELLIS / "tasks/archive/2026-11/11-20-mp-peer-channel-stream/research/process_stream.py")
)
checked, facts = C4["checked"], C4["facts"]
API = "/api/v1"
SETTINGS = f"{API}/admin/system-settings"
DRAIN = SETTINGS + "/drain-status"
MAINTENANCE = {"detail": "系统正在维护，暂不受理新任务，请稍后再试", "code": "task_admission_closed"}
ADMIN, AUTHOR, READER = "c6-probe-admin", "c6-probe-author", "c6-probe-reader"
REVIEW_PROMPT = "请提议建立一位新角色，留给我确认"


def run_server(fd: int) -> None:
    import uvicorn

    from app.main import app
    from app.services.task_engine.engine import TaskEngine

    original = TaskEngine._on_success

    def is_target(task_key: str) -> bool:
        target = Path("callback-target")
        return target.exists() and target.read_text() == task_key

    async def hold_callback(self, handler, task, *, execution=None):
        if task.kind == "chapter.generate" and await asyncio.to_thread(is_target, task.statistics_key):
            await asyncio.to_thread(Path("callback-entered").touch)
            await until(lambda: asyncio.to_thread(Path("callback-release").exists), within=45)
        return await original(self, handler, task, execution=execution)

    TaskEngine._on_success = hold_callback
    uvicorn.run(app, fd=fd, access_log=False)


async def until(check, *, within=25):
    async with asyncio.timeout(within):
        while True:
            if result := await check():
                return result
            await asyncio.sleep(0.1)


@asynccontextmanager
async def uvicorn_process(directory: Path, env: dict, name: str):
    with socket.socket() as sock, (directory / f"{name}.log").open("w+") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT), "--serve", str(sock.fileno()),
            pass_fds=(sock.fileno(),), cwd=directory, env=env, stdout=log, stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=url, timeout=2, trust_env=False) as probe:
                async def healthy():
                    if process.returncode is not None:
                        raise RuntimeError((directory / f"{name}.log").read_text()[-2500:])
                    try:
                        return (await probe.get("/healthz")).status_code == 200
                    except httpx.TransportError:
                        return False

                await until(healthy)
            yield url, process
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def save_drain(client, enabled):
    settings = checked(await client.get(SETTINGS), 200)
    saved = checked(await client.patch(SETTINGS, json={
        "revision": settings["revision"],
        "changes": {"task_admission_closed": enabled},
    }), 200)
    assert next(item["value"] for item in saved["items"] if item["key"] == "task_admission_closed") is enabled


async def add_chapter(client, project_id, title, content=""):
    return checked(await client.post(
        f"{API}/projects/{project_id}/chapters", json={"title": title, "content": content},
    ), 201)["id"]


async def create_book(client, title, *, auto_analyze):
    project_id = (await create_project(client, title=title))["id"]
    project = checked(await client.patch(f"{API}/projects/{project_id}", json={"auto_analyze": auto_analyze}), 200)
    assert project["auto_analyze"] is auto_analyze
    return project_id


def active_counts(status):
    return tuple(status[key] for key in ("queued", "running", "paused", "finalizing", "active_backups"))


async def exercise_http(directory, database, model, admins, users, processes):
    admin_a, admin_b = admins
    user_a, user_b = users
    urls = [entry[0] for entry in processes]
    pids = {process.pid for _, process in processes}
    parent_project = await create_book(user_a, "章节与分析一起结束", auto_analyze=True)
    other_project = await create_book(user_a, "排空时已有三项", auto_analyze=False)
    parent_chapter = await add_chapter(user_a, parent_project, "首章")
    chapters = [await add_chapter(user_a, other_project, f"第 {i} 章") for i in range(1, 6)]

    async def generate(client, chapter):
        return await client.post(f"{API}/chapters/{chapter}/generate", json={"auto_continue": False})

    first = model.expect()
    try:
        parent = checked(await generate(user_a, parent_chapter), 202)
        await asyncio.wait_for(first.entered.wait(), 20)
        (directory / "callback-target").write_text(parent["task_key"])
        [(owner_pid,)] = facts(database, """
            SELECT w.pid FROM tasks t JOIN task_workers w ON t.worker_key=w.worker_key WHERE t.id=?
        """, (parent["id"],))
        reader_url = next(url for url, process in processes if process.pid != owner_pid)
        async with AsyncExitStack() as stack:
            remote = C4["_ProdStream"](reader_url, user_a, parent)
            stack.push_async_callback(remote.aclose)
            await remote.wait_for(lambda frames: any(kind == "snapshot" for kind, _ in frames))
            first.send("林远穿过山门，回头看见风中的旗帜。")
            await remote.wait_for(lambda frames: any(kind == "delta" for kind, _ in frames))
            initial = [checked(await generate(user_a, chapter), 202) for chapter in chapters[:3]]

            await save_drain(admin_a, True)
            for secret_owner, admin in zip((user_a, user_b), admins, strict=True):
                assert checked(await generate(secret_owner, chapters[3]), 503) == MAINTENANCE
                observation = checked(await admin.get(DRAIN), 200)
                assert observation["enabled"] is True and active_counts(observation) == (3, 1, 0, 0, 0)
                assert {row["pid"] for row in observation["workers"]} == pids
                assert len(observation["workers"]) == len(pids)
                assert all(set(row) == {"pid", "hostname", "started_at", "heartbeat_at"}
                           for row in observation["workers"])
            assert facts(database, "SELECT count(*) FROM tasks") == [(4,)]
            assert facts(database, "SELECT status FROM chapters WHERE id=?", (chapters[3],)) == [("draft",)]
            assert len(model.calls) == 1 and model.unexpected == 0
            read_key = checked(await user_b.post(f"{API}/api-keys", json={
                "name": "隔离阅读验收", "all_projects": False, "project_ids": [other_project],
            }), 201)
            checked(await user_b.get(
                f"/api/open/v1/novels/{other_project}", headers={"X-API-Key": read_key["key"]},
            ), 200)
            opened = checked(await user_b.get("/api/open/v1/openapi.json"), 200)
            assert all(set(operations) <= {"get", "head", "parameters"} for operations in opened["paths"].values())
            print("两个进程读到 3 排队、1 运行并一致拒绝新受理。API Key 读取仍成功。", flush=True)

            first.finish()

            async def callback_entered():
                return (directory / "callback-entered").exists()

            await until(callback_entered)
            assert checked(await user_b.get(f"{API}/tasks/{parent['id']}", params=task_params(parent)), 200)[
                "status"
            ] == "succeeded"
            waiting = checked(await admin_b.get(DRAIN), 200)
            assert active_counts(waiting) == (3, 0, 0, 1, 0), waiting
            for i in range(3):
                call = model.expect()
                call.send(f"已有任务第 {i + 1} 章继续执行。")
                call.finish()
            analysis = model.expect()
            analysis.send(analysis_json(summary="排空期间自动分析已完成"))
            analysis.finish()
            (directory / "callback-release").touch()
            frames = await remote.finish()
            assert frames[-1][0] == "done", frames[-1][0]

            async def drained():
                observed = checked(await admin_b.get(DRAIN), 200)
                return observed["enabled"] and active_counts(observed) == (0, 0, 0, 0, 0)

            await until(drained, within=45)
            assert facts(database, "SELECT kind,status FROM tasks ORDER BY id") == [
                ("chapter.generate", "succeeded"), *[("chapter.generate", "succeeded")] * len(initial),
                ("chapter.analyze", "succeeded"),
            ]
            assert model.unexpected == 0
            print("父任务成功后的收尾仍计数。已有订阅收到终态，三个排队任务和派生分析全部完成。", flush=True)

        await save_drain(admin_b, False)
        unblocked = model.expect()
        unblocked.send("恢复受理后，林远翻开新篇。")
        unblocked.finish()
        checked(await generate(user_a, chapters[3]), 202)
        await asyncio.wait_for(unblocked.closed.wait(), 20)
        print("在 B 关闭开关后，A 的下一次受理成功。", flush=True)
    finally:
        first.finish()
        (directory / "callback-release").touch()
    return urls


async def exercise_browser(directory, urls, passwords, model, admin, author, reader, frontend):
    reader_user = await register(reader, READER, passwords["reader"])
    role = await create_role(admin, "排空只读", ["settings.read"])
    await assign_roles(admin, reader_user["id"], [role["id"]])
    project = await create_book(author, "浏览器排空验收", auto_analyze=False)
    chapter = await add_chapter(author, project, "已写章节", "林远登上藏书阁，手中的灯照亮了古卷。")
    await add_chapter(author, project, "空白章节一")
    await add_chapter(author, project, "空白章节二")
    (directory / "browser-fixtures.json").write_text(json.dumps({
        "adminOrigin": urls[0], "userOrigin": urls[1], "project": project, "chapter": chapter,
        "reviewPrompt": REVIEW_PROMPT,
        "admin": {"username": ADMIN, "password": passwords["admin"]},
        "author": {"username": AUTHOR, "password": passwords["author"]},
        "reader": {"username": READER, "password": passwords["reader"]},
    }), encoding="utf-8")
    reply = model.expect()
    reply.send("恢复之后，智能体收到保留的消息。")
    reply.finish()
    resumed = model.expect()
    resumed.send("这个提案已经拒绝，轮次继续。")
    resumed.finish()
    process = await asyncio.create_subprocess_exec(
        "node", str(SCRIPT.with_name("browser_c6.cjs")), str(directory),
        cwd=frontend.parent,
    )
    try:
        assert await asyncio.wait_for(process.wait(), 95) == 0
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
    assert reply.closed.is_set() and resumed.closed.is_set() and model.unexpected == 0


async def exercise(directory, frontend):
    database = directory / "c6.sqlite3"
    passwords = {key: secrets.token_urlsafe(24) for key in ("admin", "author", "reader")}
    env = {
        "PATH": os.environ["PATH"], "PYTHONPATH": str(BACKEND), "NO_PROXY": "*",
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "JWT_SECRET": secrets.token_hex(32), "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ADMIN_USERNAME": ADMIN, "ADMIN_PASSWORD": passwords["admin"], "REGISTRATION_ENABLED": "true",
        "TASK_MAX_CONCURRENCY": "1", "TASK_MAX_PER_USER": "1", "ALLOW_PRIVATE_AI_ENDPOINTS": "true",
        "BACKUP_DIR": str(directory / "backups"), "FRONTEND_DIST_DIR": str(frontend or directory / "no-dist"),
    }
    migration = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), "upgrade", "head",
        cwd=directory, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, errors = await migration.communicate()
    assert migration.returncode == 0, errors.decode()[-2200:]
    model = C4["Model"]()
    proposed = False

    async def chat(request):
        nonlocal proposed
        body = await request.json()
        if not proposed and REVIEW_PROMPT in json.dumps(body.get("messages"), ensure_ascii=False):
            assert any(tool["function"]["name"] == "create_character" for tool in body["tools"])
            proposed = True
            tool = {"index": 0, "id": "c6-review", "type": "function",
                    "function": {"name": "create_character", "arguments": '{"name":"林远"}'}}
            chunk = openai_chunk({"tool_calls": [tool]}, "tool_calls")
            return StreamingResponse(iter([f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"]),
                                     media_type="text/event-stream")
        return await model.chat(request)

    fake = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"])])

    async with AsyncExitStack() as stack:
        model_url = await stack.enter_async_context(serve(fake))
        processes = [await stack.enter_async_context(uvicorn_process(directory, env, name)) for name in ("A", "B")]

        async def login(url, name, password):
            client = await stack.enter_async_context(httpx.AsyncClient(base_url=url, timeout=10, trust_env=False))
            if name is not None:
                checked(await client.post(f"{API}/auth/login", json={"username": name, "password": password}), 200)
            return client

        a, b = [url for url, _ in processes]
        admins = [await login(url, ADMIN, passwords["admin"]) for url in (a, b)]
        author = await login(a, None, None)
        await register(author, AUTHOR, passwords["author"])
        await create_ai_config(author, base_url=f"{model_url}/v1/")
        users = [author, await login(b, AUTHOR, passwords["author"])]
        urls = await exercise_http(directory, database, model, admins, users, processes)
        if frontend:
            reader = await login(a, None, None)
            await exercise_browser(directory, urls, passwords, model, admins[0], author, reader, frontend)

        _, peer = processes[1]
        peer.terminate()
        await asyncio.wait_for(peer.wait(), 15)

        async def only_surviving_process():
            observation = checked(await admins[0].get(DRAIN), 200)
            return [row["pid"] for row in observation["workers"]] == [processes[0][1].pid]

        await until(only_surviving_process)
        print("真实进程退出后从存活列表消失。", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--frontend", type=Path, help="可选，构建包目录，另跑 Chrome 验收")
    args = parser.parse_args()
    if args.serve is not None:
        run_server(args.serve)
        return
    frontend = args.frontend.resolve() if args.frontend else None
    with TemporaryDirectory(prefix="ainovel-c6-") as tmp:
        directory = Path(tmp)
        try:
            asyncio.run(exercise(directory, frontend))
        except BaseException:
            for path in sorted(directory.glob("?.log")):
                print(f"进程 {path.stem} 尾部日志\n{path.read_text()[-2400:]}", file=sys.stderr)
            raise


if __name__ == "__main__":
    main()
