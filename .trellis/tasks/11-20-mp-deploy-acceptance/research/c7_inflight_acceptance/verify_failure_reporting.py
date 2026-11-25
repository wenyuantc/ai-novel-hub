"""真实 SQLite 应用进程中的受控失败，以及启动阶段失败的脱敏留痕验证。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import runpy
import stat
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
SCRIPT = DIRECTORY / "run_inflight.py"
MARKERS = (
    "C7_FAKE_SECRET_ONLY_58ed35d8",
    "C7_FAKE_REQUEST_BODY_ONLY_91d0d66c",
    "C7_FAKE_MODEL_CONTENT_ONLY_ad230914",
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def inject_diagnostics(directory):
    """模拟含敏感字段的 503、损坏记录、忙锁日志和异常，不触碰产品应用。"""
    secret, body, model = MARKERS
    record = {"status": 503, "pid": os.getpid(), "path": f"/{secret}", "body": body, "model": model}
    with (directory / "http-errors.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(record) + "\n")
        output.write(f"not-json {secret} {body} {model}\n")
    (directory / "injected-diagnostic.log").write_text(
        f"SQLITE_BUSY {secret} {body} {model}\n", encoding="utf-8",
    )


def run_child(mode):
    spec = importlib.util.spec_from_file_location("c7_inflight_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)

    def load_fault(*_args, **_kwargs):
        raise RuntimeError(" ".join(MARKERS))

    original_load = runpy.run_path
    try:
        if mode == "dependencies":
            runpy.run_path = load_fault
        spec.loader.exec_module(module)
    finally:
        runpy.run_path = original_load
    original = module.external_scenario
    observed = {}

    async def scenario_fault(world):
        observed["world"] = world
        observed["directory"] = world.directory
        observed["socket_directory"] = Path(world.env["TASK_PEER_SOCKET_DIR"])
        await original(world)
        inject_diagnostics(world.directory)
        raise AssertionError(" ".join(MARKERS))

    async def diagnostics_fault(world):
        observed["world"] = world
        observed["directory"] = world.directory
        observed["socket_directory"] = Path(world.env["TASK_PEER_SOCKET_DIR"])
        inject_diagnostics(world.directory)
        return {"case": "external"}

    @asynccontextmanager
    async def setup_fault(directory, _topology):
        observed["directory"] = directory
        inject_diagnostics(directory)
        raise RuntimeError(" ".join(MARKERS))
        yield  # pragma: no cover

    if mode == "scenario":
        module.external_scenario = scenario_fault
    elif mode == "diagnostics":
        module.external_scenario = diagnostics_fault
    elif mode == "setup":
        module.world_context = setup_fault
    status = asyncio.run(module.main("sqlite", "external"))
    if mode == "dependencies":
        return status
    if observed["directory"].exists():
        print("temporary_directory_cleanup_failed")
        return 2
    if mode in {"scenario", "diagnostics"}:
        if observed["socket_directory"].exists():
            print("peer_socket_directory_cleanup_failed")
            return 2
        if any(process.process.returncode is None for process in observed["world"].processes):
            print("application_process_cleanup_failed")
            return 2
    return status


def verify(mode):
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", mode],
        capture_output=True, text=True, check=False, timeout=180,
    )
    require(process.returncode == 1, f"{mode}: expected controlled failure exit 1")
    for marker in MARKERS:
        require(marker not in process.stdout and marker not in process.stderr, f"{mode}: output leaked a marker")
    notices = []
    for line in process.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("result") == "failed":
            notices.append(value)
    require(len(notices) == 1 and notices[0].get("report"), f"{mode}: missing retained report")
    path = Path(notices[0]["report"])
    require(path.parent == DIRECTORY / "failure_reports" and path.is_file(), f"{mode}: report not retained")
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, f"{mode}: report permissions are not 0600")
    raw = path.read_text(encoding="utf-8")
    for marker in MARKERS:
        require(marker not in raw, f"{mode}: report leaked a marker")
    report = json.loads(raw)
    require(set(report) == {
        "version", "topology", "case", "phase", "started_utc", "failed_utc", "processes", "error",
        "http_5xx", "sqlite_busy_log_hits",
    }, f"{mode}: unexpected report field")
    require(report["topology"] == "sqlite" and report["case"] == "external", f"{mode}: wrong scenario")
    phases = {
        "scenario": "scenario", "diagnostics": "diagnostics", "setup": "application_setup",
        "dependencies": "dependencies",
    }
    require(report["phase"] == phases[mode], f"{mode}: wrong phase")
    category = "AssertionError" if mode in {"scenario", "diagnostics"} else "RuntimeError"
    require(report["error"]["category"] == category, f"{mode}: wrong exception category")
    require(any(item["file"] == "run_inflight.py" and item["line"] > 0 for item in report["error"]["locations"]),
            f"{mode}: missing source location")
    if mode != "diagnostics":
        require(any(item["file"] == "verify_failure_reporting.py" and item["line"] > 0
                    for item in report["error"]["locations"]), f"{mode}: missing injected failure location")
    http = {"count": None, "by_status": {}, "invalid_records": None} if mode == "dependencies" else {
        "count": 1, "by_status": {"503": 1}, "invalid_records": 1,
    }
    require(report["http_5xx"] == http, f"{mode}: wrong HTTP counts")
    require(report["sqlite_busy_log_hits"] == (None if mode == "dependencies" else 1), f"{mode}: wrong busy count")
    require(report["processes"]["runner_pid"] > 0, f"{mode}: missing process")
    if mode in {"scenario", "diagnostics"}:
        process_count = 3 if mode == "scenario" else 2
        require(len(report["processes"]["applications"]) == process_count, f"{mode}: missing app processes")
        require(len(report["processes"]["registered_worker_pids"]) >= process_count, f"{mode}: missing worker PIDs")
    print(json.dumps({"case": mode, "result": "passed", "report": str(path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    modes = {"scenario", "diagnostics", "setup", "dependencies"}
    if len(sys.argv) == 3 and sys.argv[1] == "--child" and sys.argv[2] in modes:
        raise SystemExit(run_child(sys.argv[2]))
    for test_mode in ("scenario", "diagnostics", "setup", "dependencies"):
        verify(test_mode)
