"""两个真实 Uvicorn 进程的 C5 验收。只使用本脚本创建的临时库、目录及替身。"""

import asyncio
import os
import runpy
import secrets
import socket
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.routing import Route

from tests.helpers import create_ai_config, create_project, register
from tests.mcp_fakes import serve
from tests.test_inspiration import options_json

SCRIPT = Path(__file__).resolve()
TRELLIS = next(parent for parent in SCRIPT.parents if parent.name == ".trellis")
C4 = TRELLIS / "tasks/archive/2026-11/11-20-mp-peer-channel-stream/research/process_stream.py"
API = "/api/v1"
RUNS = f"{API}/admin/backups/runs"
ADMIN = "c5_probe_admin"


def serving(fd):
    import uvicorn

    from app.services.backups import database
    from app.services.backups.process import check_running

    snapshot = database.snapshot_database

    def paused_snapshot(url, output, *, deadline, stop_event, **kwargs):
        Path("entered").write_text(f"{os.getpid()}\n{output.parent}\n", encoding="utf-8")
        while not Path("release").exists():
            check_running(deadline, stop_event)
            time.sleep(0.025)
        return snapshot(url, output, deadline=deadline, stop_event=stop_event, **kwargs)

    database.snapshot_database = paused_snapshot
    uvicorn.run("app.main:app", fd=fd, access_log=False)


@asynccontextmanager
async def service(directory, env, name):
    with socket.socket() as sock, (directory / f"{name}.log").open("w+") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SCRIPT), "--serve", str(sock.fileno()),
            pass_fds=(sock.fileno(),), cwd=directory, env=env, stdout=log, stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=url, timeout=2, trust_env=False) as probe:
                async with asyncio.timeout(30):
                    while proc.returncode is None:
                        try:
                            if (await probe.get("/healthz")).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.1)
            if proc.returncode is not None:
                raise RuntimeError((directory / f"{name}.log").read_text()[-3000:])
            yield url, proc
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 15)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()


async def wait_for_backup_marker(directory):
    async with asyncio.timeout(10):
        while True:
            marker = directory / "entered"
            if marker.exists():
                fields = marker.read_text(encoding="utf-8").splitlines()
                if len(fields) == 2:
                    return int(fields[0]), Path(fields[1])
            await asyncio.sleep(0.025)


async def wait_for_backup_status(client, checked, run_id, status, *, seconds=15):
    async with asyncio.timeout(seconds):
        while True:
            row = checked(await client.get(f"{RUNS}/{run_id}"), 200)
            if row["status"] == status:
                return
            if row["status"] not in ("queued", "running"):
                raise AssertionError((run_id, row["status"], status))
            await asyncio.sleep(0.15)


