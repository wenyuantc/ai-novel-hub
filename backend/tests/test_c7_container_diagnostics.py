"""容器验收失败时保留受限诊断，并验证真实持久设置接口。"""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.c7_container_diagnostics import Diagnostics, ProbeFailure, wait_ready

BACKEND = Path(__file__).resolve().parents[1]
MARKER = "SECRET_VALUE postgresql://user:secret@private/db NOVEL_CONTENT"


@pytest.mark.parametrize(
    ("script", "suite"),
    [("c7_container_smoke.py", "compose"), ("c7_migration_container_smoke.py", "migration")],
)
def test_failed_command_persists_only_safe_structured_diagnostics(tmp_path, script, suite):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$1:$2" in\n'
        f'  image:tag|network:create) printf "%s\\n" "{MARKER}" >&2; exit 37;;\n'
        '  *) exit 0;;\nesac\n'
    )
    docker.chmod(0o755)
    report_dir = tmp_path / "durable-reports"
    env = {"PATH": f"{binaries}:/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(BACKEND / "tests" / script), "--image", "isolated-test", "--diagnostics-dir",
         str(report_dir)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 1
    reports = list(report_dir.glob("*.json"))
    assert len(reports) == 1
    raw = reports[0].read_text()
    data = json.loads(raw)
    assert data["suite"] == suite
    assert data["error"]["category"] == "command_failed"
    assert data["error"]["exit_status"] == 37
    assert data["scenario"] and data["stage"]
    assert data["trace"] and all(item["file"].endswith(".py") and item["line"] > 0 for item in data["trace"])
    assert "http" in data and "container_exit_statuses" in data
    assert str(reports[0]) in result.stderr
    assert all(
        part not in raw + result.stderr + result.stdout for part in ("SECRET_VALUE", "postgresql://", "NOVEL_CONTENT")
    )


@pytest.fixture
def http_server():
    servers = []

    def start(config_status, payload):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status = 200 if self.path == "/healthz" else config_status
                content = b'{}' if self.path == "/healthz" else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server.server_port

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_readiness_requires_health_and_database_backed_settings(tmp_path, http_server):
    port = http_server(200, {"registration_enabled": True, "mcp_enabled": True})
    diagnostic = Diagnostics("compose", tmp_path)
    wait_ready([port], diagnostic, timeout=1, interval=0.02)

    assert {(item["endpoint"], item["status"]) for item in diagnostic.http} == {
        ("healthz", 200), ("auth_config", 200),
    }
    assert not list(tmp_path.glob("*.json"))


def test_health_alone_does_not_pass_and_response_content_is_not_recorded(tmp_path, http_server):
    port = http_server(503, {"detail": MARKER})
    diagnostic = Diagnostics("migration", tmp_path)
    diagnostic.set_scenario("pg_old")
    diagnostic.set_stage("readiness")
    diagnostic.record_exit(0, 23)
    with pytest.raises(ProbeFailure) as failure:
        wait_ready([port], diagnostic, timeout=0.25, interval=0.02)
    report = diagnostic.persist(failure.value)
    data = json.loads(report.read_text())

    assert data["error"]["category"] == "http_not_ready"
    assert data["container_exit_statuses"] == [{"index": 0, "status": 23, "state": "exited"}]
    assert any(item["endpoint"] == "auth_config" and item["status"] == 503 for item in data["http"])
    assert all(part not in report.read_text() for part in ("SECRET_VALUE", "postgresql://", "NOVEL_CONTENT"))
    assert os.stat(report).st_mode & 0o777 == 0o600


def test_success_status_with_wrong_settings_is_rejected_without_saving_body(tmp_path, http_server):
    port = http_server(200, {"registration_enabled": False, "mcp_enabled": True, "detail": MARKER})
    diagnostic = Diagnostics("compose", tmp_path)
    with pytest.raises(ProbeFailure) as failure:
        wait_ready([port], diagnostic, timeout=0.25, interval=0.02)
    report = diagnostic.persist(failure.value)
    data = json.loads(report.read_text())

    assert data["error"]["category"] == "http_not_ready"
    assert any(item["status"] == 200 and item["category"] == "invalid_payload" for item in data["http"])
    assert all(part not in report.read_text() for part in ("SECRET_VALUE", "postgresql://", "NOVEL_CONTENT"))


def test_compose_failure_records_status_before_resource_cleanup(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_container_smoke")
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if "--services" in args:
            return "app1\napp2\ndb"
        if "--quiet" in args and "ps" in args:
            return args[-1]
        if args[:2] == ["docker", "inspect"]:
            return "exited|23"
        return ""

    def fail_ready(_ports, diagnostic):
        diagnostic.record_http(0, "auth_config", 503, "http_error")
        raise script.ProbeFailure("http_not_ready")

    report_dir = tmp_path / "compose-failure"
    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script, "wait_ready", fail_ready)
    monkeypatch.setattr(sys, "argv", ["script", "--image", "fake", "--diagnostics-dir", str(report_dir)])
    assert script.main() == 1
    data = json.loads(next(report_dir.glob("*.json")).read_text())
    assert data["scenario"] == "default_workers" and data["stage"] == "readiness"
    assert data["error"]["category"] == "http_not_ready"
    assert len(data["container_exit_statuses"]) == 3
    assert all(item["status"] == 23 for item in data["container_exit_statuses"])
    assert data["http"][0]["status"] == 503
    assert any("down" in args for args in calls)
    assert str(report_dir) in capsys.readouterr().err


def test_migration_failure_records_exits_before_removing_containers(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")
    diagnostic = script.Diagnostics("migration", tmp_path / "migration-failure")
    diagnostic.set_scenario("pg_old")
    events = []

    def fake_run(args, **_kwargs):
        if args[:2] == ["docker", "inspect"]:
            events.append("inspect")
            return "exited|17"
        if args[:2] == ["docker", "rm"]:
            events.append("remove")
        return ""

    def fail_wait(_containers, _ports, _env, collector):
        collector.record_http(0, "healthz", 500, "http_error")
        raise script.ProbeFailure("unexpected_http")

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout="", stderr=f"RuntimeError: {MARKER}\n",
    ))
    monkeypatch.setattr(script, "free_ports", lambda _count: [12301, 12302, 12303])
    monkeypatch.setattr(script, "create_three", lambda _args, *, env: ["first", "second", "third"])
    monkeypatch.setattr(script, "wait_without_http", fail_wait)
    with pytest.raises(script.ProbeFailure) as failure:
        script.exercise(
            image="fake", env={}, network="isolated", directory=tmp_path, run_id="fake-pg-old",
            database_url=MARKER, mounts=[], fault=tmp_path / "fault.py", state=lambda: ("schema", "0034", []),
            expected_versions=36, diagnostic=diagnostic,
        )
    report = diagnostic.persist(failure.value)
    data = json.loads(report.read_text())
    assert data["scenario"] == "pg_old" and data["stage"] == "fault_migration"
    assert data["error"]["category"] == "unexpected_http"
    assert len(data["container_exit_statuses"]) == 3
    assert all(item["status"] == 17 for item in data["container_exit_statuses"])
    assert all(item["exception_counts"] == {"RuntimeError": 1} for item in data["migration_logs"])
    assert events == ["inspect", "inspect", "inspect", "remove", "remove", "remove"]
    assert all(part not in report.read_text() for part in ("SECRET_VALUE", "postgresql://", "NOVEL_CONTENT"))


