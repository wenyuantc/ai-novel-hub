"""用两个真实 uvicorn 进程验证取消送达、管理员受理去重和跨进程作品删除。"""

import argparse
import asyncio
import json
import os
import re
import runpy
import secrets
import signal
import sqlite3
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cryptography.fernet import Fernet

# isort: split
from tests.helpers import create_ai_config, create_project, register, task_params
from tests.test_agent_providers import openai_chunk

TRELLIS = next(path for path in Path(__file__).resolve().parents if path.name == ".trellis")
COMMON = runpy.run_path(
    str(TRELLIS / "tasks/archive/2026-11/11-20-mp-scheduling-constraints/research/sqlite_http_processes.py")
)
service, facts, checked = COMMON["service"], COMMON["facts"], COMMON["checked"]


@dataclass
class Stream:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0


class Model:
    def __init__(self):
        self.streams = {name: Stream() for name in ("owner", "admin", "delete")}
        self.errors = []
        self.requests = set()

    async def handle(self, reader, writer):
        handle = asyncio.current_task()
        self.requests.add(handle)
        waits = []
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            assert headers.startswith(b"POST /v1/chat/completions ")
            length = int(re.search(rb"(?im)^content-length:\s*(\d+)", headers).group(1))
            body = json.loads(await reader.readexactly(length))
            tags = set(re.findall(r"C3MARK-(owner|admin|delete)", json.dumps(body["messages"])))
            assert len(tags) == 1, tags
            stream = self.streams[tags.pop()]
            stream.calls += 1
            delta = json.dumps(openai_chunk({"content": "正在生成。"})).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            writer.write(b"data: " + delta + b"\n\n")
            await writer.drain()
            # 等到至少一次真实增量可以跨过原引擎 2 秒刷库窗口，调用端据已提交正文再暂停执行者。
            await asyncio.sleep(2.3)
            second = json.dumps(openai_chunk({"content": "演练停点。"})).encode()
            writer.write(b"data: " + second + b"\n\n")
            await writer.drain()
            stream.entered.set()
            peer = asyncio.create_task(reader.read(1))
            finish = asyncio.create_task(stream.finish.wait())
            waits.extend((peer, finish))
            ready, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if peer in ready:
                try:
                    assert not peer.result()
                except ConnectionResetError:
                    pass
                stream.disconnected.set()
            else:
                writer.write(b"data: " + json.dumps(openai_chunk({}, "stop")).encode() + b"\n\ndata: [DONE]\n\n")
                await writer.drain()
        except Exception as exc:
            self.errors.append(repr(exc))
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.requests.discard(handle)


def snapshot(database, task, project_id, chapter_id, has_c3):
    # 两种版本使用同一个观察器；每次采样中的所有字段取自同一条 SELECT。
    extras = (
        """t.cancel_requested_at IS NOT NULL,
           (SELECT count(*) FROM task_work_records r WHERE r.task_key=t.statistics_key AND r.kind='execution')"""
        if has_c3
        else "NULL, NULL"
    )
    [(state, admin, chapter, claim, deleted, token, cancel_pending, bodies)] = facts(
        database,
        f"""SELECT t.status,t.admin_cancel_operation_id,c.status,c.generation_claim_key,
                   p.deleted_at,p.deletion_token,{extras}
            FROM tasks t JOIN chapters c ON c.id=? JOIN projects p ON p.id=?
            WHERE t.id=? AND t.statistics_key=?""",
        (chapter_id, project_id, task["id"], task["task_key"]),
    )
    return {
        "task": state,
        "admin_pending": admin is not None,
        "chapter": chapter,
        "claimed": claim is not None,
        "deleted": deleted is not None,
        "deleting": token is not None,
        "owner_requested": cancel_pending,
        "execution_work": bodies,
    }


