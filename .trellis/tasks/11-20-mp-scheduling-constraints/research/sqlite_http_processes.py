"""两个 uvicorn 进程共用临时 SQLite。通过 HTTP 受理任务、保存设置，只读 SQL 识别执行进程。"""

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager, closing
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from tests.helpers import analysis_json, create_ai_config, create_project, register, task_params
from tests.mcp_fakes import serve
from tests.test_agent_providers import openai_chunk


def checked(response, expected):
    assert response.status_code == expected, (response.request.url.path, response.status_code, response.text[:500])
    return response.json()


def facts(database, sql, args=()):
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=2)) as db:
        return db.execute(sql, args).fetchall()


@asynccontextmanager
async def service(directory, env, name):
    with socket.socket() as sock, (directory / f"{name}.log").open("w+") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--fd",
            str(sock.fileno()),
            "--no-access-log",
            pass_fds=(sock.fileno(),),
            cwd=directory,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=url, timeout=2, trust_env=False) as probe:
                async with asyncio.timeout(60):
                    while proc.returncode is None:
                        try:
                            if (await probe.get("/healthz")).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.1)
            if proc.returncode is not None:
                log.seek(0)
                raise RuntimeError(f"{name} 启动失败\n{log.read()[-3000:]}")
            yield url, proc.pid
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 20)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()


class ModelServer:
    def __init__(self, database):
        self.database = database
        self.release = asyncio.Event()
        self.entered = []
        self.completed = []
        self.active = 0
        self.peak = 0
        self.active_by_group = {group: 0 for group in "XYZ"}
        self.peak_by_group = self.active_by_group.copy()

    async def chat(self, request: Request):
        body = await request.json()
        tags = set(re.findall(r"C2MARK-([XYZ][123])", json.dumps(body["messages"], ensure_ascii=False)))
        assert len(tags) == 1, tags
        tag = tags.pop()
        [(task_id, pid)] = facts(
            self.database,
            """
            SELECT t.id,w.pid FROM tasks t JOIN chapters c ON c.id=t.chapter_id
            JOIN task_workers w ON w.worker_key=t.worker_key
            WHERE c.title=? AND t.kind='chapter.analyze' AND t.status='running'
        """,
            (tag,),
        )
        self.entered.append((tag, task_id, pid))
        self.active += 1
        group = tag[0]
        self.active_by_group[group] += 1
        self.peak_by_group[group] = max(self.peak_by_group[group], self.active_by_group[group])
        self.peak = max(self.peak, self.active)
        chunk = openai_chunk({"content": analysis_json(summary=f"完成 {tag}")}, "stop")

        async def stream():
            try:
                # 每次真实请求立即收到响应头，共用信号只延迟正文。所有在场请求可同时开始和结束。
                await self.release.wait()
                await asyncio.sleep(0.25)
                yield f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
                self.completed.append(tag)
            finally:
                self.active -= 1
                self.active_by_group[group] -= 1

        return StreamingResponse(stream(), media_type="text/event-stream")


async def task_status(client, task):
    resp = await client.get(f"/api/v1/tasks/{task['id']}", params=task_params(task))
    return checked(resp, 200)


