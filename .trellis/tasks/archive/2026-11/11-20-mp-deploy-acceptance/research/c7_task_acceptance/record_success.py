"""将真实进程验收的非敏感成功事实保存为 JSON。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SCRIPTS = runpy.run_path(str(HERE / "process_tasks.py"))
run = SCRIPTS["main"]
TOPOLOGIES = SCRIPTS["TOPOLOGIES"]
CHECKS = {
    "restart": ["running_identity_unchanged", "other_process_started", "original_task_succeeded"],
    "forward": ["direct_and_remote_events_equal", "task_succeeded"],
    "revoke": ["both_streams_permission_denied", "post_revocation_content_not_sent"],
    "downgrade": ["both_streams_permission_denied", "post_downgrade_content_not_sent"],
    "agent-structured": ["direct_and_remote_events_equal", "tool_start_and_proposal", "proposed_and_paused"],
    "author-off": ["remote_snapshot_while_running", "remote_done_succeeded"],
    "collab-off": ["remote_permission_denied", "later_content_not_sent", "reconnect_done_succeeded"],
    "kill": ["interrupted", "chapter_claim_released", "terminal_facts_stable", "retry_succeeded"],
    "kill-serial": ["result_requires_review", "immediate_retry_rejected", "serial_successor_succeeded"],
    "scheduling": ["global_limit", "per_user_limit", "serial_fifo", "two_real_executors", "bounded_barriers"],
    "cancel": ["remote_cancel_accepted", "model_connection_closed", "cancelled", "chapter_claim_released"],
    "admin-cancel": ["remote_admin_cancel_accepted", "model_connection_closed", "cancelled", "chapter_claim_released"],
    "delete": ["remote_delete_accepted", "model_connection_closed_before_delete_returned"],
}
EVENTS = {"snapshot", "delta", "progress", "done", "error", "tool_start", "proposal", "paused"}
STATUS = {"queued", "running", "interrupted", "succeeded", "结果待核对"}
NUMERIC_FIELDS = {"running_samples", "recovery_s", "original_retry", "elapsed_s"}
EVENT_FIELDS = {"events", "remote_events", "replay_events"}
STATUS_FIELDS = {"status", "retry_status", "retry_final", "original_status"}
PID_FIELDS = {"executor_pid", "other_pid", "new_pid", "replacement_pid"}


def public_port(process):
    port = urlsplit(process.url).port
    assert type(port) is int and 0 < port < 65536
    return port


def pid_endpoint(world, pid):
    assert type(pid) is int and pid > 0
    if world.topology == "pg-workers":
        return {"pid": pid, "public_port": public_port(world.processes[0])}
    process = next(process for process in world.processes if process.process.pid == pid)
    return {"pid": pid, "public_port": public_port(process)}


def safe_schedule(world, result):
    requests = result["request_pids"]
    allowed = {"X1", "X2", "X3", "Y1", "Y2", "A1", "A2", "A3", "B1"}
    assert set(requests) == allowed

    def observations(rows):
        items = []
        for tag, task_id, pid in rows:
            assert tag in allowed and type(task_id) is int and task_id > 0
            items.append({
                "tag": tag,
                "task_id": task_id,
                "executor": pid_endpoint(world, pid),
                "request": pid_endpoint(world, requests[tag]),
            })
        return items

    global_run, user_run = result["global"], result["per_user"]
    assert global_run["peak"] == 2 and user_run["peak"] == 3 and user_run["peak_by_user"] == {"A": 2, "B": 1}
    barriers = result["barrier_seconds"]
    assert len(barriers) == 2 and all(type(value) in {int, float} and 0 <= value < 5 for value in barriers)
    first_wave = observations(global_run["first_wave"])
    assert [item["tag"] for item in first_wave] == ["X1", "Y1"]
    assert first_wave[0]["executor"]["pid"] != first_wave[1]["executor"]["pid"]
    facts = {
        "barrier_seconds": barriers,
        "global": {"peak": global_run["peak"], "starts": observations(global_run["starts"])},
        "per_user": {
            "peak": user_run["peak"],
            "peak_by_user": user_run["peak_by_user"],
            "starts": observations(user_run["starts"]),
        },
    }
    endpoints = {"executor_pid": first_wave[0]["executor"], "other_pid": first_wave[1]["executor"]}
    return facts, endpoints


def safe_case(world, result):
    case = result["case"]
    assert case in CHECKS
    facts, endpoints = safe_schedule(world, result) if case == "scheduling" else ({}, {})
    for key in NUMERIC_FIELDS & result.keys():
        assert type(result[key]) in {int, float}
        facts[key] = result[key]
    for key in STATUS_FIELDS & result.keys():
        assert result[key] in STATUS
        facts[key] = result[key]
    for key in EVENT_FIELDS & result.keys():
        assert isinstance(result[key], list) and set(result[key]) <= EVENTS
        facts[key] = result[key]
    if "serial_order" in result:
        assert result["serial_order"] == ["K1", "K2"]
        facts["serial_order"] = result["serial_order"]
    endpoints.update({key: pid_endpoint(world, result[key]) for key in PID_FIELDS & result.keys()})
    assert {"executor_pid", "other_pid"} <= endpoints.keys()
    assert endpoints["executor_pid"]["pid"] != endpoints["other_pid"]["pid"]
    services = [
        {
            "process_pid": process.process.pid,
            "public_port": public_port(process),
            "returncode": process.process.returncode,
            "role": "uvicorn_supervisor" if world.topology == "pg-workers" else "application",
        }
        for process in world.processes
    ]
    return {
        "case": case,
        "recorded_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "checks_passed": CHECKS[case],
        "endpoints": endpoints,
        "services": services,
        "facts": facts,
    }


async def collect(topology, cases):
    started = datetime.now(UTC).isoformat(timespec="milliseconds")
    rows, diagnostics = [], {}

    async def result_hook(world, result):
        rows.append(safe_case(world, result))

    def diagnostics_hook(data):
        diagnostics.update(data)

    await run(topology, cases, _result_hook=result_hook, _diagnostics_hook=diagnostics_hook)
    assert [row["case"] for row in rows] == cases
    return {
        "format_version": 1,
        "topology": topology,
        "started_at_utc": started,
        "finished_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "outcome": "passed",
        "cases": rows,
        "diagnostics": diagnostics,
    }


def write_result(path, report):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", required=True, choices=TOPOLOGIES)
    parser.add_argument("--cases", nargs="+", required=True, choices=CHECKS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在，不会覆盖")
    if len(set(args.cases)) != len(args.cases) or any(case.endswith("-off") for case in args.cases[:-1]):
        parser.error("每个场景只执行一次，关闭内部通道的场景必须最后运行")
    try:
        report = asyncio.run(collect(args.topology, args.cases))
        write_result(args.output, report)
    except Exception as exc:
        # 异常正文可能含密钥和业务内容；只输出固定白名单分类。
        print(json.dumps({"recording_error_category": SCRIPTS["error_category"](exc)}), file=sys.stderr, flush=True)
        sys.exit(1)
    print(json.dumps({"success_report": str(args.output), "topology": args.topology}), flush=True)