async def exercise(name, database, services, authors, admins, stream):
    author = authors[1] if name == "admin" else authors[0]
    project_id = (await create_project(author, title=f"取消演练 {name}"))["id"]
    chapter_id = checked(
        await author.post(f"/api/v1/projects/{project_id}/chapters", json={"title": name, "content": ""}), 201
    )["id"]
    task = checked(
        await author.post(
            f"/api/v1/chapters/{chapter_id}/generate", json={"instruction": f"C3MARK-{name}", "auto_continue": False}
        ),
        202,
    )
    await asyncio.wait_for(stream.entered.wait(), 25)
    async with asyncio.timeout(5):
        while True:
            [(progress,)] = facts(database, "SELECT output_text FROM tasks WHERE statistics_key=?", (task["task_key"],))
            if progress and "演练停点" in progress:
                break
            await asyncio.sleep(0.02)
    [(pid,)] = facts(
        database,
        """SELECT w.pid FROM tasks t JOIN task_workers w ON t.worker_key=w.worker_key
                                  WHERE t.id=? AND t.statistics_key=? AND t.status='running'""",
        (task["id"], task["task_key"]),
    )
    # 仅对本演练启动的两个子进程发信号。执行者由数据库识别，请求必定发到另一个进程。
    pids = [process_id for _, process_id in services]
    remote = 1 - pids.index(pid)
    other_pid = pids[remote]
    task_path = f"/api/v1/tasks/{task['id']}/cancel"
    admin_path = f"/api/v1/admin/tasks/{task['task_key']}/cancel"
    admin_body = {"confirm": True, "reason": "演练取消运行中任务"}

    async def request():
        if name == "owner":
            return [await authors[remote].post(task_path, params=task_params(task))]
        if name == "delete":
            return [await authors[remote].delete(f"/api/v1/projects/{project_id}")]
        return [await admins[remote].post(admin_path, json=admin_body) for _ in range(2)]

    has_c3 = "cancel_requested_at" in {r[1] for r in facts(database, "PRAGMA table_info(tasks)")}
    before = snapshot(database, task, project_id, chapter_id, has_c3)
    assert before["task"] == "running" and before["claimed"], before
    os.kill(pid, signal.SIGSTOP)
    command = asyncio.create_task(request())
    try:
        # 短于默认 30 秒的 worker 租约；等待受理事务或旧版提前响应。
        started = time.monotonic()
        while not command.done() and time.monotonic() - started < 3:
            pending = snapshot(database, task, project_id, chapter_id, has_c3)
            if name == "delete" and (pending["deleting"] or pending["deleted"]):
                break
            if name == "owner" and pending["owner_requested"]:
                break
            await asyncio.sleep(0.025)
        # 给接收端处理同一意图的机会，此时原进程仍无法运行。
        await asyncio.sleep(0.15)
        frozen = snapshot(database, task, project_id, chapter_id, has_c3)
        frozen.update(http_returned=command.done(), socket_closed=stream.disconnected.is_set())
    finally:
        os.kill(pid, signal.SIGCONT)
    try:
        responses = await asyncio.wait_for(command, 20)
        codes = [r.status_code for r in responses]
        at_reply = snapshot(database, task, project_id, chapter_id, has_c3)
        # 原进程恢复后，模型服务不主动发结束帧；观察真正由 SDK 端先关闭的连接。
        deadline = time.monotonic() + 7
        while True:
            final = snapshot(database, task, project_id, chapter_id, has_c3)
            slot = facts(
                database, "SELECT COUNT(*) FROM task_execution_slots WHERE statistics_key=?", (task["task_key"],)
            )[0][0]
            if final["task"] != "running" and not final["admin_pending"] and not slot and stream.disconnected.is_set():
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        logs = [
            r[0]
            for r in facts(database, "SELECT status FROM generation_logs WHERE task_id=? ORDER BY id", (task["id"],))
        ]
        operation_ids = [r.json().get("operation_id") for r in responses] if name == "admin" else []
        audits = facts(
            database,
            """SELECT count(*) FROM admin_audit_logs
               WHERE action='task.cancel_request' AND target_id=? AND result='success'""",
            (task["id"],),
        )[0][0]
        result = {
            "executor_pid": pid,
            "request_pid": other_pid,
            "task": task["id"],
            "frozen": frozen,
            "http": codes,
            "observed_after_http": at_reply,
            "socket_closed_by_client": stream.disconnected.is_set(),
            "final": final,
            "used_slots": slot,
            "generation_logs": logs,
            "model_calls": stream.calls,
            "admin_operations": operation_ids,
            "admin_success_audits": audits,
        }
        valid = (
            frozen["task"] == "running"
            and frozen["claimed"]
            and (not has_c3 or frozen["execution_work"] == 1)
            and not frozen["deleted"]
            and not frozen["socket_closed"]
            and final["task"] == "cancelled"
            and not final["claimed"]
            and not final["admin_pending"]
            and stream.disconnected.is_set()
            and not slot
            and logs == ["cancelled"]
            and stream.calls == 1
        )
        if name == "admin":
            valid = valid and frozen["http_returned"] and frozen["admin_pending"] and codes == [202, 202]
            valid = valid and bool(operation_ids[0]) and operation_ids[0] == operation_ids[1] and audits == 1
        elif name == "owner":
            valid = (
                valid
                and not frozen["http_returned"]
                and (not has_c3 or bool(frozen["owner_requested"]))
                and codes == [200]
                and responses[0].json()["status"] == "cancelled"
            )
        else:
            valid = (
                valid
                and frozen["deleting"]
                and not frozen["http_returned"]
                and codes == [204]
                and at_reply["deleted"]
                and (not has_c3 or at_reply["execution_work"] == 0)
            )
        result["passed"] = valid
        return result
    finally:
        stream.finish.set()


async def run(backend, directory):
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
        "AUTO_ANALYZE_ENABLED": "false",
        "ALLOW_PRIVATE_AI_ENDPOINTS": "true",
        "BACKUP_DIR": str(directory / "backups"),
        "FRONTEND_DIST_DIR": str(directory / "no-dist"),
    }
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
        raise RuntimeError(errors.decode()[-2000:])
    model = Model()
    result = {}
    async with await asyncio.start_server(model.handle, "127.0.0.1", 0) as server, AsyncExitStack() as stack:
        services = [await stack.enter_async_context(service(directory, env, name)) for name in ("A", "B")]
        admins, authors = [], []
        for url, _ in services:
            for clients in (admins, authors):
                clients.append(
                    await stack.enter_async_context(httpx.AsyncClient(base_url=url, timeout=22, trust_env=False))
                )
            checked(
                await admins[-1].post(
                    "/api/v1/auth/login", json={"username": env["ADMIN_USERNAME"], "password": env["ADMIN_PASSWORD"]}
                ),
                200,
            )
        await register(authors[0], "probe_author")
        checked(
            await authors[1].post("/api/v1/auth/login", json={"username": "probe_author", "password": "secret123"}), 200
        )
        await create_ai_config(authors[0], base_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/")
        try:
            for name, stream in model.streams.items():
                result[name] = await exercise(name, database, services, authors, admins, stream)
        finally:
            for stream in model.streams.values():
                stream.finish.set()
    await asyncio.gather(*model.requests)
    result["model_errors"] = model.errors
    result["schema"] = facts(database, "SELECT version_num FROM alembic_version")[0][0]
    result["sqlite_version"] = sqlite3.sqlite_version
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return all(result[n]["passed"] for n in model.streams) and not model.errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="ainovel-c3-http-") as tmp:
        passed = asyncio.run(run(args.backend.resolve(strict=True), Path(tmp)))
    raise SystemExit(0 if passed else 1)