async def exercise(directory):
    backend = TRELLIS.parent / "backend"
    password = secrets.token_urlsafe(24)
    database = directory / "c5.sqlite3"
    c4 = runpy.run_path(str(C4))
    fixed_env = {
        "PATH": os.environ["PATH"], "PYTHONPATH": str(backend), "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8", "NO_PROXY": "*",
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(), "JWT_SECRET": secrets.token_hex(32),
        "ADMIN_USERNAME": ADMIN, "ADMIN_PASSWORD": password, "REGISTRATION_ENABLED": "true",
        "ALLOW_PRIVATE_AI_ENDPOINTS": "true", "BACKUP_DIR": str(directory / "backups"),
        "FRONTEND_DIST_DIR": str(directory / "no-dist"),
    }
    migration = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"), "upgrade", "head",
        cwd=directory, env=fixed_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, errors = await migration.communicate()
    assert migration.returncode == 0, errors.decode()[-2000:]
    checked, facts = c4["checked"], c4["facts"]
    model = c4["Model"]()
    fake = Starlette(routes=[Route("/v1/chat/completions", model.chat, methods=["POST"])])

    async with AsyncExitStack() as stack:
        async def client(url, **kwargs):
            return await stack.enter_async_context(
                httpx.AsyncClient(base_url=url, timeout=10, trust_env=False, **kwargs),
            )

        model_url = await stack.enter_async_context(serve(fake))
        a_url, a_proc = await stack.enter_async_context(service(directory, fixed_env, "A"))
        admin_a = await client(a_url)
        checked(await admin_a.post(f"{API}/auth/login", json={"username": ADMIN, "password": password}), 200)
        settings = checked(await admin_a.patch(f"{API}/admin/backups/settings", json={
            "revision": 0, "changes": {"encryption_enabled": False},
        }), 200)

        async def accept():
            return checked(await admin_a.post(RUNS, json={
                "request_id": str(uuid4()), "settings_revision": settings["revision"],
            }), 202)["id"]

        first_id = await accept()
        pid, temporary = await wait_for_backup_marker(directory)
        assert pid == a_proc.pid and temporary.is_dir()
        b_url, b_proc = await stack.enter_async_context(service(directory, fixed_env, "B"))
        admin_b = await client(b_url, cookies=admin_a.cookies)
        assert checked(await admin_b.get(f"{RUNS}/{first_id}"), 200)["status"] == "running"
        assert temporary.is_dir()
        (directory / "release").touch()
        await wait_for_backup_status(admin_b, checked, first_id, "succeeded")
        assert not temporary.exists()
        artifact_count = facts(
            database, "SELECT count(*) FROM backup_artifacts WHERE run_id=? AND status='ready'", (first_id,),
        )
        assert artifact_count == [(1,)]

        user_a = await client(a_url)
        await register(user_a, "c5-process-author")
        user_b = await client(b_url, cookies=user_a.cookies)
        source = await create_project(user_a, title="跨进程导入")
        exported = await user_a.get(f"{API}/projects/{source['id']}/backup")
        assert exported.status_code == 200
        preview = checked(await user_a.post(f"{API}/projects/import/preview", files={
            "file": ("project.json", exported.content, "application/json"),
        }), 200)
        result = checked(await user_b.post(f"{API}/projects/import", json={"token": preview["token"]}), 201)
        assert result["project"]["id"] != source["id"]
        checked(await user_a.post(f"{API}/projects/import", json={"token": preview["token"]}), 400)

        await create_ai_config(user_a, base_url=f"{model_url}/v1/")
        session_id = checked(await user_a.post(f"{API}/inspiration/sessions", json={"idea": "跨进程灵感"}), 201)["id"]
        path = f"{API}/inspiration/sessions/{session_id}"
        generation = f"{path}/steps/title/generate"
        call = model.expect()
        generating = asyncio.create_task(user_a.post(generation, json={}))
        try:
            await asyncio.wait_for(call.entered.wait(), 10)
            duplicate = checked(await user_b.post(generation, json={}), 409)
            assert "正在生成" in duplicate["detail"]
        finally:
            call.send(options_json(["山海", "白昼", "长夜", "雨声"]))
            call.finish()
        stream = await generating
        assert stream.status_code == 200 and "event: done" in stream.text and model.unexpected == 0
        state = checked(await user_b.get(path), 200)
        assert len(state["state"]["steps"]["title"]["rounds"]) == 1

        (directory / "release").unlink()
        (directory / "entered").unlink()
        abandoned_id = await accept()
        pid, abandoned = await wait_for_backup_marker(directory)
        loser, remaining = (a_proc, admin_b) if pid == a_proc.pid else (b_proc, admin_a)
        assert pid == loser.pid and abandoned.is_dir()
        started = time.monotonic()
        loser.kill()
        await loser.wait()
        await wait_for_backup_status(remaining, checked, abandoned_id, "interrupted", seconds=50)
        assert not abandoned.exists()
        print(
            "备份在对端启动后成功且产物唯一，导入跨端成功且令牌仅消费一次，"
            f"灵感并发只调用一次模型。真实执行进程强杀后 {time.monotonic() - started:.3f}s "
            "内备份转为中断并清除工作目录。"
        )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serving(int(sys.argv[2]))
    else:
        with TemporaryDirectory(prefix="ainovel-c5-") as tmp:
            asyncio.run(exercise(Path(tmp).resolve()))
