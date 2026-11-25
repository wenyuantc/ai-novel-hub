"""C7 PostgreSQL 实进程取消与删除；仅管理一次性容器和临时数据库。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import runpy
import signal
import tempfile
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from tests.helpers import task_params

COMMON = runpy.run_path(str(Path(__file__).resolve().parents[1] / "c7_task_acceptance/process_tasks.py"))
world = COMMON["world"]
create_book = COMMON["create_book"]
checked = COMMON["checked"]
wait_until = COMMON["wait_until"]
_FAKE_SECRET = "C7_DIAGNOSTIC_FAKE_KEY_78ac4d"
_FAKE_BODY = "C7_DIAGNOSTIC_FAKE_STORY_62e9"


@dataclass
class Progress:
    topology: str
    case: str = "startup"
    executor_pid: int | None = None
    request_pid: int | None = None
    application_pids: list[int] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)


def http_failure_counts(directory):
    """只读取 5xx 数量，不保留路径、响应或其他日志内容。"""
    path = Path(directory) / "http-errors.jsonl"
    count = invalid = 0
    if not path.exists():
        return count, invalid
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                status = record.get("status") if isinstance(record, dict) else None
                if type(status) is not int or not 500 <= status <= 599:
                    invalid += 1
                else:
                    count += 1
            except (json.JSONDecodeError, TypeError):
                invalid += 1
    except (OSError, UnicodeError):
        return None, None
    return count, invalid


def error_category(error):
    """使用固定类别，避免把异常文本或用户自定义类型名带入报告。"""
    for cls, name in (
        (AssertionError, "AssertionError"), (TimeoutError, "TimeoutError"), (httpx.HTTPError, "HTTPError"),
        (OSError, "OSError"), (RuntimeError, "RuntimeError"), (asyncio.CancelledError, "CancelledError"),
    ):
        if isinstance(error, cls):
            return name
    return "OtherError"


def error_location(error):
    """仅记录受信验收代码的文件名与行号，不记录代码行或原始堆栈。"""
    trusted = {
        Path(__file__).resolve(): "run_pg_cancellation.py",
        (Path(__file__).resolve().parents[1] / "c7_task_acceptance/process_tasks.py").resolve(): "process_tasks.py",
    }
    result = {"file": "run_pg_cancellation.py", "line": 0}
    tb = error.__traceback__
    while tb is not None:
        name = trusted.get(Path(tb.tb_frame.f_code.co_filename).resolve())
        if name:
            result = {"file": name, "line": int(tb.tb_lineno)}
        tb = tb.tb_next
    return result


def save_failure_report(error, progress, directory, diagnostic_dir):
    """临时运行目录外创建私有报告；永不复制原始异常、请求或日志。"""
    five_xx, invalid_lines = http_failure_counts(directory)
    parent = Path(diagnostic_dir).expanduser().resolve(strict=True)
    report_dir = Path(tempfile.mkdtemp(prefix="ainovel-c7-failure-", dir=parent))
    report_path = report_dir / "failure.json"
    report = {
        "schema": "c7-scheduling-failure-v1",
        "topology": progress.topology,
        "case": progress.case,
        "time_utc": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.monotonic() - progress.started, 3),
        "error_category": error_category(error),
        "location": error_location(error),
        "processes": {
            "runner_pid": os.getpid(), "application_pids": progress.application_pids,
            "executor_pid": progress.executor_pid, "request_pid": progress.request_pid,
        },
        "http_5xx_count": five_xx,
        "invalid_http_diagnostic_lines": invalid_lines,
    }
    descriptor = os.open(report_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    return report_path


async def assert_owned_worker(env, pid):
    """只允许暂停本脚手架创建的 Uvicorn 进程。"""
    if env.topology == "pg-instances":
        assert any(p.process.pid == pid and p.process.returncode is None for p in env.processes), pid
        return
    supervisor = env.processes[0].process
    assert supervisor.returncode is None
    probe = await asyncio.create_subprocess_exec(
        "ps", "-o", "ppid=", "-p", str(pid), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output, _ = await probe.communicate()
    assert probe.returncode == 0 and output.strip().isdigit(), (pid, probe.returncode)
    assert int(output.strip()) == supervisor.pid, (pid, output.decode().strip(), supervisor.pid)


async def pinned_client(env, pid, cookies, stack):
    """暂停前锁定远端 worker 的 keep-alive 连接，避免新连接落到已暂停的 worker。"""
    base_url = await env.target_url(pid)
    async with asyncio.timeout(30):
        while True:
            client = httpx.AsyncClient(
                base_url=base_url, cookies=cookies, timeout=15, trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=1, max_connections=1),
            )
            try:
                response = await client.get("/healthz", headers={"X-C7-Target-Pid": str(pid)}, timeout=2)
                if not response.headers.get("x-c7-target-mismatch"):
                    assert response.status_code == 200 and int(response.headers["x-c7-pid"]) == pid
                    stack.push_async_callback(client.aclose)
                    return client
            except (httpx.TransportError, TimeoutError):
                pass
            await client.aclose()
            await asyncio.sleep(0.02)


async def begin_case(env, book):
    """先从隔离库确认存活 PID，随后由 pinned_client 建立真实连接。"""
    model = env.model.expect()
    task = checked(
        await book.actor.post(f"/api/v1/chapters/{book.chapter_id}/generate", json={"auto_continue": False}), 202,
    )
    await asyncio.wait_for(model.entered.wait(), 40)
    owner = await env.owner({"id": task["id"], "key": task["task_key"]})

    async def candidate():
        for pid in sorted((await env.pids()) - {owner}):
            try:
                await assert_owned_worker(env, pid)
            except (AssertionError, OSError):
                continue
            return pid
        return None

    remote = await wait_until(candidate, seconds=30)
    return task, model, owner, remote


async def facts(env, book, task):
    rows = await env.rows(
        """SELECT t.status, c.status, c.generation_claim_key IS NOT NULL,
                  p.deleted_at IS NOT NULL, p.deletion_token IS NOT NULL,
                  t.cancel_requested_at IS NOT NULL, t.admin_cancel_operation_id,
                  (SELECT count(*) FROM task_work_records r
                   WHERE r.task_key=t.statistics_key AND r.kind='execution'),
                  (SELECT count(*) FROM task_execution_slots s WHERE s.statistics_key=t.statistics_key)
           FROM tasks t JOIN chapters c ON c.id=:chapter JOIN projects p ON p.id=:project
           WHERE t.id=:task AND t.statistics_key=:key""",
        {"chapter": book.chapter_id, "project": book.project_id, "task": task["id"], "key": task["task_key"]},
    )
    assert len(rows) == 1, rows
    status, chapter_status, claimed, deleted, deleting, cancel_requested, operation_id, works, slots = rows[0]
    return {
        "task": status,
        "chapter": chapter_status,
        "claimed": claimed,
        "deleted": deleted,
        "deleting": deleting,
        "cancel_requested": cancel_requested,
        "operation_id": operation_id,
        "execution_work": works,
        "slots": slots,
    }


async def run_case(env, case, progress, *, simulate_failure=False):
    async with AsyncExitStack() as stack:
        book = await create_book(env, stack, f"pg-{case}")
        task, model, owner, remote = await begin_case(env, book)
        stack.callback(model.finish)
        progress.executor_pid, progress.request_pid = owner, remote
        assert owner != remote and owner in await env.pids() and remote in await env.pids()
        model.send("C7 持久化后暂停执行者。")

        async def persisted():
            rows = await env.rows("SELECT output_text FROM tasks WHERE id=:id", {"id": task["id"]})
            return rows and "暂停执行者" in (rows[0][0] or "")

        await wait_until(persisted, seconds=20)
        before = await facts(env, book, task)
        assert before["task"] == "running" and before["claimed"] and before["slots"] == 1, before

        admin = None
        if case == "admin":
            admin = await stack.enter_async_context(
                httpx.AsyncClient(base_url=env.processes[0].url, timeout=30, trust_env=False,
                                  limits=httpx.Limits(max_keepalive_connections=0))
            )
            checked(await admin.post("/api/v1/auth/login", json={
                "username": env.env["ADMIN_USERNAME"], "password": env.env["ADMIN_PASSWORD"],
            }), 200)

        remote_client = await pinned_client(env, remote, (admin or book.owner).cookies, stack)

        async def pinned_request(method, path, **kwargs):
            response = await remote_client.request(method, path, headers={"X-C7-Target-Pid": str(remote)}, **kwargs)
            assert int(response.headers["x-c7-pid"]) == remote
            return response

        async def request():
            if case == "owner":
                response = await pinned_request("POST", f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task))
                return [response]
            if case == "delete":
                response = await pinned_request("DELETE", f"/api/v1/projects/{book.project_id}")
                return [response]
            assert admin is not None
            path = f"/api/v1/admin/tasks/{task['task_key']}/cancel"
            body = {"confirm": True, "reason": "C7 真实进程验收"}
            return [await pinned_request("POST", path, json=body) for _ in range(2)]

        await assert_owned_worker(env, owner)
        started = time.monotonic()
        os.kill(owner, signal.SIGSTOP)
        request_task = asyncio.create_task(request())
        failed = True
        try:
            if simulate_failure:
                # 在暂停执行者及未完成的请求期间注入错误，验证清理和报告不复制虚构内容。
                (env.directory / "fake-app.log").write_text(f"{_FAKE_SECRET} {_FAKE_BODY}\n", encoding="utf-8")
                with (env.directory / "http-errors.jsonl").open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"status": 503, "path": f"/{_FAKE_SECRET}", "body": _FAKE_BODY}) + "\n")
                raise RuntimeError(f"{_FAKE_SECRET} {_FAKE_BODY}")

            async def observed():
                row = await facts(env, book, task)
                if case == "admin":
                    return row if row["operation_id"] is not None and request_task.done() else None
                if case == "owner":
                    return row if row["cancel_requested"] else None
                return row if row["deleting"] else None

            frozen = await wait_until(observed, seconds=4)
            assert frozen["task"] == "running" and frozen["claimed"] and frozen["slots"] == 1, frozen
            assert not frozen["deleted"] and not model.closed.is_set(), frozen
            if case != "admin":
                assert not request_task.done(), (case, frozen)
            failed = False
        finally:
            os.kill(owner, signal.SIGCONT)
            if failed:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)

        responses = await asyncio.wait_for(request_task, timeout=30)
        codes = [response.status_code for response in responses]
        if case == "owner":
            assert codes == [200] and responses[0].json()["status"] == "cancelled", codes
        elif case == "delete":
            assert codes == [204] and model.closed.is_set(), codes
        else:
            assert codes == [202, 202], codes
            operations = [response.json()["operation_id"] for response in responses]
            assert operations[0] and operations[0] == operations[1], operations

        await asyncio.wait_for(model.closed.wait(), 20)

        async def completed():
            row = await facts(env, book, task)
            if (row["task"] == "cancelled" and not row["claimed"] and row["slots"] == 0
                    and not row["operation_id"] and row["execution_work"] == 0):
                return row
            return None

        final = await wait_until(completed, seconds=30)
        assert final["chapter"] == "draft" and final["execution_work"] == 0, final
        if case == "delete":
            assert final["deleted"] and not final["deleting"], final
        logs = await env.rows("SELECT status FROM generation_logs WHERE task_id=:id ORDER BY id", {"id": task["id"]})
        assert logs == [("cancelled",)], logs
        audits = await env.rows(
            "SELECT count(*) FROM admin_audit_logs "
            "WHERE action='task.cancel_request' AND target_id=:id AND result='success'", {"id": task["id"]},
        )
        if case == "admin":
            assert audits == [(1,)], audits
        # 同一轮次只收到一次模型调用，且收尾事实重复采样保持一致。
        assert env.model.calls.count(model) == 1
        await asyncio.sleep(0.25)
        assert await facts(env, book, task) == final
        return {
            "case": case,
            "executor_pid": owner,
            "request_pid": remote,
            "frozen": {key: value for key, value in frozen.items() if key != "operation_id"},
            "http_codes": codes,
            "final": {key: value for key, value in final.items() if key != "operation_id"},
            "model_calls": env.model.calls.count(model),
            "generation_statuses": [row[0] for row in logs],
            "admin_success_audits": audits[0][0],
            "elapsed_s": round(time.monotonic() - started, 3),
        }


async def main(topology, cases, diagnostic_dir, *, simulate_failure=False):
    progress = Progress(topology)
    with TemporaryDirectory(prefix="ainovel-c7-pg-cancellation-") as directory:
        try:
            async with world(Path(directory), topology) as env:
                progress.application_pids = [p.process.pid for p in env.processes]
                for case in cases:
                    progress.case = case
                    progress.executor_pid = progress.request_pid = None
                    result = await run_case(env, case, progress, simulate_failure=simulate_failure)
                    print(json.dumps({"topology": topology, **result}, ensure_ascii=False), flush=True)
                assert env.model.unexpected == 0
                progress.case = "http_diagnostics"
                count, invalid = http_failure_counts(directory)
                if count is None or invalid is None or invalid or count:
                    raise RuntimeError("验收请求出现 5xx 或诊断计数不完整")
                print(json.dumps({"topology": topology, "http_5xx_count": count}), flush=True)
        except BaseException as error:
            try:
                report = save_failure_report(error, progress, directory, diagnostic_dir)
                print(json.dumps({"failure_report": str(report)}, ensure_ascii=False), flush=True)
            except Exception:
                print("验收失败且无法保存安全诊断报告", flush=True)
            return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", required=True, choices=["pg-workers", "pg-instances"])
    parser.add_argument(
        "--cases", nargs="+", choices=["owner", "admin", "delete"], default=["owner", "admin", "delete"],
    )
    parser.add_argument("--diagnostic-dir", default=tempfile.gettempdir(), help="失败报告目录，须已存在")
    parser.add_argument("--simulate-failure", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    raise SystemExit(not asyncio.run(main(args.topology, args.cases, args.diagnostic_dir,
                                        simulate_failure=args.simulate_failure)))
