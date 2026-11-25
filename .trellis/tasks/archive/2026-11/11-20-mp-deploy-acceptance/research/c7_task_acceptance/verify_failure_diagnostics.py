"""在真实 SQLite 多进程环境验证失败诊断脱敏、持久化及成功清理。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import runpy
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
SCRIPTS = runpy.run_path(str(HERE / "process_tasks.py"))
run = SCRIPTS["main"]
BODY_MARKER = "C7_PRIVATE_REQUEST_BODY_TEST_MARKER"
MODEL_MARKER = "C7_PRIVATE_MODEL_CONTENT_TEST_MARKER"


def inject_sensitive_logs(world, sensitive_values):
    # 模拟包含敏感内容的应用日志和 5xx 采集行；持久报告只能保留白名单字段。
    with (world.directory / "probe.log").open("w", encoding="utf-8") as output:
        output.write("database is locked " + " ".join(sensitive_values) + "\n")
    with (world.directory / "http-errors.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({
            "pid": world.processes[0].process.pid,
            "status": 503,
            "time": time.time(),
            "path": "/" + BODY_MARKER,
            "body": MODEL_MARKER,
            "authorization": sensitive_values[0],
        }) + "\n")
        output.write("{invalid " + BODY_MARKER + "\n")
    with (world.directory / "process-events.jsonl").open("a", encoding="utf-8") as output:
        output.write("{invalid " + MODEL_MARKER + "\n")


def capture_resources(world):
    return {
        "directory": world.directory,
        "peer_directory": Path(world.env["TASK_PEER_SOCKET_DIR"]),
        "processes": [process.process for process in world.processes],
    }


def assert_clean(resources):
    assert not resources["directory"].exists()
    assert not resources["peer_directory"].exists()
    assert all(process.returncode is not None for process in resources["processes"])


def one_new_report(destination, previous):
    created = set(destination.glob("c7-failure-*.json")) - previous
    assert len(created) == 1
    path = created.pop()
    text = path.read_text(encoding="utf-8")
    assert os.stat(path).st_mode & 0o777 == 0o600
    return path, text, json.loads(text)


async def verify(destination):
    previous = set(destination.glob("c7-failure-*.json")) if destination.exists() else set()
    sensitive_values = []
    captured = {}

    async def fail_with_private_data(world, case):
        assert case == "forward"
        captured.update(capture_resources(world))
        async with httpx.AsyncClient(base_url=world.entry_url(), timeout=30, trust_env=False) as client:
            response = await client.post(
                "/api/v1/auth/register", json={"username": "c7-diagnostic-user", "password": "c7-test-password"}
            )
            assert response.status_code == 201
            jwt = next(iter(client.cookies.values()))
        sensitive_values.extend([
            world.env["JWT_SECRET"],
            world.env["ENCRYPTION_KEY"],
            world.env["ADMIN_PASSWORD"],
            world.env["C7_CONTROL_TOKEN"],
            jwt,
            "c7-test-password",
            BODY_MARKER,
            MODEL_MARKER,
        ])
        await asyncio.to_thread(inject_sensitive_logs, world, sensitive_values)
        raise AssertionError(" ".join(sensitive_values))

    try:
        await run("sqlite", ["forward"], diagnostics_dir=destination, _case_hook=fail_with_private_data)
    except AssertionError:
        pass
    else:
        raise AssertionError("受控故障未发生")

    path, text, report = one_new_report(destination, previous)
    assert all(value and value not in text for value in sensitive_values)
    assert report["topology"] == "sqlite" and report["scenario"] == "forward" and report["phase"] == "scenario"
    assert report["error_category"] == "AssertionError" and report["runner_pid"] == os.getpid()
    assert report["started_at_utc"] and report["failed_at_utc"] and report["traceback"]
    assert len(report["observed_worker_pids"]) >= 2
    assert len(report["diagnostics"]["http_5xx"]) == 1
    assert report["diagnostics"]["invalid_http_records"] == 1
    assert report["diagnostics"]["invalid_process_records"] == 1
    assert report["diagnostics"]["log_counts"]["sqlite_busy"] == 1
    assert {event["kind"] for event in report["diagnostics"]["process_events"]} >= {
        "migration_start", "migration_stop", "uvicorn_start", "uvicorn_stop"
    }
    assert_clean(captured)

    previous.add(path)
    original_service = run.__globals__["service"]
    startup_resources = {}
    startup_secrets = []

    @asynccontextmanager
    async def fail_second_start(directory, env, name, workers=1):
        if name == "B":
            startup_secrets.extend([env["JWT_SECRET"], env["ADMIN_PASSWORD"], BODY_MARKER, MODEL_MARKER])
            raise RuntimeError(" ".join(startup_secrets))
        async with original_service(directory, env, name, workers=workers) as process:
            startup_resources.update({
                "directory": directory,
                "peer_directory": Path(env["TASK_PEER_SOCKET_DIR"]),
                "processes": [process.process],
            })
            yield process

    try:
        run.__globals__["service"] = fail_second_start
        try:
            await run("sqlite", ["forward"], diagnostics_dir=destination)
        except RuntimeError:
            pass
        else:
            raise AssertionError("启动受控故障未发生")
    finally:
        run.__globals__["service"] = original_service

    startup_path, startup_text, startup_report = one_new_report(destination, previous)
    assert all(value and value not in startup_text for value in startup_secrets)
    assert startup_report["phase"] == "setup" and startup_report["scenario"] is None
    assert startup_report["error_category"] == "RuntimeError"
    assert startup_resources["processes"][0].pid in startup_report["diagnostics"]["log_pids"]
    assert {event["kind"] for event in startup_report["diagnostics"]["process_events"]} >= {
        "migration_start", "migration_stop", "uvicorn_start", "uvicorn_stop"
    }
    assert_clean(startup_resources)

    previous.add(startup_path)
    original_temporary_directory = run.__globals__["TemporaryDirectory"]

    def fail_temporary_directory(*args, **kwargs):
        raise RuntimeError(BODY_MARKER + MODEL_MARKER)

    try:
        run.__globals__["TemporaryDirectory"] = fail_temporary_directory
        try:
            await run("sqlite", ["forward"], diagnostics_dir=destination)
        except RuntimeError:
            pass
        else:
            raise AssertionError("临时目录受控故障未发生")
    finally:
        run.__globals__["TemporaryDirectory"] = original_temporary_directory

    temporary_path, temporary_text, temporary_report = one_new_report(destination, previous)
    assert BODY_MARKER not in temporary_text and MODEL_MARKER not in temporary_text
    assert temporary_report["phase"] == "setup" and temporary_report["scenario"] is None
    assert temporary_report["error_category"] == "RuntimeError"
    assert temporary_report["diagnostics"]["collection_error"] == "TemporaryDirectoryNotAvailable"

    previous.add(temporary_path)
    teardown_resources = {}
    teardown_secrets = []

    async def fail_during_teardown(world, case):
        assert case == "forward"
        teardown_resources.update(capture_resources(world))
        teardown_secrets.extend([world.env["JWT_SECRET"], world.env["ADMIN_PASSWORD"], BODY_MARKER, MODEL_MARKER])

        async def fail_cleanup():
            raise AssertionError(" ".join(teardown_secrets))

        world.stack.push_async_callback(fail_cleanup)

    try:
        await run("sqlite", ["forward"], diagnostics_dir=destination, _case_hook=fail_during_teardown)
    except AssertionError:
        pass
    else:
        raise AssertionError("清理受控故障未发生")
    teardown_path, teardown_text, teardown_report = one_new_report(destination, previous)
    assert all(value and value not in teardown_text for value in teardown_secrets)
    assert teardown_report["phase"] == "teardown" and teardown_report["scenario"] == "forward"
    assert teardown_report["completed_scenarios"] == ["forward"]
    assert teardown_report["error_category"] == "AssertionError"
    assert len(teardown_report["observed_worker_pids"]) >= 2
    assert_clean(teardown_resources)

    previous.add(teardown_path)
    success_resources = {}

    async def observe_success(world, case):
        assert case == "forward"
        success_resources.update(capture_resources(world))

    await run("sqlite", ["forward"], diagnostics_dir=destination, _case_hook=observe_success)
    assert set(destination.glob("c7-failure-*.json")) == previous
    assert_clean(success_resources)
    print(json.dumps({
        "controlled_failure_reports": {
            "setup": str(startup_path), "scenario": str(path), "teardown": str(teardown_path),
            "temporary_directory": str(temporary_path),
        },
        "secret_and_content_absent": True,
        "failed_and_success_resources_cleaned": True,
        "successful_run_created_no_report": True,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostics-dir", type=Path, default=HERE / "diagnostics")
    args = parser.parse_args()
    asyncio.run(verify(args.diagnostics_dir))
