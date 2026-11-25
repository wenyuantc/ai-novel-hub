"""从零部署验收仅管理自己创建的目录、配置和诊断文件。"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("from_scratch_smoke.py")
SPEC = importlib.util.spec_from_file_location("c7_from_scratch_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
PEER_SPEC = importlib.util.spec_from_file_location(
    "c7_guide_peer_probe", Path(__file__).with_name("guide_peer_probe.py"),
)
peer_probe = importlib.util.module_from_spec(PEER_SPEC)
PEER_SPEC.loader.exec_module(peer_probe)
UI_SPEC = importlib.util.spec_from_file_location("c7_guide_ui_probe", Path(__file__).with_name("guide_ui_probe.py"))
ui_probe = importlib.util.module_from_spec(UI_SPEC)
UI_SPEC.loader.exec_module(ui_probe)


class FakeMachine:
    def __init__(self, mode="success"):
        self.mode = mode
        self.events = []
        self.workspaces = set()
        self.configs = []
        self.pending = None

    def invoke(self, args, stage, **_kwargs):
        self.events.append(stage)
        for arg in args:
            if arg.startswith("DOCKER_CONFIG="):
                self.configs.append(arg.removeprefix("DOCKER_CONFIG="))
        if stage == "workspace_preflight":
            self.pending = args[-1]
            if self.mode == "occupied":
                raise smoke.Failure(stage, 1)
        if stage == "workspace":
            assert args[-2] == self.pending
            if self.mode != "unowned":
                self.workspaces.add(self.pending)
            if self.mode in {"timeout", "unowned"}:
                raise smoke.Failure(stage, "timeout")
        if stage in {"workspace_verify", "workspace_ownership"}:
            return SimpleNamespace(returncode=0 if args[-2] in self.workspaces else 1, stdout="")
        if stage == "workspace_cleanup":
            assert args[-2] in self.workspaces
            if self.mode == "cleanup_error":
                raise smoke.Failure(stage, 1)
            self.workspaces.remove(args[-2])
        if stage == "workspace_absent":
            assert args[-1] not in self.workspaces
        if stage == "test_env":
            return SimpleNamespace(returncode=0, stdout="32001 32002\n")
        if stage == "dockerignore_sha":
            digest = hashlib.sha256((smoke.ROOT / ".dockerignore").read_bytes()).hexdigest()
            return SimpleNamespace(returncode=0, stdout=f"{digest}  .dockerignore\n")
        if stage == "compose_services":
            return SimpleNamespace(returncode=0, stdout="db\napp1\napp2\n")
        if stage == "http":
            return SimpleNamespace(returncode=0, stdout=json.dumps([
                {"endpoint": "healthz", "status": 200}, {"endpoint": "auth_config", "status": 200},
                {"endpoint": "healthz", "status": 200}, {"endpoint": "auth_config", "status": 200},
            ]))
        if stage == "migration_sha":
            digest = hashlib.sha256((smoke.ROOT / "backend/alembic/env.py").read_bytes()).hexdigest()
            return SimpleNamespace(returncode=0, stdout=f"{digest}  env.py\n")
        if stage == "restore_helper_sha":
            helper = smoke.ROOT / "backend/app/services/backups/sqlite_restore.py"
            digest = hashlib.sha256(helper.read_bytes()).hexdigest()
            return SimpleNamespace(returncode=0, stdout=f"{digest}  sqlite_restore.py\n")
        if stage == "admin_ui":
            return SimpleNamespace(returncode=1 if self.mode == "ui_failure" else 0,
                                   stdout=json.dumps({"ui_page": self.mode != "ui_failure", "workers": [
                {"hostname": "host1", "pid": 1, "extra": "SECRET_VALUE"},
                {"hostname": "wrong" if self.mode == "ui_mismatch" else "host2", "pid": 1},
            ]}))
        if stage == "peer_checks":
            return SimpleNamespace(returncode=1 if self.mode == "peer_failure" else 0, stdout=json.dumps({
                "positive_control": True, "trusted_before_after": True, "external_blocked": True,
                "stage": "connectivity", "error": "external_network_reachable" if self.mode == "peer_failure" else None,
                "cleanup_confirmed": self.mode != "peer_failure", "db_workers": [
                    {"hostname": "host1", "pid": 1, "service": "app1", "peer_port": 12345,
                     "extra": "SECRET_VALUE"},
                    {"hostname": "host2", "pid": 1, "service": "app2", "peer_port": 12346},
                ],
            }))
        return SimpleNamespace(returncode=0, stdout="")


def setup_run(tmp_path, monkeypatch, machine):
    monkeypatch.setattr(smoke, "invoke", machine.invoke)

    def fake_transfer(source, _target, stage):
        if stage == "c7_overlay":
            assert ".dockerignore" in source
            assert "backend/app/services/backups/sqlite_restore.py" in source

    monkeypatch.setattr(smoke, "transfer", fake_transfer)
    monkeypatch.setattr(sys, "argv", ["script", "--machine", "c7accept0123456789ab", "--report-dir", str(tmp_path)])


@pytest.mark.parametrize("task_path", [".trellis/tasks/c7", ".trellis/tasks/archive/2026-11/c7"])
@pytest.mark.parametrize("script_name", ["from_scratch_smoke.py", "healthcheck_smoke.py", "guide_peer_probe.py"])
def test_scripts_find_root_at_active_and_archived_depths(tmp_path, task_path, script_name):
    root = tmp_path / "project"
    marker = root / "backend/alembic/env.py"
    marker.parent.mkdir(parents=True)
    marker.touch()
    (root / "Dockerfile").touch()
    tests_dir = root / "backend/tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "c7_container_smoke.py").write_text("# synthetic import target\n")
    (tests_dir / "c7_container_diagnostics.py").write_text(
        "class Diagnostics: pass\nclass ProbeFailure(Exception): pass\n",
    )
    target = root / task_path / "research/c7_container_acceptance" / script_name
    target.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name(script_name), target)
    loader = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('probe',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "print(module.ROOT)"
    )
    result = subprocess.run([sys.executable, "-c", loader, str(target)], capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()) == root


def test_peer_probe_finds_remote_tooling_without_application_tree(tmp_path):
    tooling = tmp_path / "tooling"
    tests_dir = tooling / "backend/tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "c7_container_smoke.py").touch()
    (tests_dir / "c7_container_diagnostics.py").write_text(
        "class Diagnostics: pass\nclass ProbeFailure(Exception): pass\n",
    )
    target = tooling / ".trellis/tasks/archive/2026-11/c7/research/c7_container_acceptance/guide_peer_probe.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name(target.name), target)
    loader = (
        "import importlib.util,sys; "
        "spec=importlib.util.spec_from_file_location('probe',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "print(module.ROOT)"
    )
    result = subprocess.run([sys.executable, "-c", loader, str(target)], capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()) == tooling
    assert not (tooling / "Dockerfile").exists() and not (tooling / "backend/alembic.ini").exists()


@pytest.mark.parametrize("script_name", ["from_scratch_smoke.py", "healthcheck_smoke.py", "guide_peer_probe.py"])
def test_scripts_fail_when_no_dependency_root_exists(tmp_path, script_name):
    target = tmp_path / "isolated" / script_name
    target.parent.mkdir()
    shutil.copyfile(Path(__file__).with_name(script_name), target)
    result = subprocess.run([sys.executable, str(target)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Cannot locate" in result.stderr


def test_archived_guide_transfers_and_runs_probes_from_archived_path(tmp_path, monkeypatch):
    machine = FakeMachine()
    setup_run(tmp_path, monkeypatch, machine)
    archived = ".trellis/tasks/archive/2026-11/c7/research/c7_container_acceptance"
    monkeypatch.setattr(smoke, "REPORT_DIR", smoke.ROOT / archived)
    transfers, calls = {}, {}

    def capture_transfer(source, _target, stage):
        transfers[stage] = source

    def capture_invoke(args, stage, **kwargs):
        calls[stage] = args
        return machine.invoke(args, stage, **kwargs)

    monkeypatch.setattr(smoke, "transfer", capture_transfer)
    monkeypatch.setattr(smoke, "invoke", capture_invoke)
    assert smoke.main() == 0
    for filename, stage in (("guide_ui_probe.py", "admin_ui"), ("guide_peer_probe.py", "peer_checks")):
        assert f"{archived}/{filename}" in transfers["probes"]
        assert any(arg.endswith(f"/tooling/{archived}/{filename}") for arg in calls[stage])


def test_results_are_unique_and_docker_config_is_private_to_each_run(tmp_path, monkeypatch):
    machine = FakeMachine()
    setup_run(tmp_path, monkeypatch, machine)
    old = tmp_path / "from-scratch-result.json"
    old.write_text('{"previous":true}\n')
    assert smoke.main() == 0
    assert smoke.main() == 0

    reports = sorted(tmp_path.glob("from-scratch-result-*.json"))
    assert len(reports) == 2 and old.read_text() == '{"previous":true}\n'
    assert all(json.loads(path.read_text())["cleanup_confirmed"] is True for path in reports)
    assert all(json.loads(path.read_text())["peer_checks"]["external_blocked"] is True for path in reports)
    assert all("SECRET_VALUE" not in path.read_text() for path in reports)
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in reports)
    configs = set(machine.configs)
    assert len(configs) == 2
    assert all(config.startswith("/tmp/c7doc") and config.endswith("/docker-config") for config in configs)
    assert "/tmp/c7-docker-config" not in configs
    assert not machine.workspaces


@pytest.mark.parametrize("mode", ["timeout", "unowned", "cleanup_error"])
def test_unknown_or_failed_workspace_cleanup_is_never_confirmed(tmp_path, monkeypatch, mode):
    machine = FakeMachine(mode)
    setup_run(tmp_path, monkeypatch, machine)
    assert smoke.main() == 1
    result = json.loads(next(tmp_path.glob("from-scratch-result-*.json")).read_text())
    assert result["cleanup_confirmed"] is False
    assert result["workspace"]["path"] == machine.pending
    assert machine.events.index("workspace_preflight") < machine.events.index("workspace")
    if mode == "timeout":
        assert "workspace_cleanup" in machine.events and not machine.workspaces
    elif mode == "unowned":
        assert "workspace_cleanup" not in machine.events
    else:
        assert machine.workspaces == {machine.pending}


def test_preexisting_workspace_is_not_removed(tmp_path, monkeypatch):
    machine = FakeMachine("occupied")
    setup_run(tmp_path, monkeypatch, machine)
    assert smoke.main() == 1
    assert machine.events == ["workspace_preflight"]
    result = json.loads(next(tmp_path.glob("from-scratch-result-*.json")).read_text())
    assert result["error"]["stage"] == "workspace_preflight"
    assert "workspace" not in result


def test_report_write_failure_is_reported(tmp_path, monkeypatch, capsys):
    machine = FakeMachine()
    setup_run(tmp_path, monkeypatch, machine)

    def fail_report(*_args, **_kwargs):
        raise OSError("failure text must not be printed")

    monkeypatch.setattr(smoke, "persist_result", fail_report)
    assert smoke.main() == 1
    captured = capsys.readouterr()
    assert "无法保存" in captured.err and "failure text" not in captured.err
    assert not machine.workspaces


@pytest.mark.parametrize("mode", ["ui_mismatch", "ui_failure", "peer_failure"])
def test_guide_cannot_pass_with_ui_mismatch_or_unknown_peer_cleanup(tmp_path, monkeypatch, mode):
    machine = FakeMachine(mode)
    setup_run(tmp_path, monkeypatch, machine)
    assert smoke.main() == 1
    result = json.loads(next(tmp_path.glob("from-scratch-result-*.json")).read_text())
    assert result["error"]["stage"] == {
        "ui_mismatch": "admin_peer_match", "ui_failure": "admin_ui", "peer_failure": "peer_checks",
    }[mode]
    assert result["cleanup_confirmed"] is (mode == "ui_mismatch")
    if mode == "peer_failure":
        assert result["peer_failure_category"] == "external_network_reachable"


def test_guide_workers_must_cover_both_distinct_instances():
    with pytest.raises(ValueError):
        smoke.checked_workers([
            {"hostname": "one", "pid": 1, "service": "app1", "peer_port": 12345},
            {"hostname": "two", "pid": 1, "service": "app1", "peer_port": 12346},
        ], peer=True)


def test_guide_peer_checks_use_documented_workers_and_trusted_recheck(monkeypatch, capsys):
    events = []
    workers = {"app1": [("host1", 1, 12345, 2)], "app2": [("host2", 1, 12346, 2)]}

    def fake_run(args, **_kwargs):
        if "SELECT DISTINCT" in " ".join(args):
            return "host1|1|tcp://app1:12345\nhost2|1|tcp://app2:12346"
        return ""

    def trusted(*_args):
        events.append("trusted")

    def outside(_env, _targets, _image, _project, _diagnostic, recheck):
        events.append("outside")
        recheck()

    monkeypatch.setattr(peer_probe.smoke, "run", fake_run)
    monkeypatch.setattr(peer_probe.smoke, "wait_workers", lambda *_args: workers)
    monkeypatch.setattr(peer_probe.smoke, "verify_exposure", lambda *_args: [("192.0.2.1", 12345)])
    monkeypatch.setattr(peer_probe.smoke, "verify_peer_links", trusted)
    monkeypatch.setattr(peer_probe.smoke, "verify_external_connections", outside)
    monkeypatch.setattr(sys, "argv", ["probe", "--env-file", "/tmp/test.env", "--compose-file", "/tmp/compose",
                                    "--project", "c7doc0123456789abcdef"])
    assert peer_probe.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert events == ["trusted", "outside", "trusted"]
    assert data["positive_control"] and data["external_blocked"] and data["cleanup_confirmed"]
    assert {(item["hostname"], item["pid"]) for item in data["db_workers"]} == {("host1", 1), ("host2", 1)}


def test_guide_peer_does_not_remove_preexisting_external_resources(monkeypatch, capsys):
    commands = []

    def fake_run(args, **_kwargs):
        commands.append(args)
        if args[:2] == ["docker", "ps"]:
            return "existing-container"
        return ""

    monkeypatch.setattr(peer_probe.smoke, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["probe", "--env-file", "/tmp/test.env", "--compose-file", "/tmp/compose",
                                    "--project", "c7doc0123456789abcdef"])
    assert peer_probe.main() == 1
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "invalid_state"
    assert not any(args[:2] == ["docker", "rm"] for args in commands)


@pytest.mark.parametrize("fail", [False, True], ids=["local-only", "redacted-failure"])
def test_guide_browser_blocks_nonlocal_and_never_outputs_credentials(tmp_path, monkeypatch, capsys, fail):
    env_file = tmp_path / "test.env"
    env_file.write_text("ADMIN_USERNAME=testadmin\nADMIN_PASSWORD=SECRET_VALUE\nJWT_SECRET=SECRET_VALUE\n")
    actions = []

    class Route:
        def __init__(self, url):
            self.request = SimpleNamespace(url=url)

        def continue_(self):
            actions.append("allowed")

        def abort(self):
            actions.append("blocked")

    class Locator:
        def fill(self, _value):
            pass

        def click(self):
            pass

        def wait_for(self, **_kwargs):
            pass

        def all_text_contents(self):
            return ["host1（PID 1）", "host2（PID 1）"]

    class Page:
        def goto(self, url, **_kwargs):
            assert url.startswith("http://127.0.0.1:32001/")
            if fail:
                raise ValueError("SECRET_VALUE")

        def get_by_placeholder(self, _name):
            return Locator()

        def get_by_role(self, *_args, **_kwargs):
            return Locator()

        def wait_for_url(self, *_args, **_kwargs):
            pass

        def wait_for_function(self, *_args, **_kwargs):
            pass

        def locator(self, _selector):
            return Locator()

    class Context:
        def route(self, _pattern, handler):
            handler(Route("http://127.0.0.1:32001/assets/app.js"))
            handler(Route("https://external.invalid/secret"))

        def new_page(self):
            return Page()

        def close(self):
            pass

    class Browser:
        def new_context(self, **_kwargs):
            return Context()

        def close(self):
            pass

    class Playwright:
        chromium = SimpleNamespace(launch=lambda **_kwargs: Browser())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=Playwright))
    monkeypatch.setattr(sys, "argv", ["probe", "--env-file", str(env_file), "--port", "32001"])
    assert ui_probe.main() == (1 if fail else 0)
    captured = capsys.readouterr()
    assert actions == ["allowed", "blocked"]
    assert "SECRET_VALUE" not in captured.out + captured.err
