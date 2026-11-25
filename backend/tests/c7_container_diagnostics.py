"""容器验收的受限故障诊断与数据库可用性探针。"""

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

SCENARIOS = {"setup", "compose", "default_workers", "four_workers", "pg_empty", "pg_old", "sqlite_empty", "sqlite_old"}
STAGES = {
    "preflight", "image_tag", "compose_config", "compose_start", "services", "readiness", "worker_registration",
    "port_exposure", "peer_connectivity", "recreate", "network_create", "database_ready", "legacy_seed",
    "fault_migration", "rollback_check", "retry_migration", "success_verify", "external_network", "cleanup",
}
ERRORS = {
    "command_failed", "container_start_failed", "http_not_ready", "unexpected_http", "worker_registration",
    "port_exposure", "schema_mismatch", "migration_count", "migration_failure", "invalid_state", "timeout",
    "system_error", "unexpected_error", "interrupted", "cleanup_unconfirmed", "external_network_unavailable",
    "external_network_reachable", "other",
}
TRACE_FILES = {"c7_container_smoke.py", "c7_migration_container_smoke.py", "c7_container_diagnostics.py"}
DEFAULT_REPORT_DIR = Path(tempfile.gettempdir()) / "ainovel-c7-container-diagnostics"
MIGRATION_LINE = re.compile(
    r"^\s*(INFO|WARN|ERROR)\s+\[alembic\.runtime\.migration\]\s+Running "
    r"(upgrade|downgrade)\s+([0-9]{4})?\s*->\s*([0-9]{4})(?:,|\s|$)"
)
EXCEPTION_LINE = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)*"
    r"(RuntimeError|IntegrityError|OperationalError|ProgrammingError|TimeoutError|DBAPIError)(?::|$)"
)


class ProbeFailure(RuntimeError):
    def __init__(self, category, *, exit_status=None):
        self.category = category if category in ERRORS else "other"
        self.exit_status = exit_status if type(exit_status) is int else None
        super().__init__(self.category)


