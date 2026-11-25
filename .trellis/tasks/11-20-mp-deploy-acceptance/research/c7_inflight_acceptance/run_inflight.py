"""C7 AC1: 真实 MCP 调用及作品删除期间，另一进程启动不能破坏原工作。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import runpy
import signal
import tempfile
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

SCRIPT = Path(__file__).resolve()
COMMON_SCRIPT = SCRIPT.parents[1] / "c7_task_acceptance/process_tasks.py"
ROOT = next((path for path in SCRIPT.parents if (path / "backend/alembic.ini").is_file()), None)
BOOTSTRAP_ERROR = None
try:
    import httpx

    from tests.helpers import wait_task

    if ROOT is None:
        raise RuntimeError("Repository root unavailable")
    COMMON = runpy.run_path(str(COMMON_SCRIPT))
    world_context = COMMON["world"]
    service = COMMON["service"]
    wait_until = COMMON["wait_until"]
    agent_setup = COMMON["agent_setup"]
except BaseException as error:
    # 依赖加载失败同样走安全报告，避免输出导入过程中的原始异常。
    BOOTSTRAP_ERROR = error


def checked(response, expected):
    assert response.status_code == expected, (response.request.url.path, response.status_code, response.text[:1000])
    return response.json() if response.content else None


async def start_additional_process(world, name):
    """只增加本验收启动的应用实例或 Uvicorn worker，不终止任何现有进程。"""
    before = await world.pids()
    if world.topology == "pg-workers":
        supervisor = world.processes[0].process
        assert supervisor.returncode is None
        os.kill(supervisor.pid, signal.SIGTTIN)

        async def new_worker():
            for pid in (await world.pids()) - before:
                if await world.owns_pid(pid):
                    return pid
            return None

        pid = await wait_until(new_worker, seconds=30)
    else:
        instance = await world.stack.enter_async_context(service(world.directory, world.env, name))
        world.processes.append(instance)
        pid = instance.process.pid
        assert await world.owns_pid(pid)

    assert pid not in before
    async with httpx.AsyncClient(
        timeout=5, trust_env=False, limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        response = await world.request(client, pid, "GET", "/healthz")
    assert response.status_code == 200
    return pid


async def current_external(world, change_id):
    rows = await world.rows(
        "SELECT a.status,a.external_worker_key,w.pid,r.state,r.worker_key "
        "FROM agent_changes a LEFT JOIN task_workers w ON w.worker_key=a.external_worker_key "
        "LEFT JOIN task_work_records r ON r.kind='external' AND r.operation_id=a.change_key "
        "WHERE a.id=:id",
        {"id": change_id},
    )
    assert len(rows) == 1, rows
    return tuple(rows[0])


async def external_scenario(world):
    async with AsyncExitStack() as stack:
        scene = await agent_setup(world, stack, "inflight-external")
        author, remote, base, task, change = (scene[key] for key in ("client", "remote", "base", "task", "change"))
        owner = await world.remote(-1)
        payload = {"change_key": change["change_key"], "view_version": change["view_version"]}
        pending = asyncio.create_task(world.request(
            author, owner, "POST", f"{base}/changes/{change['id']}/confirm", json=payload,
        ))
        try:
            await asyncio.wait_for(remote.entered.wait(), 20)
            before = await current_external(world, change["id"])
            assert before[0] == "executing" and before[1] and before[2] == owner, before
            assert before[3:] == ("active", before[1]), before
            assert remote.calls == [{"name": "send", "arguments": {"to": "测试编辑部"}}]
            assert not pending.done()
            new_pid = await start_additional_process(world, "external-new")
            after = await current_external(world, change["id"])
            assert after == before, (before, after)
            assert not pending.done() and len(remote.calls) == 1
            model = world.model.expect()
            model.send("C7 外部工具已完成。")
            model.finish()
            remote.release.set()
            result = await asyncio.wait_for(pending, 30)
            result_json = checked(result, 200)
            assert result_json["status"] == "executed" and result_json["result"] == {"text": "C7 外部调用完成"}
            completed = await wait_task(author, task, within=30, statuses=("succeeded", "failed"))
            assert completed["status"] == "succeeded", completed
            final = await current_external(world, change["id"])
            assert final == ("executed", None, None, None, None), final
            assert len(remote.calls) == 1 and model.entered.is_set() and world.model.unexpected == 0
            return {"case": "external", "owner_pid": owner, "new_pid": new_pid,
                    "mcp_calls": len(remote.calls), "result": "executed", "task": "succeeded"}
        finally:
            remote.release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def current_deletion(world, project_id):
    rows = await world.rows(
        "SELECT p.deletion_token,p.deletion_worker_key,w.pid,p.deleted_at "
        "FROM projects p LEFT JOIN task_workers w ON w.worker_key=p.deletion_worker_key "
        "WHERE p.id=:id", {"id": project_id},
    )
    assert len(rows) == 1, rows
    return tuple(rows[0])


async def deletion_scenario(world):
    async with AsyncExitStack() as stack:
        scene = await agent_setup(world, stack, "inflight-deletion")
        author, remote, base, task, change, project = (
            scene[key] for key in ("client", "remote", "base", "task", "change", "project")
        )
        confirm_pid = await world.remote(-1)
        delete_pid = await world.remote(confirm_pid)
        payload = {"change_key": change["change_key"], "view_version": change["view_version"]}
        confirmation = asyncio.create_task(world.request(
            author, confirm_pid, "POST", f"{base}/changes/{change['id']}/confirm", json=payload,
        ))
        deletion = None
        try:
            await asyncio.wait_for(remote.entered.wait(), 20)
            external_before = await current_external(world, change["id"])
            assert external_before[0] == "executing" and external_before[2] == confirm_pid, external_before
            assert external_before[3:] == ("active", external_before[1]), external_before
            assert len(remote.calls) == 1
            deletion = asyncio.create_task(world.request(
                author, delete_pid, "DELETE", f"/api/v1/projects/{project['id']}",
            ))

            async def intent():
                row = await current_deletion(world, project["id"])
                return row if row[0] else None

            before = await wait_until(intent, seconds=15)
            assert before[1] and before[2] == delete_pid and before[3] is None, before
            assert not deletion.done() and not confirmation.done()
            new_pid = await start_additional_process(world, "deletion-new")
            after = await current_deletion(world, project["id"])
            external_after = await current_external(world, change["id"])
            assert before == after and external_before == external_after, (
                before, after, external_before, external_after,
            )
            assert not deletion.done() and not confirmation.done() and len(remote.calls) == 1
            remote.release.set()
            response = await asyncio.wait_for(deletion, 30)
            assert response.status_code == 204, response.text
            result = await asyncio.wait_for(confirmation, 30)
            assert result.status_code == 409, result.text
            final = await current_deletion(world, project["id"])
            assert final[:3] == (None, None, None) and final[3] is not None, final
            rows = await world.rows("SELECT status FROM tasks WHERE id=:id", {"id": task["id"]})
            assert rows == [("cancelled",)], rows
            external_final = await current_external(world, change["id"])
            assert external_final == ("failed", None, None, None, None), external_final
            assert len(remote.calls) == 1 and world.model.unexpected == 0
            return {"case": "deletion", "external_pid": confirm_pid, "owner_pid": delete_pid,
                    "new_pid": new_pid, "delete_http": 204, "external_http": 409,
                    "external_result": "failed", "mcp_calls": len(remote.calls), "task": "cancelled"}
        finally:
            remote.release.set()
            for pending in (confirmation, deletion):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(p for p in (confirmation, deletion) if p is not None), return_exceptions=True)


def diagnostics(directory):
    """只汇总白名单数值；绝不保存 HTTP 路径或应用日志原文。"""
    if not directory.is_dir():
        return {
            "http_5xx": {"count": None, "by_status": {}, "invalid_records": None},
            "sqlite_busy_log_hits": None,
        }
    statuses = {}
    malformed = 0
    try:
        error_file = directory / "http-errors.jsonl"
        for line in error_file.read_text(encoding="utf-8").splitlines() if error_file.exists() else ():
            try:
                record = json.loads(line)
                status = record.get("status") if isinstance(record, dict) else None
                if type(status) is not int or not 500 <= status <= 599:
                    malformed += 1
                    continue
                key = str(status)
                statuses[key] = statuses.get(key, 0) + 1
            except (ValueError, TypeError):
                malformed += 1
        http = {"count": sum(statuses.values()), "by_status": statuses, "invalid_records": malformed}
    except (OSError, UnicodeError):
        http = {"count": None, "by_status": {}, "invalid_records": None}

    try:
        logs = "\n".join(path.read_text(errors="replace") for path in directory.glob("*.log"))
        busy = len(re.findall(r"database (?:table )?is (?:locked|busy)|sqlite_(?:busy|locked)",
                              logs, flags=re.IGNORECASE))
    except OSError:
        busy = None
    return {"http_5xx": http, "sqlite_busy_log_hits": busy}


async def process_snapshot(world):
    """在关闭隔离应用前记录由本次验收启动的实例和已登记的 worker。"""
    processes = []
    for item in world.processes:
        role = item.name if item.name in {"A", "B", "multi", "external-new", "deletion-new"} else "instance"
        processes.append({"role": role, "pid": item.process.pid, "returncode": item.process.returncode})
    workers = None
    try:
        async with asyncio.timeout(5):
            workers = []
            for pid in await world.pids():
                if await world.owns_pid(pid):
                    workers.append(pid)
            workers.sort()
    except Exception:
        workers = None
    return {"runner_pid": os.getpid(), "applications": processes, "registered_worker_pids": workers}


def exception_category(error):
    httpx_module = globals().get("httpx")
    if httpx_module is not None and isinstance(error, httpx_module.HTTPError):
        return "HTTPError"
    categories = (
        (AssertionError, "AssertionError"),
        (TimeoutError, "TimeoutError"),
        (asyncio.CancelledError, "CancelledError"),
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (OSError, "OSError"),
        (RuntimeError, "RuntimeError"),
        (ValueError, "ValueError"),
        (TypeError, "TypeError"),
        (KeyError, "KeyError"),
        (ImportError, "ImportError"),
    )
    return next((name for kind, name in categories if isinstance(error, kind)), "UnexpectedError")


def exception_locations(error):
    """只允许固定的测试脚本文件名和行号，不输出异常、代码或局部变量。"""
    allowed = {
        str(SCRIPT): "run_inflight.py",
        str(COMMON_SCRIPT): "../c7_task_acceptance/process_tasks.py",
        str(SCRIPT.parent / "verify_failure_reporting.py"): "verify_failure_reporting.py",
    }
    if ROOT is not None:
        allowed[str(ROOT / "backend/tests/helpers.py")] = "backend/tests/helpers.py"
    locations = []
    trace = error.__traceback__
    while trace:
        name = allowed.get(trace.tb_frame.f_code.co_filename)
        if name:
            locations.append({"file": name, "line": trace.tb_lineno})
        trace = trace.tb_next
    return locations


def save_failure(topology, case, phase, started_at, snapshot, directory, error):
    details = diagnostics(directory) if directory is not None else {
        "http_5xx": {"count": None, "by_status": {}, "invalid_records": None},
        "sqlite_busy_log_hits": None,
    }
    report = {
        "version": 1,
        "topology": topology,
        "case": case,
        "phase": phase,
        "started_utc": started_at,
        "failed_utc": datetime.now(UTC).isoformat(),
        "processes": snapshot or {"runner_pid": os.getpid(), "applications": None, "registered_worker_pids": None},
        "error": {"category": exception_category(error), "locations": exception_locations(error)},
        **details,
    }
    report_directory = SCRIPT.parent / "failure_reports"
    report_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(prefix=f"{topology}-{case}-", suffix=".json", dir=report_directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return Path(path)


def record_failure(topology, case, phase, started_at, snapshot, directory, error):
    try:
        path = save_failure(topology, case, phase, started_at, snapshot, directory, error)
        notice = {"result": "failed", "report": str(path)}
    except BaseException:
        # 不打印写入异常：其中也可能含有敏感路径或内容。
        notice = {"result": "failed", "report": None, "report_error": "write_failed"}
    print(json.dumps(notice, ensure_ascii=False), flush=True)


async def main(topology, case):
    for name, scenario in (("external", external_scenario), ("deletion", deletion_scenario)):
        if case != "all" and name != case:
            continue
        started_at = datetime.now(UTC).isoformat()
        if BOOTSTRAP_ERROR is not None:
            record_failure(topology, name, "dependencies", started_at, None, None, BOOTSTRAP_ERROR)
            return 1
        snapshot, directory, phase = None, None, "temporary_setup"
        try:
            with TemporaryDirectory(prefix="ainovel-c7-inflight-") as temp:
                directory = Path(temp)
                try:
                    phase = "application_setup"
                    async with world_context(directory, topology) as world:
                        phase = "scenario"
                        try:
                            result = await scenario(world)
                        finally:
                            snapshot = await process_snapshot(world)
                        phase = "application_teardown"
                    phase = "diagnostics"
                    details = await asyncio.to_thread(diagnostics, directory)
                    http = details["http_5xx"]
                    assert http["count"] == 0 and http["invalid_records"] == 0
                    assert details["sqlite_busy_log_hits"] is not None
                    result["http_5xx"] = []
                    result["sqlite_busy_log_hits"] = details["sqlite_busy_log_hits"]
                except BaseException as error:
                    record_failure(topology, name, phase, started_at, snapshot, directory, error)
                    return 1
                phase = "temporary_cleanup"
        except BaseException as error:
            record_failure(topology, name, phase, started_at, snapshot, directory, error)
            return 1
        print(json.dumps({"topology": topology, **result}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--topology", required=True, choices=("sqlite", "pg-workers", "pg-instances"))
    parser.add_argument("--case", choices=("external", "deletion", "all"), default="all")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.topology, args.case)))