async def scheduling(authors, admins, user_id, model, url):
    a, b = authors
    await create_ai_config(a, base_url=f"{url}/v1/")
    chapters = {}
    for group, count in (("X", 3), ("Y", 2), ("Z", 1)):
        project = await create_project(a, title=f"共库演练{group}")
        for index in range(1, count + 1):
            tag = f"{group}{index}"
            row = await a.post(
                f"/api/v1/projects/{project['id']}/chapters",
                json={"title": tag, "content": f"这段正文标记为 C2MARK-{tag}。"},
            )
            chapters[tag] = checked(row, 201)["id"]
    tasks = {}

    async def enqueue(client, tag):
        response = await client.post(f"/api/v1/chapters/{chapters[tag]}/analyze")
        tasks[tag] = checked(response, 202)

    async def observed(tag):
        async with asyncio.timeout(20):
            while not any(row[0] == tag for row in model.entered):
                row = await task_status(a, tasks[tag])
                assert row["status"] in {"queued", "running"}, (tag, row["status"], row.get("error"))
                await asyncio.sleep(0.05)

    try:
        await enqueue(a, "X1")
        await observed("X1")
        await enqueue(b, "Y1")
        await observed("Y1")
        for client, tag in ((b, "X2"), (a, "Y2"), (b, "X3"), (a, "Z1")):
            await enqueue(client, tag)
        # 两端调度循环都有至少一次正常轮询机会。剩余任务如越过串行/总并发限制，模型会接到请求。
        await asyncio.sleep(5.25)
        quota_path = f"/api/v1/admin/users/{user_id}/quotas"
        quotas = [
            checked(row, 200)["task_concurrency"]["used"]
            for row in await asyncio.gather(*(c.get(quota_path) for c in admins))
        ]
        positions = []
        for client in authors:
            rows = await asyncio.gather(*(task_status(client, t) for t in tasks.values()))
            positions.append({k: (row["status"], row["queue_position"]) for k, row in zip(tasks, rows, strict=True)})
        first_wave = list(model.entered)
    finally:
        model.release.set()

    async with asyncio.timeout(55):
        while True:
            rows = await asyncio.gather(*(task_status(b, t) for t in tasks.values()))
            assert all(row["status"] in {"queued", "running", "succeeded"} for row in rows), [
                (row["id"], row["status"], row.get("error")) for row in rows
            ]
            if all(row["status"] == "succeeded" for row in rows):
                break
            await asyncio.sleep(0.1)
        while True:
            final_used = [
                checked(row, 200)["task_concurrency"]["used"]
                for row in await asyncio.gather(*(c.get(quota_path) for c in admins))
            ]
            if final_used == [0, 0]:
                break
            await asyncio.sleep(0.1)
    return {
        "peak": model.peak,
        "peak_by_group": model.peak_by_group,
        "first_wave": first_wave,
        "used": quotas,
        "end_used": final_used,
        "positions": positions,
        "calls": model.entered,
        "completed": model.completed,
    }


