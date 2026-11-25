"""用临时假密钥、令牌和正文触发故障，验证持久报告和控制台均不含原值。"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet

HERE = Path(__file__).resolve().parent
TRELLIS = next(path for path in HERE.parents if path.name == ".trellis")
BACKEND = TRELLIS.parent / "backend"
DIAGNOSTICS = HERE / "diagnostics"


def main():
    DIAGNOSTICS.mkdir(mode=0o700, exist_ok=True)
    before = set(DIAGNOSTICS.glob("*.json"))
    markers = {
        "C7_DIAG_TEST_PASSWORD": f"temporary-password-{secrets.token_hex(16)}",
        "C7_DIAG_TEST_JWT": f"temporary-jwt-{secrets.token_hex(32)}",
        "C7_DIAG_TEST_CIPHER": Fernet.generate_key().decode(),
        "C7_DIAG_TEST_TOKEN": f"temporary-token-{secrets.token_hex(16)}",
        "C7_DIAG_TEST_REQUEST": f"temporary-request-body-{secrets.token_hex(16)}",
        "C7_DIAG_TEST_MODEL": f"temporary-model-body-{secrets.token_hex(16)}",
    }
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update(markers)
    env.update(PYTHONPATH=str(BACKEND), LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8", NO_PROXY="*")
    for flag in ("--inject-failure", "--inject-command-failure"):
        proc = subprocess.run(
            [
                sys.executable,
                str(HERE / "run_operations.py"),
                "--shape",
                "sqlite",
                flag,
                "--diagnostics-dir",
                str(DIAGNOSTICS),
            ],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        added = set(DIAGNOSTICS.glob("*.json")) - before
        assert proc.returncode == 1 and len(added) == 1
        path = added.pop()
        before.add(path)
        raw = path.read_text(encoding="utf-8")
        report = json.loads(raw)
        combined = raw + proc.stdout + proc.stderr
        assert all(value not in combined for value in markers.values())
        assert "Traceback (most recent call last)" not in combined
        assert str(path) in proc.stderr and "隔离临时资源已清理" in proc.stderr
        assert report["shape"] == "sqlite"
        assert datetime.fromisoformat(report["recorded_at_utc"]).tzinfo is not None and report["elapsed_s"] > 0
        assert all(set(frame) == {"file", "line", "function"} and frame["line"] > 0 for frame in report["frames"])
        assert any(frame["file"] == "run_operations.py" for frame in report["frames"])
        assert path.stat().st_mode & 0o077 == 0
        if flag == "--inject-failure":
            assert report["phase"] == "受控故障"
            assert report["category"] == "controlled_failure" and report["exception_type"] == "ControlledFailure"
            assert len(report["worker_pids"]) == 2 and len(report["processes"]) == 2
            assert all(item["pid"] > 0 and item.get("exit_code") is not None for item in report["processes"])
            fault_log = next(item for item in report["log_summaries"] if item["file"] == "controlled-fault.log")
            assert fault_log["error_lines"] == 1
        else:
            assert report["phase"] == "受控子进程故障"
            assert report["category"] == "connection_refused" and report["exception_type"] == "CommandFailure"
            assert report["command_failure"]["program"] == "python" and report["command_failure"]["exit_code"] == 17
            assert report["command_failure"]["pid"] > 0
        print(f"{flag} 脱敏验证通过：{path}")


if __name__ == "__main__":
    main()