@pytest.mark.parametrize("failure", [True, False], ids=["unexpected-failure", "success"])
def test_isolated_sqlite_is_preserved_only_on_unexpected_failure(tmp_path, monkeypatch, failure):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")
    diagnostic = script.Diagnostics("migration", tmp_path / "reports")
    directory = None
    try:
        with script.isolated_directory(lambda: None, {}, diagnostic) as directory:
            db_dir = directory / "sqlite"
            db_dir.mkdir()
            with sqlite3.connect(db_dir / "old.db") as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('isolated-test-data')")
            (directory / "isolated.env").write_text(MARKER)
            if failure:
                raise ValueError(MARKER)
    except ValueError as exc:
        assert failure
        report = diagnostic.persist(exc)
        assert all(part not in report.read_text() for part in ("SECRET_VALUE", "postgresql://", "NOVEL_CONTENT"))

    assert directory is not None and not directory.exists()
    if failure:
        snapshots = list((tmp_path / "reports").rglob("*.sqlite3"))
        assert len(snapshots) == 1
        with sqlite3.connect(snapshots[0]) as connection:
            assert connection.execute("SELECT value FROM sample").fetchone() == ("isolated-test-data",)
        assert os.stat(snapshots[0]).st_mode & 0o777 == 0o600
        assert not list((tmp_path / "reports").rglob("*.env"))
    else:
        assert not (tmp_path / "reports").exists()