@asynccontextmanager
async def mcp_probe():
    accepted = []
    tasks = set()

    async def respond(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        accepted.append(time.monotonic())
        try:
            async with asyncio.timeout(3):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        try:
            yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", accepted
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.gather(*tasks)


async def settings(authors, admin, url, accepted):
    a, b = authors
    plugin = checked(await a.post("/api/v1/mcp-plugins", json={"name": "c2-probe", "url": url + "/mcp"}), 201)
    key = checked(await a.post("/api/v1/api-keys", json={"name": "限流验收", "all_projects": True}), 201)
    headers = {"X-API-Key": key["key"]}
    if time.time() % 60 > 51:
        await asyncio.sleep(61 - time.time() % 60)
    # 基线 2 次/分钟。在 B 用尽同一窗口的限额，再由 A 提至 3。
    before = [(await b.get("/api/open/v1/me", headers=headers)).status_code for _ in range(3)]
    assert before == [200, 200, 429], before
    path = "/api/v1/admin/system-settings"
    revision = checked(await admin.get(path), 200)["revision"]
    checked(
        await admin.patch(
            path,
            json={
                "revision": revision,
                "changes": {
                    "registration_enabled": False,
                    "mcp_enabled": False,
                    "open_api_rate_limit_per_minute": 3,
                },
            },
        ),
        200,
    )
    start = time.monotonic()
    # 独立未登录连接避免错误放行注册时覆盖 b 的作者 Cookie。
    async with httpx.AsyncClient(base_url=b.base_url, trust_env=False) as anon:
        registered, plugins, refresh, public = await asyncio.gather(
            anon.post("/api/v1/auth/register", json={"username": "closed_registration", "password": "password123"}),
            b.get("/api/v1/mcp-plugins"),
            b.post(f"/api/v1/mcp-plugins/{plugin['id']}/refresh"),
            anon.get("/api/v1/auth/config"),
        )
    observed_after = round(time.monotonic() - start, 3)
    previous_probe = 0
    deadline = start + 5.3
    while True:
        result = await b.get("/api/open/v1/me", headers=headers)
        elapsed = time.monotonic() - start
        if result.status_code == 200 or time.monotonic() > deadline:
            break
        assert result.status_code == 429, result.status_code
        previous_probe = elapsed
        await asyncio.sleep(0.05)
    again = await b.get("/api/open/v1/me", headers=headers)
    return {
        "registration": registered.status_code,
        "cookie_issued": "set-cookie" in registered.headers,
        "plugin_list": plugins.status_code,
        "refresh": refresh.status_code,
        "config": checked(public, 200),
        "mcp_tcp_connections": len(accepted),
        "safety_checked_s": observed_after,
        "rate_before": before,
        "raised": result.status_code,
        "next": again.status_code,
        "last_old_s": round(previous_probe, 3),
        "rate_observed_s": round(elapsed, 3),
    }


async def run(backend, directory, limit):
    database = directory / "probe.sqlite3"
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(backend),
        "NO_PROXY": "*",
        "LANG": "en_US.UTF-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "JWT_SECRET": secrets.token_hex(32),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ADMIN_USERNAME": "probe_admin",
        "ADMIN_PASSWORD": secrets.token_urlsafe(18),
        "TASK_MAX_CONCURRENCY": "2" if limit == "global" else "6",
        "TASK_MAX_PER_USER": "6" if limit == "global" else "2",
        "AUTO_ANALYZE_ENABLED": "false",
        "OPEN_API_RATE_LIMIT_PER_MINUTE": "2",
        "ALLOW_PRIVATE_AI_ENDPOINTS": "true",
        "BACKUP_DIR": str(directory / "backups"),
        "FRONTEND_DIST_DIR": str(directory / "no-dist"),
    }
    migrate = await asyncio.create_subprocess_exec(
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
    _, errors = await migrate.communicate()
    if migrate.returncode:
        raise RuntimeError(errors.decode()[-2000:])
    model = ModelServer(database)
    model_app = Starlette(routes=[Route("/v1/chat/completions", model.chat, methods=["POST"])])
    async with AsyncExitStack() as stack:
        model_url = await stack.enter_async_context(serve(model_app))
        services = [await stack.enter_async_context(service(directory, env, name)) for name in ("A", "B")]
        admins, authors = [], []
        for url, _ in services:
            clients = [
                await stack.enter_async_context(httpx.AsyncClient(base_url=url, timeout=15, trust_env=False))
                for _ in range(2)
            ]
            admins.append(clients[0])
            authors.append(clients[1])
            checked(
                await clients[0].post(
                    "/api/v1/auth/login",
                    json={
                        "username": env["ADMIN_USERNAME"],
                        "password": env["ADMIN_PASSWORD"],
                    },
                ),
                200,
            )
        user = await register(authors[0], "probe_author")
        checked(
            await authors[1].post("/api/v1/auth/login", json={"username": "probe_author", "password": "secret123"}), 200
        )
        result = await scheduling(authors, admins, user["id"], model, model_url)
        async with mcp_probe() as (mcp_url, accepted):
            config = await settings(authors, admins[0], mcp_url, accepted)
    expected = {
        "X1": ("running", None),
        "Y1": ("running", None),
        "X2": ("queued", 0),
        "Y2": ("queued", 1),
        "X3": ("queued", 2),
        "Z1": ("queued", 3),
    }
    tags = [row[0] for row in result["calls"]]
    pids = [pid for _, pid in services]
    observed_pids = {row[2] for row in result["first_wave"]}
    groups = [["X1", "X2", "X3"], ["Y1", "Y2"], ["Z1"]]
    checks = {
        "both_workers_executed": set(pids) == observed_pids,
        "total_capacity": result["peak"] == len(result["first_wave"]) == 2,
        "serial_capacity": result["peak_by_group"] == {"X": 1, "Y": 1, "Z": 1},
        "shared_quota": result["used"] == [len(result["first_wave"])] * 2,
        "queue_positions": result["positions"] == [expected, expected],
        "fifo_starts_and_completes": all(
            [tag for tag in trace if tag[0] == group[0][0]] == group
            for trace in [tags, result["completed"]]
            for group in groups
        ),
        "closed": (
            config["registration"],
            config["cookie_issued"],
            config["plugin_list"],
            config["refresh"],
            config["config"],
        )
        == (403, False, 403, 403, {"registration_enabled": False, "mcp_enabled": False}),
        "closed_no_mcp_tcp": config["mcp_tcp_connections"] == 0,
        "rate_limit": config["raised"] == 200 and config["next"] == 429 and config["rate_observed_s"] <= 5,
    }
    print(
        json.dumps(
            {
                "limit": limit,
                "pids": pids,
                "schema": facts(database, "SELECT version_num FROM alembic_version")[0][0],
                "scheduling": result,
                "settings": config,
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, default=Path.cwd(), help="要运行的后端源码目录")
    parser.add_argument("--limit", choices=("global", "user"), default="global", help="本轮约束来源")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="ainovel-c2-http-") as workspace:
        asyncio.run(run(args.backend.resolve(strict=True), Path(workspace), args.limit))