class Diagnostics:
    def __init__(self, suite, report_dir=DEFAULT_REPORT_DIR):
        self.suite = suite if suite in {"compose", "migration"} else "other"
        self.report_dir = Path(report_dir)
        self.run_id = secrets.token_hex(12)
        self.scenario = "setup"
        self.stage = "preflight"
        self.http = []
        self.container_exit_statuses = []
        self.cleanup_confirmed = None
        self.cleanup_uncertain = False
        self.migration_logs = []
        self.snapshots = []
        self.snapshot_failed = False

    def set_scenario(self, scenario):
        self.scenario = scenario if scenario in SCENARIOS else "other"
        self.http.clear()
        self.container_exit_statuses.clear()
        self.migration_logs.clear()

    def set_stage(self, stage):
        self.stage = stage if stage in STAGES else "other"

    def record_http(self, index, endpoint, status, category):
        if type(index) is not int or index < 0 or endpoint not in {"healthz", "auth_config"}:
            return
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        if category not in {"ok", "http_error", "connection_error", "invalid_payload"}:
            category = "other"
        self.http[:] = [item for item in self.http if (item["index"], item["endpoint"]) != (index, endpoint)]
        self.http.append({"index": index, "endpoint": endpoint, "status": status, "category": category})

    def record_exit(self, index, status, state="exited"):
        if type(index) is not int or index < 0 or (status is not None and type(status) is not int):
            return
        if state not in {"running", "exited", "dead", "created", "restarting"}:
            state = "other"
        self.container_exit_statuses[:] = [item for item in self.container_exit_statuses if item["index"] != index]
        self.container_exit_statuses.append({"index": index, "status": status, "state": state})

    @property
    def snapshot_dir(self):
        return self.report_dir / f"c7-{self.suite}-{self.run_id}-databases"

    def record_snapshot(self, kind, filename):
        allowed = {
            ("sqlite", "sqlite-empty.sqlite3"), ("sqlite", "sqlite-old.sqlite3"),
            ("postgresql", "pg-c7_empty.dump"), ("postgresql", "pg-c7_old.dump"),
        }
        if (kind, filename) in allowed:
            self.snapshots.append({"kind": kind, "file": filename})

    def record_migration_log(self, index, phase, output):
        if type(index) is not int or index < 0 or phase not in {"fault", "retry"}:
            return
        transitions = []
        exceptions = {}
        if isinstance(output, str):
            for line in output.splitlines():
                revision = MIGRATION_LINE.match(line)
                if revision and len(transitions) < 100:
                    level, action, source, target = revision.groups()
                    transitions.append({"level": level, "action": action, "from": source or "base", "to": target})
                exception = EXCEPTION_LINE.match(line)
                if exception:
                    kind = exception.group(1)
                    exceptions[kind] = exceptions.get(kind, 0) + 1
        self.migration_logs[:] = [item for item in self.migration_logs
                                  if (item["phase"], item["index"]) != (phase, index)]
        self.migration_logs.append({
            "phase": phase, "index": index, "available": isinstance(output, str),
            "transitions": transitions, "exception_counts": exceptions,
        })

    def persist(self, exc):
        if isinstance(exc, ProbeFailure):
            category, exit_status = exc.category, exc.exit_status
        elif isinstance(exc, subprocess.TimeoutExpired):
            category, exit_status = "timeout", None
        elif isinstance(exc, KeyboardInterrupt):
            category, exit_status = "interrupted", None
        elif isinstance(exc, OSError):
            category, exit_status = "system_error", None
        else:
            category, exit_status = "unexpected_error", None
        trace = []
        for frame in traceback.extract_tb(exc.__traceback__):
            name = Path(frame.filename).name
            if name in TRACE_FILES:
                trace.append({"file": name, "line": frame.lineno})
        payload = {
            "schema_version": 1,
            "suite": self.suite,
            "scenario": self.scenario,
            "stage": self.stage,
            "error": {"category": category, "exit_status": exit_status},
            "container_exit_statuses": self.container_exit_statuses,
            "http": self.http,
            "cleanup": {"confirmed": self.cleanup_confirmed},
            "migration_logs": self.migration_logs,
            "raw_log_retained": False,
            "snapshots": self.snapshots,
            "snapshot_failed": self.snapshot_failed,
            "trace": trace,
        }
        self.report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.report_dir / f"c7-{self.suite}-{self.run_id}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        return path


def checked_run(args, *, cwd, env, timeout=120, check=True):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise ProbeFailure("command_failed", exit_status=result.returncode)
    return result.stdout.strip()


def _probe(index, port, endpoint, diagnostic, timeout):
    path = "/healthz" if endpoint == "healthz" else "/api/v1/auth/config"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            status = response.status
            if status != 200:
                diagnostic.record_http(index, endpoint, status, "http_error")
                return False
            if endpoint == "auth_config":
                body = response.read(4097)
                if len(body) > 4096:
                    diagnostic.record_http(index, endpoint, status, "invalid_payload")
                    return False
                try:
                    value = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    diagnostic.record_http(index, endpoint, status, "invalid_payload")
                    return False
                if not isinstance(value, dict) or value.get("registration_enabled") is not True \
                        or value.get("mcp_enabled") is not True:
                    diagnostic.record_http(index, endpoint, status, "invalid_payload")
                    return False
            diagnostic.record_http(index, endpoint, status, "ok")
            return True
    except urllib.error.HTTPError as exc:
        diagnostic.record_http(index, endpoint, exc.code, "http_error")
    except (OSError, urllib.error.URLError):
        diagnostic.record_http(index, endpoint, None, "connection_error")
    return False


def wait_ready(ports, diagnostic, *, timeout=90, interval=0.25):
    deadline = time.monotonic() + timeout
    pending = set(range(len(ports)))
    while pending and time.monotonic() < deadline:
        for index in list(pending):
            request_timeout = max(0.05, min(2.0, deadline - time.monotonic()))
            if _probe(index, ports[index], "healthz", diagnostic, request_timeout) and _probe(
                index, ports[index], "auth_config", diagnostic, request_timeout,
            ):
                pending.remove(index)
        if pending:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
    if pending:
        raise ProbeFailure("http_not_ready")
