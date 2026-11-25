"""以虚构凭据和正文验证失败报告留存、脱敏及临时资源清理。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

FAKE_SECRET = "C7_DIAGNOSTIC_FAKE_KEY_78ac4d"
FAKE_BODY = "C7_DIAGNOSTIC_FAKE_STORY_62e9"


def main():
    root = Path(tempfile.mkdtemp(prefix="ainovel-c7-diagnostic-proof-")).resolve()
    temporary = root / "runtime"
    reports = root / "reports"
    binary_dir = root / "bin"
    container_ids = root / "container-ids.txt"
    temporary.mkdir(mode=0o700)
    reports.mkdir(mode=0o700)
    binary_dir.mkdir(mode=0o700)
    docker = shutil.which("docker")
    assert docker, "没有找到 Docker"
    wrapper = binary_dir / "docker"
    wrapper.write_text(
        """#!/usr/bin/env python3
import os, subprocess, sys
real = os.environ["C7_REAL_DOCKER"]
if len(sys.argv) > 1 and sys.argv[1] == "run":
    done = subprocess.run([real, *sys.argv[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if done.returncode == 0:
        with open(os.environ["C7_CONTAINER_IDS"], "a", encoding="utf-8") as target:
            target.write(done.stdout.decode().strip() + "\\n")
    sys.stdout.buffer.write(done.stdout)
    sys.stderr.buffer.write(done.stderr)
    raise SystemExit(done.returncode)
os.execv(real, [real, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    harness = Path(__file__).resolve().with_name("run_pg_cancellation.py")
    repository = next(p for p in harness.parents if (p / "backend/alembic.ini").is_file())
    backend = repository / "backend"
    env = {
        "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"], "PYTHONPATH": str(backend),
        "TMPDIR": str(temporary), "C7_REAL_DOCKER": docker, "C7_CONTAINER_IDS": str(container_ids),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "NO_PROXY": "*",
    }
    run = subprocess.run(
        [sys.executable, str(harness), "--topology", "pg-instances", "--cases", "owner", "--simulate-failure",
         "--diagnostic-dir", str(reports)],
        cwd=backend, env=env, capture_output=True, text=True, timeout=240, check=False,
    )
    assert run.returncode == 1, "受控失败未按预期退出"
    assert all(secret not in run.stdout and secret not in run.stderr for secret in (FAKE_SECRET, FAKE_BODY))
    events = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    report_path = Path(next(event["failure_report"] for event in events if "failure_report" in event))
    assert report_path.is_relative_to(reports) and report_path.is_file(), "失败报告没有保留"
    raw = report_path.read_text(encoding="utf-8")
    assert all(secret not in raw for secret in (FAKE_SECRET, FAKE_BODY)), "报告泄露了虚构凭据或正文"
    report = json.loads(raw)
    assert report["schema"] == "c7-scheduling-failure-v1" and report["topology"] == "pg-instances"
    assert report["case"] == "owner" and report["error_category"] == "RuntimeError"
    assert report["location"]["file"] == "run_pg_cancellation.py" and report["location"]["line"] > 0
    assert report["http_5xx_count"] == 1 and report["invalid_http_diagnostic_lines"] == 0
    assert report["elapsed_seconds"] >= 0 and datetime.fromisoformat(report["time_utc"]).tzinfo is not None
    processes = report["processes"]
    assert processes["runner_pid"] > 0 and len(processes["application_pids"]) == 2
    assert processes["executor_pid"] != processes["request_pid"]
    assert report_path.stat().st_mode & 0o077 == 0 and report_path.parent.stat().st_mode & 0o077 == 0
    assert not list(temporary.iterdir()), "失败后仍有未清理的临时运行目录"
    for pid in (processes["runner_pid"], *processes["application_pids"]):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError("验收进程仍未退出")

    success = subprocess.run(
        [sys.executable, str(harness), "--topology", "pg-instances", "--cases", "owner",
         "--diagnostic-dir", str(reports)],
        cwd=backend, env=env, capture_output=True, text=True, timeout=240, check=False,
    )
    assert success.returncode == 0, "正常场景未通过"
    success_events = [json.loads(line) for line in success.stdout.splitlines() if line.startswith("{")]
    assert any(event.get("http_5xx_count") == 0 for event in success_events)
    assert not any("failure_report" in event for event in success_events)
    assert report_path.is_file() and list(reports.rglob("failure.json")) == [report_path]
    assert not list(temporary.iterdir()), "成功后仍有未清理的临时运行目录"
    created = container_ids.read_text(encoding="utf-8").splitlines()
    assert len(created) == 2 and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in created)
    for container in created:
        inspect = subprocess.run([docker, "inspect", container], capture_output=True, text=True, check=False)
        assert inspect.returncode != 0 and "no such object" in inspect.stderr.lower(), "验收容器未确认删除"
    for event in success_events:
        if event.get("case") == "owner":
            for pid in (event["executor_pid"], event["request_pid"]):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                raise AssertionError("成功场景的应用进程仍未退出")
    temporary.rmdir()
    wrapper.unlink()
    binary_dir.rmdir()
    container_ids.unlink()
    print(json.dumps({"passed": True, "failure_report": str(report_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
