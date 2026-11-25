"""C8：真实旧版备份在快照边界被暂停，验证排空等待与恢复。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from zoneinfo import ZoneInfo

import c8_drill as c8
import httpx
from cryptography.fernet import Fernet

WRAPPER = r"""import os
import time
from pathlib import Path

from app.main import app
from app.services.backups import database
from app.services.backups.process import check_running

gate = Path(os.environ["C8_BACKUP_GATE"])
original_snapshot = database.snapshot_database

def gated_snapshot(*args, deadline, stop_event, **kwargs):
    (gate / "entered").touch()
    while not (gate / "release").exists():
        check_running(deadline, stop_event)
        time.sleep(0.02)
    return original_snapshot(*args, deadline=deadline, stop_event=stop_event, **kwargs)

database.snapshot_database = gated_snapshot
"""


def pg_dump_adapter(root: Path, pg: dict) -> Path:
    """只把应用的 pg_dump 输出转发自本次专属容器，口令仍由容器环境提供。"""
    path = root / "pg_dump_adapter"
    code = f"""#!{sys.executable}
import os, subprocess, sys
cid={pg["cid"]!r}
names=("PATH", "HOME", "TMPDIR", *{c8.DOCKER_ENV!r})
env={{key:os.environ[key] for key in names if key in os.environ}}
args=sys.argv[1:]
if args == ["--version"]:
    raise SystemExit(subprocess.call(["docker", "exec", cid, "pg_dump", "--version"], env=env))
destination=args[args.index("--file")+1]
database=args[args.index("--dbname")+1]
if database != "c8base": raise SystemExit(2)
with open(destination,"wb") as output:
    raise SystemExit(subprocess.call(["docker", "exec", cid, "sh", "-c",
      'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -Fc "$1"',
      "sh", database], stdout=output, env=env))