def test_compose_cleanup_failure_cannot_be_reported_as_success(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_container_smoke")

    def fake_run(args, **_kwargs):
        if "--services" in args:
            return "app1\napp2\ndb"
        if "down" in args:
            raise script.ProbeFailure("command_failed", exit_status=42)
        return ""

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script, "wait_ready", lambda *_args: None)
    monkeypatch.setattr(script, "wait_workers", lambda *_args: {})
    monkeypatch.setattr(script, "verify_exposure", lambda *_args: None)
    monkeypatch.setattr(script, "verify_peer_links", lambda *_args: None)
    monkeypatch.setattr(script, "verify_external_connections", lambda *_args: None)
    report_dir = tmp_path / "cleanup-failure"
    monkeypatch.setattr(sys, "argv", ["script", "--image", "fake", "--diagnostics-dir", str(report_dir)])
    assert script.main() == 1
    data = json.loads(next(report_dir.glob("*.json")).read_text())
    assert data["stage"] == "cleanup" and data["error"]["category"] == "cleanup_unconfirmed"
    assert data["cleanup"]["confirmed"] is False


def test_migration_timeout_terminates_and_waits_all_docker_clients(monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")
    children = []

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.waited = False
            children.append(self)

        def communicate(self, timeout):
            if timeout == 120:
                raise subprocess.TimeoutExpired("docker", timeout)
            self.waited = True
            self.returncode = -15
            return "", ""

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(script.subprocess, "Popen", FakeProcess)
    with pytest.raises(subprocess.TimeoutExpired):
        script.create_three([["one"], ["two"], ["three"]], env={})
    assert len(children) == 3
    assert all(child.waited and child.poll() is not None for child in children)


@pytest.mark.parametrize("failure_type", [OSError, KeyboardInterrupt])
def test_migration_create_interruption_marks_cleanup_uncertain(tmp_path, monkeypatch, failure_type):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")
    diagnostic = Diagnostics("migration", tmp_path)
    monkeypatch.setattr(script, "free_ports", lambda _count: [12301, 12302, 12303])

    def fail_create(*_args, **_kwargs):
        raise failure_type("created CLI may still be running on the daemon")

    monkeypatch.setattr(script, "create_three", fail_create)
    with pytest.raises(failure_type):
        script.exercise(
            image="fake", env={}, network="isolated", directory=tmp_path, run_id="fake-pg-old",
            database_url="unused", mounts=[], fault=tmp_path / "fault.py", state=lambda: ([], "", []),
            expected_versions=36, diagnostic=diagnostic,
        )
    assert diagnostic.cleanup_uncertain is True


def test_postgresql_failure_snapshot_only_includes_script_owned_databases(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")
    diagnostic = script.Diagnostics("migration", tmp_path / "reports")
    commands = []

    def fake_pg_sql(*_args):
        return "c7_old\nnot_owned"

    def fake_process(args, **_kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout=b"isolated-pg-dump")

    monkeypatch.setattr(script, "pg_sql", fake_pg_sql)
    monkeypatch.setattr(script.subprocess, "run", fake_process)
    script.preserve_databases(tmp_path, "owned-pg", {}, diagnostic)
    dumps = list(diagnostic.snapshot_dir.glob("*.dump"))
    assert [path.name for path in dumps] == ["pg-c7_old.dump"]
    assert dumps[0].read_bytes() == b"isolated-pg-dump"
    assert os.stat(dumps[0]).st_mode & 0o777 == 0o600
    assert len(commands) == 1 and commands[0][-1] == "c7_old"


def test_migration_cleanup_must_confirm_resources_are_gone(monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")

    def cannot_remove(args, **_kwargs):
        if args[:2] == ["docker", "ps"]:
            return "still-running"
        if args[:2] == ["docker", "rm"]:
            raise script.ProbeFailure("command_failed", exit_status=1)
        return ""

    monkeypatch.setattr(script, "run", cannot_remove)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    with pytest.raises(script.ProbeFailure) as failure:
        script.cleanup_owned("isolated-run", {})
    assert failure.value.category == "cleanup_unconfirmed"


@pytest.mark.parametrize("timeout", [False, True], ids=["cli-error", "cli-timeout"])
def test_failed_network_create_cleans_owned_network_but_does_not_claim_certainty(tmp_path, monkeypatch, timeout):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")
    diagnostic = Diagnostics("migration", tmp_path)
    created = []
    removed = []
    monkeypatch.setattr(script.secrets, "token_hex", lambda _length: "0123456789abcdef")
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)

    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "network", "create"]:
            created.append("owned-network")
            if timeout:
                raise subprocess.TimeoutExpired("docker", 120)
            raise script.ProbeFailure("command_failed", exit_status=1)
        if args[:3] == ["docker", "network", "ls"]:
            assert args[-1] == "label=ainovel.c7=c7mig0123456789abcdef"
            return "\n".join(created)
        if args[:3] == ["docker", "network", "rm"]:
            removed.append(args[-1])
            created.remove(args[-1])
        return ""

    monkeypatch.setattr(script, "run", fake_run)
    with pytest.raises((script.ProbeFailure, subprocess.TimeoutExpired)):
        script.execute(SimpleNamespace(image="unused"), diagnostic)
    assert removed == ["owned-network"] and not created
    assert diagnostic.cleanup_confirmed is False


@pytest.mark.parametrize("database", ["pg", "sqlite"])
def test_legacy_seed_cli_failure_marks_cleanup_uncertain(tmp_path, monkeypatch, database):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_migration_container_smoke")
    diagnostic = Diagnostics("migration", tmp_path)
    cleaned = []

    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "network", "create"]:
            return "owned-network"
        if args[:3] == ["docker", "run", "--rm"]:
            is_sqlite = "--mount" in args
            if (database == "sqlite") == is_sqlite:
                raise script.ProbeFailure("command_failed", exit_status=1)
        return ""

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script, "create_container", lambda *_args, **_kwargs: "db-container")
    monkeypatch.setattr(script, "exercise", lambda **_kwargs: None)
    monkeypatch.setattr(script, "cleanup_owned", lambda run_id, env: cleaned.append(run_id))
    monkeypatch.setattr(script.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    with pytest.raises(script.ProbeFailure):
        script.execute(SimpleNamespace(image="unused"), diagnostic)
    assert len(cleaned) == 1
    assert diagnostic.cleanup_confirmed is False


@pytest.mark.parametrize("existing", [False, True], ids=["tag-timeout", "preexisting-tag"])
def test_compose_tag_cleanup_never_deletes_a_preexisting_tag(tmp_path, monkeypatch, existing):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_container_smoke")
    diagnostic = Diagnostics("compose", tmp_path)
    monkeypatch.setattr(script.secrets, "token_hex", lambda _length: "0123456789abcdef")
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    tags = {"c7deploy0123456789abcdef-app1"} if existing else set()
    removed = []

    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "image", "ls"]:
            tag = args[-1].removeprefix("reference=")
            return "owned-id" if tag in tags else ""
        if args[:3] == ["docker", "image", "tag"]:
            tags.add(args[-1])
            raise subprocess.TimeoutExpired("docker", 120)
        if args[:3] == ["docker", "image", "rm"]:
            removed.append(args[-1])
            tags.remove(args[-1])
        return ""

    monkeypatch.setattr(script, "run", fake_run)
    with pytest.raises((script.ProbeFailure, subprocess.TimeoutExpired)):
        script.execute(SimpleNamespace(image="preexisting-source"), diagnostic)
    if existing:
        assert not removed and tags == {"c7deploy0123456789abcdef-app1"}
    else:
        assert removed == ["c7deploy0123456789abcdef-app1"] and not tags
        assert diagnostic.cleanup_confirmed is False


