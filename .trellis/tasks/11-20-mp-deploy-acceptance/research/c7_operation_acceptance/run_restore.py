"""在专属 SQLite 或 PostgreSQL 中演练排空、备份、停服、恢复和重新受理。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir

import run_operations as op
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.routing import Route

OBSERVE_APP = r"""import os
from pathlib import Path

from starlette.responses import JSONResponse

import app.main as application_module
from app.main import app as application

async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"] == "/__c7_probe__":
        engine = application.state.task_engine
        body = {"pid": os.getpid(), "source": str(Path(application_module.__file__).resolve()),
                "request_live": engine.request_worker.valid, "scheduler_live": engine.lease.valid}
        return await JSONResponse(body, headers={"x-c7-pid": str(os.getpid())})(scope, receive, send)
    async def tagged(message):
        if message["type"] == "http.response.start":
            message.setdefault("headers", []).append((b"x-c7-pid", str(os.getpid()).encode()))
        await send(message)
    await application(scope, receive, tagged)
"""


async def migrate(workspace, env):
    await op.command(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(op.BACKEND / "alembic.ini"),
        "upgrade",
        "head",
        cwd=workspace,
        env=env,
        limit_s=180,
    )


async def restore_sqlite(root, workspace, env, snapshot):
    database = root / "source.sqlite3"
    rollback = root / "sqlite-rollback"

    def save_previous_group():
        rollback.mkdir(mode=0o700)
        moved = []
        for base in (database, Path(f"{database}.leases.sqlite3"), Path(f"{database}.leases.lock")):
            for suffix in ("", "-journal", "-wal", "-shm"):
                source = Path(f"{base}{suffix}")
                if source.exists():
                    source.rename(rollback / source.name)
                    moved.append(source.name)
        return moved

    moved = await asyncio.to_thread(save_previous_group)
    previous_lock = rollback / f"{database.name}.leases.lock"
    assert database.name in moved and previous_lock.is_file()
    await op.command(
        sys.executable,
        "-m",
        "app.services.backups.sqlite_restore",
        str(snapshot),
        str(database),
        "--previous-lock",
        str(previous_lock),
        cwd=workspace,
        env=env,
    )
    paths = (database, Path(f"{database}.leases.sqlite3"), Path(f"{database}.leases.lock"), rollback / database.name)
    assert await asyncio.to_thread(lambda: all(path.is_file() for path in paths))
    return env["DATABASE_URL"], {"previous_group_saved": True, "new_lease_pair": True, "moved_files": len(moved)}


async def restore_pg(container, source_url, snapshot):
    db_name = f"c7restore_{secrets.token_hex(6)}"
    await op.command(
        "docker",
        "exec",
        container,
        "sh",
        "-c",
        'PGPASSWORD="$POSTGRES_PASSWORD" exec createdb -h 127.0.0.1 -U "$POSTGRES_USER" '
        '--maintenance-db=postgres --template=template0 -- "$1"',
        "sh",
        db_name,
    )
    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" '
        '--exit-on-error --no-owner --no-privileges --dbname="$1"',
        "sh",
        db_name,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    record = {
        "program": "docker",
        "phase": op.RUN["phase"],
        "pid": process.pid,
        "started_s": round(time.monotonic() - op.STARTED, 3),
    }
    op.RUN["commands"].append(record)
    try:
        _, errors = await asyncio.wait_for(process.communicate(snapshot.read_bytes()), 120)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        record.update(exit_code=process.returncode, ended_s=round(time.monotonic() - op.STARTED, 3))
        raise
    record.update(exit_code=process.returncode, ended_s=round(time.monotonic() - op.STARTED, 3))
    if process.returncode:
        raise op.CommandFailure(op.safe_command_category(errors), "docker", process.returncode, process.pid)
    return source_url.rsplit("/", 1)[0] + "/" + db_name, {"new_database": True, "pg_restore_exit": 0}


async def exercise(shape, root, database_url, pg_dump, db_version, pg_container):
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "c7_probe_app.py").write_text(OBSERVE_APP, encoding="utf-8")
    password = secrets.token_urlsafe(24)
    fake_model_key = "c7-fake-model-key-" + secrets.token_hex(16)
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": f"{workspace}{os.pathsep}{op.BACKEND}",
        "NO_PROXY": "*",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "DATABASE_URL": database_url,
        "JWT_SECRET": secrets.token_hex(32),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ADMIN_USERNAME": op.ADMIN,
        "ADMIN_PASSWORD": password,
        "REGISTRATION_ENABLED": "true",
        "AUTO_ANALYZE_ENABLED": "false",
        "ALLOW_PRIVATE_AI_ENDPOINTS": "true",
        "BACKUP_DIR": str(workspace / "backups"),
        "FRONTEND_DIST_DIR": str(workspace / "no-dist"),
    }
    if pg_dump:
        env.update(
            BACKUP_PG_DUMP_PATH=str(pg_dump),
            TASK_PEER_BIND_HOST="127.0.0.1",
            TASK_PEER_ADVERTISE_HOST="127.0.0.1",
        )
    op.mark("初始迁移")
    await migrate(workspace, env)
    model = op.C4["Model"]()
    credential_checks = []

    async def chat(request):
        credential_checks.append(request.headers.get("authorization") == f"Bearer {fake_model_key}")
        return await model.chat(request)

    fake = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"])])
    try:
        async with AsyncExitStack() as outer:
            model_url = await outer.enter_async_context(op.serve(fake))
            apps = await outer.enter_async_context(AsyncExitStack())
            op.mark("启动源应用与虚构数据")
            services = [await apps.enter_async_context(op.app_process(workspace, env, name)) for name in ("A", "B")]
            clients = await op.clients_by_pid(apps, services, 2)
            initial_pids = list(clients)
            op.RUN["worker_pids"].extend(initial_pids)
            admin = next(iter(clients.values()))
            op.checked(await admin.post(f"{op.API}/auth/login", json={"username": op.ADMIN, "password": password}), 200)
            author = await op.pinned_client(apps, str(admin.base_url), initial_pids[0])
            await op.register(author, op.AUTHOR)
            await op.create_ai_config(author, base_url=f"{model_url}/v1/", api_key=fake_model_key)
            project = await op.create_project(author, title="C7 隔离恢复样本")
            project_id = project["id"]
            op.checked(await author.patch(f"{op.API}/projects/{project_id}", json={"auto_analyze": False}), 200)
            chapter = op.checked(
                await author.post(
                    f"{op.API}/projects/{project_id}/chapters",
                    json={"title": "恢复前的章节", "content": "这是专用于恢复验收的虚构内容。"},
                ),
                201,
            )
            running_chapter = await op.add_chapter(author, project_id, "排空中的任务")
            reopen_chapter = await op.add_chapter(author, project_id, "恢复后重新受理")
            op.checked(
                await admin.patch(
                    f"{op.API}/admin/backups/settings", json={"revision": 0, "changes": {"encryption_enabled": False}}
                ),
                200,
            )

            op.mark("排空并完成已受理任务")
            initial_call = model.expect()
            task = op.checked(await author.post(f"{op.API}/chapters/{running_chapter}/generate", json={}), 202)
            await asyncio.wait_for(initial_call.entered.wait(), 25)
            await op.save_settings(admin, task_admission_closed=True)
            rejected = await author.post(f"{op.API}/chapters/{reopen_chapter}/generate", json={})
            assert rejected.status_code == 503 and rejected.json() == op.MAINTENANCE
            initial_call.send("排空之前已受理的任务完成。")
            initial_call.finish()
            await op.task_state(author, task, {"succeeded"})

            async def drained():
                state = op.checked(await admin.get(op.DRAIN), 200)
                empty = all(state[key] == 0 for key in ("queued", "running", "paused", "finalizing", "active_backups"))
                return state if state["enabled"] and empty else None

            await op.until(drained, within=60)
            op.mark("手动备份与隔离校验")
            backup = await op.backup_after_drain(workspace, database_url, admin, pg_container)
            downloaded = await admin.get(f"{op.RUNS}/{backup['run_id']}/download", params={"destination": "local"})
            assert downloaded.status_code == 200
            data = downloaded.content
            row = await op.sql(
                database_url,
                "SELECT checksum_sha256,size_bytes FROM backup_artifacts WHERE run_id=:id AND status='ready'",
                {"id": backup["run_id"]},
            )
            assert len(row) == 1 and (hashlib.sha256(data).hexdigest(), len(data)) == tuple(row[0])
            snapshot = workspace / ("snapshot.sqlite3" if shape == "sqlite" else "snapshot.dump")
            snapshot.write_bytes(data)
            op.mark("停止全部源进程")
            await apps.aclose()
            assert all(process.returncode is not None for _, process in services)
            op.mark("冷恢复目标")
            if shape == "sqlite":
                restored_url, restore_details = await restore_sqlite(root, workspace, env, snapshot)
            else:
                restored_url, restore_details = await restore_pg(pg_container, database_url, snapshot)
            restored_env = dict(env, DATABASE_URL=restored_url)
            before_migration = (await op.sql(restored_url, "SELECT version_num FROM alembic_version"))[0][0]
            assert before_migration == "0070"
            op.mark("匹配版本迁移和启动")
            await migrate(workspace, restored_env)
            restored = await outer.enter_async_context(AsyncExitStack())
            after_services = [
                await restored.enter_async_context(op.app_process(workspace, restored_env, name))
                for name in ("A-restored", "B-restored")
            ]
            after = await op.clients_by_pid(restored, after_services, 2)
            restored_pids = list(after)
            op.RUN["worker_pids"].extend(restored_pids)
            new_admin = next(iter(after.values()))
            assert Path(op.checked(await new_admin.get("/__c7_probe__"), 200)["source"]).is_relative_to(op.BACKEND)
            op.checked(await new_admin.get(f"{op.API}/auth/config"), 200)
            op.checked(
                await new_admin.post(f"{op.API}/auth/login", json={"username": op.ADMIN, "password": password}), 200
            )
            new_author = await op.pinned_client(restored, str(new_admin.base_url), restored_pids[0])
            op.checked(
                await new_author.post(f"{op.API}/auth/login", json={"username": op.AUTHOR, "password": "secret123"}),
                200,
            )
            loaded = op.checked(await new_author.get(f"{op.API}/projects/{project_id}"), 200)
            loaded_chapter = op.checked(
                await new_author.get(f"{op.API}/projects/{project_id}/chapters/{chapter['id']}"), 200
            )
            assert loaded["title"] == "C7 隔离恢复样本"
            assert loaded_chapter["content"] == "这是专用于恢复验收的虚构内容。"
            state = op.checked(await new_admin.get(op.DRAIN), 200)
            assert state["enabled"]
            still_rejected = await new_author.post(f"{op.API}/chapters/{reopen_chapter}/generate", json={})
            assert still_rejected.status_code == 503 and still_rejected.json() == op.MAINTENANCE

            async def restored_drained():
                state = op.checked(await new_admin.get(op.DRAIN), 200)
                return (
                    state
                    if state["enabled"]
                    and all(state[key] == 0 for key in ("queued", "running", "paused", "finalizing", "active_backups"))
                    else None
                )

            await op.until(restored_drained, within=90)
            op.mark("恢复任务受理")
            await op.save_settings(new_admin, task_admission_closed=False)
            reopened_call = model.expect()
            reopened_call.send("恢复目标已重新受理任务。")
            reopened_call.finish()
            reopened = op.checked(await new_author.post(f"{op.API}/chapters/{reopen_chapter}/generate", json={}), 202)
            await op.task_state(new_author, reopened, {"succeeded"})
            assert len(credential_checks) == 2 and all(credential_checks) and model.unexpected == 0
            final_revision = (await op.sql(restored_url, "SELECT version_num FROM alembic_version"))[0][0]
            assert final_revision == before_migration
            result = {
                "shape": shape,
                "database": db_version,
                "initial_pids": initial_pids,
                "restored_pids": restored_pids,
                "all_source_processes_stopped": True,
                "drain_rejected_new_before_backup": True,
                "already_running_finished": True,
                "backup_checksum_verified": True,
                "backup_bytes": len(data),
                "backup_revision": backup["restored_revision"],
                "restoration": restore_details,
                "restored_revision_before_after_migration": [before_migration, final_revision],
                "restored_author_login": True,
                "restored_admin_login": True,
                "restored_project_and_chapter": True,
                "same_encryption_key_and_model_credential_usable": True,
                "restored_drain_rejected_new": True,
                "reopened_task": "succeeded",
                "model_calls": len(credential_checks),
                "alembic_sha256": hashlib.sha256((op.BACKEND / "alembic/env.py").read_bytes()).hexdigest(),
            }
            await restored.aclose()
            return result
    finally:
        for call in model.calls:
            call.finish()


async def main(shape, root):
    asyncio.get_running_loop().set_exception_handler(op.safe_loop_exception)
    op.mark("准备独占隔离资源")
    if shape == "sqlite":
        return await exercise(shape, root, f"sqlite+aiosqlite:///{root / 'source.sqlite3'}", None, "SQLite", None)
    async with op.postgres(root) as (url, pg_dump, version, container):
        return await exercise(shape, root, url, pg_dump, version, container)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shape", required=True, choices=("sqlite", "postgres"))
    parser.add_argument("--diagnostics-dir", type=Path, default=Path(__file__).resolve().parent / "failures")
    args = parser.parse_args()
    op.RUN.update(shape=f"restore-{args.shape}", from_c6=False)
    exit_code = 0
    with TemporaryDirectory(prefix="ainovel-c7-restore-") as temp:
        root = Path(temp).resolve()
        try:
            result = asyncio.run(main(args.shape, root))
            if op.RUN["unhandled_async"]:
                raise RuntimeError("恢复验收出现未处理的异步任务异常")
        except BaseException as exc:
            path = None
            for destination in (args.diagnostics_dir, Path(gettempdir()) / "ainovel-c7-diagnostics"):
                try:
                    path = op.persist_failure(root, destination, exc)
                    break
                except Exception:
                    continue
            if path is None:
                print("恢复验收失败，无法写入脱敏诊断。", file=sys.stderr, flush=True)
                exit_code = 2
            else:
                print(f"恢复验收失败，脱敏诊断：{path}", file=sys.stderr, flush=True)
                exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    if root.exists():
        print("专属临时目录未清理。", file=sys.stderr, flush=True)
        raise SystemExit(2)
    if exit_code:
        print("专属临时资源已清理。", file=sys.stderr, flush=True)
        raise SystemExit(exit_code)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