"""
    path.write_text(code, encoding="utf-8")
    path.chmod(0o700)
    return path


@asynccontextmanager
async def gated_application(root: Path, backend: Path, env: dict):
    wrapper = root / "c8_backup_gate.py"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    gate = root / "gate"
    gate.mkdir(mode=0o700)
    env = dict(env, PYTHONPATH=f"{root}{os.pathsep}{backend}", C8_BACKUP_GATE=str(gate))
    with socket.socket() as sock, (root / "old-backup.log").open("w", encoding="utf-8") as log:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        process = await c8.spawn_owned_process(
            [
                str(backend / ".venv/bin/python"),
                "-m",
                "uvicorn",
                "c8_backup_gate:app",
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
                        raise RuntimeError("gated app failed")
                    try:
                        async with httpx.AsyncClient(base_url=url, timeout=2, trust_env=False) as client:
                            if (await client.get("/healthz")).status_code == 200:
                                break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
            c8.mark("旧版屏障应用就绪", pid=process.pid)
            yield url, gate
        finally:
            (gate / "release").touch(exist_ok=True)
            await c8.cleanup_owned(process)
            c8.mark("旧版屏障应用退出", pid=process.pid, exit_code=process.returncode)


async def exercise(shape: str, root: Path, pg: dict | None) -> None:
    versions = await c8.prepare(root)
    old = versions["old"]
    database = c8.url_for(root, shape, pg, "c8base")
    env = c8.app_env(root, old, database, Fernet.generate_key().decode(), secrets.token_hex(32))
    if pg is not None:
        env.update({key: os.environ[key] for key in c8.DOCKER_ENV if key in os.environ})
        env["BACKUP_PG_DUMP_PATH"] = str(pg_dump_adapter(root, pg))
    await c8.migrate(old, env)
    assert await c8.version(database) == "0043"
    async with (
        gated_application(root, old, env) as (base, gate),
        httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as author,
    ):
        await c8.register(author, c8.AUTHOR)
        project = await c8.project(author, "C8 备份屏障作品")
        admin = await c8.login(base, c8.ADMIN)
        try:
            settings = c8.checked(await admin.get(c8.API + "/admin/backups/settings"), 200)
            assert settings["schedule_enabled"] is False
            future = (datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")) + timedelta(hours=2)).strftime("%H:%M")
            settings = c8.checked(
                await admin.patch(
                    c8.API + "/admin/backups/settings",
                    json={
                        "revision": settings["revision"],
                        "changes": {
                            "encryption_enabled": False,
                            "schedule_enabled": True,
                            "schedule_time": future,
                            "smtp_host": "127.0.0.1",
                            "smtp_from_address": "c8@example.com",
                            "smtp_recipients": ["c8@example.com"],
                        },
                    },
                ),
                200,
            )
            assert settings["schedule_enabled"] is True
            settings = c8.checked(
                await admin.patch(
                    c8.API + "/admin/backups/settings",
                    json={"revision": settings["revision"], "changes": {"schedule_enabled": False}},
                ),
                200,
            )
            assert settings["schedule_enabled"] is False
            c8.mark("旧版每日备份开关", initial=False, enabled=True, disabled=True, future_schedule=True)
            accepted = c8.checked(
                await admin.post(
                    c8.API + "/admin/backups/runs",
                    json={"request_id": str(uuid4()), "settings_revision": settings["revision"]},
                ),
                202,
            )
            async with asyncio.timeout(40):
                while True:
                    if await asyncio.to_thread((gate / "entered").exists):
                        break
                    await asyncio.sleep(0.05)
            overview = c8.checked(await admin.get(c8.API + "/admin/backups"), 200)
            assert overview["active_run"] is not None and overview["active_run"]["id"] == accepted["id"]
            assert overview["active_run"]["status"] == "running"
            c8.mark("备份快照入口被屏障暂停", run_id=accepted["id"], active_status="running", drained=False)
            await asyncio.to_thread((gate / "release").touch)
            async with asyncio.timeout(90):
                while True:
                    detail = c8.checked(await admin.get(f"{c8.API}/admin/backups/runs/{accepted['id']}"), 200)
                    if detail["status"] in {"succeeded", "partial", "failed", "interrupted"}:
                        break
                    await asyncio.sleep(0.1)
            assert detail["status"] == "succeeded"
            overview = c8.checked(await admin.get(c8.API + "/admin/backups"), 200)
            assert overview["active_run"] is None
            download = await admin.get(
                f"{c8.API}/admin/backups/runs/{accepted['id']}/download", params={"destination": "local"}
            )
            assert download.status_code == 200 and download.content
            artifacts = [
                row for row in detail["artifacts"] if row["destination"] == "local" and row["status"] == "ready"
            ]
            checksum = hashlib.sha256(download.content).hexdigest()
            assert len(artifacts) == 1
            assert artifacts[0]["checksum_sha256"] == checksum and artifacts[0]["size_bytes"] == len(download.content)
            snapshot = root / ("manual.sqlite3" if shape == "sqlite" else "manual.dump")
            snapshot.write_bytes(download.content)
            restored = await c8.restore(root, shape, pg, snapshot, "backup_verified")
            assert await c8.version(restored) == "0043"
            count = await c8.query(restored, f"SELECT count(*) FROM projects WHERE id={project['id']}")
            assert count[0][0] == 1
            c8.mark(
                "旧版活动备份完成并恢复验证",
                run_id=accepted["id"],
                status=detail["status"],
                active_run_after=None,
                restored_revision="0043",
                restored_project=True,
                bytes=len(download.content),
                sha256=checksum,
                checksum_matches_backup_record=True,
                pg_dump="专属容器原生命令" if pg is not None else None,
            )
        finally:
            await admin.aclose()


async def main(shape: str, root: Path) -> None:
    if shape == "sqlite":
        await exercise(shape, root, None)
    else:
        async with c8.postgres() as pg:
            await exercise(shape, root, pg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", choices=("sqlite", "postgres"), required=True)
    args = parser.parse_args()
    c8.mark(
        "备份脚本版本",
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
        engine=args.shape,
    )
    with TemporaryDirectory(prefix="ainovel-c8-drill-") as temporary:
        try:
            asyncio.run(main(args.shape, Path(temporary).resolve()))
        except BaseException as exc:
            import traceback

            frames = traceback.extract_tb(exc.__traceback__)
            frame = frames[-1] if frames else None
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "type": type(exc).__name__,
                        "file": Path(frame.filename).name if frame else "",
                        "line": frame.lineno if frame else 0,
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(1) from None