def test_compose_rejects_preexisting_external_label_before_any_cleanup(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_container_smoke")
    diagnostic = Diagnostics("compose", tmp_path)
    commands = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        if args[:2] == ["docker", "ps"] and "label=ainovel.c7.external=" in args[-1]:
            return "some-existing-container"
        return ""

    monkeypatch.setattr(script, "run", fake_run)
    with pytest.raises(script.ProbeFailure) as failure:
        script.execute(SimpleNamespace(image="unused"), diagnostic)
    assert failure.value.category == "invalid_state"
    assert not any(args[:2] == ["docker", "rm"] for args in commands)


@pytest.mark.parametrize("failure_type", ["command", "os-error", "interrupted"])
def test_compose_recreate_failure_does_not_claim_cleanup_certainty(tmp_path, monkeypatch, failure_type):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_container_smoke")
    diagnostic = Diagnostics("compose", tmp_path)

    def fake_run(args, **_kwargs):
        if "--services" in args:
            return "app1\napp2\ndb"
        if "--force-recreate" in args:
            if failure_type == "os-error":
                raise OSError("daemon outcome is unknown")
            if failure_type == "interrupted":
                raise KeyboardInterrupt
            raise script.ProbeFailure("command_failed", exit_status=1)
        return ""

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(script, "wait_ready", lambda *_args: None)
    monkeypatch.setattr(script, "wait_workers", lambda *_args: {})
    monkeypatch.setattr(script, "verify_exposure", lambda *_args: [])
    monkeypatch.setattr(script, "verify_peer_links", lambda *_args: None)
    monkeypatch.setattr(script, "verify_external_connections", lambda *_args: None)
    with pytest.raises((script.ProbeFailure, OSError, KeyboardInterrupt)):
        script.execute(SimpleNamespace(image="unused"), diagnostic)
    assert diagnostic.stage == "recreate"
    assert diagnostic.cleanup_confirmed is False


def test_external_peer_probe_rejects_a_reachable_private_endpoint(monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_container_smoke")
    script.require_external_isolation([{"connected": False}, {"connected": False}])
    with pytest.raises(script.ProbeFailure) as failure:
        script.require_external_isolation([{"connected": False}, {"connected": True}])
    assert failure.value.category == "external_network_reachable"


@pytest.mark.parametrize("positive_control", [False, True], ids=["no-positive-control", "peer-died"])
def test_external_probe_rejects_dead_network_or_peer(tmp_path, monkeypatch, positive_control):
    monkeypatch.syspath_prepend(str(BACKEND / "tests"))
    script = importlib.import_module("c7_container_smoke")
    diagnostic = script.Diagnostics("compose", tmp_path)
    events = []

    def fake_run(args, **_kwargs):
        if args[:3] == ["docker", "network", "create"]:
            return "outside-network"
        if args[:3] == ["docker", "run", "-d"]:
            events.append("control")
            return "control-container"
        if args[:2] == ["docker", "create"]:
            events.append("probe")
            return "probe-container"
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([{"NetworkSettings": {"Networks": {"project-setup-outside": {}}}}])
        if args[:2] == ["docker", "logs"]:
            return json.dumps({"positive_control": positive_control, "targets": [{"connected": False}]})
        return ""

    def dead_peer():
        events.append("trusted-recheck")
        raise script.ProbeFailure("command_failed")

    monkeypatch.setattr(script, "run", fake_run)
    with pytest.raises(script.ProbeFailure) as failure:
        script.verify_external_connections({}, [("192.0.2.1", 10000)], "image", "project", diagnostic, dead_peer)
    assert failure.value.category == ("command_failed" if positive_control else "external_network_unavailable")
    assert events == (["control", "probe", "trusted-recheck"] if positive_control else ["control", "probe"])


def test_migration_log_keeps_allowlisted_clues_without_messages_or_sql(tmp_path):
    diagnostic = Diagnostics("migration", tmp_path)
    diagnostic.set_scenario("pg_old")
    diagnostic.set_stage("fault_migration")
    diagnostic.record_migration_log(
        0, "fault", f"INFO  [alembic.runtime.migration] Running upgrade 0034 -> 0035, {MARKER}\n"
        f"RuntimeError: {MARKER}\n[SQL: INSERT INTO secret VALUES ('{MARKER}')]\n",
    )
    report = diagnostic.persist(ProbeFailure("migration_failure"))
    data = json.loads(report.read_text())
    assert data["raw_log_retained"] is False
    assert data["migration_logs"] == [{
        "phase": "fault", "index": 0, "available": True,
        "transitions": [{"level": "INFO", "action": "upgrade", "from": "0034", "to": "0035"}],
        "exception_counts": {"RuntimeError": 1},
    }]
    assert all(part not in report.read_text() for part in ("SECRET_VALUE", "postgresql://", "NOVEL_CONTENT", "INSERT"))
