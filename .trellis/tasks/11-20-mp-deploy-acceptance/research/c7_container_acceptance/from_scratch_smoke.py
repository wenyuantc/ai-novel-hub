"""Build and exercise the deployment guide in an explicitly named isolated OrbStack machine.

Only HEAD source and explicitly listed C7 files are copied. The temporary env, database,
backup directory, Compose project and image tags are created solely for this exercise.
Raw build, application and database logs are not printed or persisted.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = next(
    (parent for parent in Path(__file__).resolve().parents
     if (parent / "Dockerfile").is_file() and (parent / "backend/alembic/env.py").is_file()),
    None,
)
if ROOT is None:
    raise RuntimeError("Cannot locate the project root")
REPORT_DIR = Path(__file__).resolve().parent

ABSENT = 'if [ -e "$1" ] || [ -L "$1" ]; then exit 1; fi'
CLAIM = (
    'set -eu; mkdir -m 0700 -- "$1"; printf "%s" "$2" > "$1/.c7-owner"; '
    'mkdir -m 0700 -- "$1/source" "$1/docker-config" "$1/tooling"'
)
VERIFY_OWNER = (
    'test -d "$1" && test ! -L "$1" && test -f "$1/.c7-owner" && '
    'test ! -L "$1/.c7-owner" && test "$(stat -c "%u:%a" -- "$1")" = "0:700" && '
    'test "$(cat -- "$1/.c7-owner")" = "$2"'
)
REMOVE_OWNER = VERIFY_OWNER + ' && rm -rf -- "$1"'


class Failure(Exception):
    def __init__(self, stage, status=None):
        self.stage = stage
        self.status = status


def invoke(args, stage, *, timeout=180, check=True):
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise Failure(stage, "timeout") from exc
    if check and result.returncode:
        raise Failure(stage, result.returncode)
    return result


def transfer(source, target, stage):
    producer = subprocess.Popen(source, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        consumer = subprocess.Popen(target, cwd=ROOT, stdin=producer.stdout,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except BaseException:
        if producer.poll() is None:
            producer.kill()
        producer.wait()
        raise
    producer.stdout.close()
    try:
        consumer.communicate(timeout=180)
        producer.wait(timeout=30)
    except BaseException as exc:
        for process in (producer, consumer):
            if process.poll() is None:
                process.kill()
        producer.wait()
        consumer.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            raise Failure(stage, "timeout") from exc
        raise
    if producer.returncode or consumer.returncode:
        raise Failure(stage, producer.returncode or consumer.returncode)


def persist_result(directory, result):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix="from-scratch-result-", suffix=".json", dir=directory)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def checked_workers(value, *, peer=False):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("invalid workers")
    result = []
    for item in value:
        if (not isinstance(item, dict) or not isinstance(item.get("hostname"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", item["hostname"])
                or type(item.get("pid")) is not int or item["pid"] < 1):
            raise ValueError("invalid worker")
        worker = {"hostname": item["hostname"], "pid": item["pid"]}
        if peer:
            if (item.get("service") not in {"app1", "app2"} or type(item.get("peer_port")) is not int
                    or not 1 <= item["peer_port"] <= 65535):
                raise ValueError("invalid peer")
            worker.update(service=item["service"], peer_port=item["peer_port"])
        result.append(worker)
    if len({(item["hostname"], item["pid"]) for item in result}) != 2:
        raise ValueError("duplicate workers")
    if peer and {item["service"] for item in result} != {"app1", "app2"}:
        raise ValueError("missing instance")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    args = parser.parse_args()
    if not re.fullmatch(r"c7accept[0-9a-f]{12}", args.machine):
        raise SystemExit("Expected the unique isolated C7 machine name")

    project = "c7doc" + secrets.token_hex(8)
    owner = secrets.token_hex(16)
    workspace = f"/tmp/{project}"
    source_dir = f"{workspace}/source"
    config_dir = f"{workspace}/docker-config"
    tooling = f"{workspace}/tooling"
    env_path = f"{source_dir}/.env"
    compose_file = f"{source_dir}/docker-compose.multi.yml"
    orb = ["orb", "run", "-m", args.machine, "-u", "root", "-w", "/"]
    docker = [*orb, "env", "-u", "DOCKER_CONTEXT", f"DOCKER_CONFIG={config_dir}",
              "DOCKER_HOST=unix:///var/run/docker.sock", "PATH=/usr/bin:/bin", "/usr/bin/docker"]
    compose = [*docker, "compose", "--env-file", env_path, "-p", project, "-f", compose_file]
    result = {"schema_version": 1, "steps": [], "http": [], "cleanup_confirmed": None, "error": None}
    started = False
    workspace_attempted = False
    uncertain = False

    def step(stage, command, *, timeout=180):
        print(f"文档验收：{stage}", flush=True)
        value = invoke(command, stage, timeout=timeout)
        result["steps"].append({"stage": stage, "exit_status": value.returncode})
        return value

    try:
        # 先检查路径不存在，再由 mkdir 原子认领；中断后只清理能证明属于本轮的目录。
        step("workspace_preflight", [*orb, "sh", "-c", ABSENT, "sh", workspace])
        workspace_attempted = True
        try:
            step("workspace", [*orb, "sh", "-c", CLAIM, "sh", workspace, owner])
            step("workspace_verify", [*orb, "sh", "-c", VERIFY_OWNER, "sh", workspace, owner])
        except BaseException:
            uncertain = True
            raise
        for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"]), ("volume", ["ls", "-q"])):
            if invoke([*docker, kind, *extra, "--filter", f"label=com.docker.compose.project={project}"],
                      "preflight").stdout.strip():
                raise Failure("preflight", "occupied")
        for suffix in ("app1", "app2"):
            if invoke([*docker, "image", "ls", "-q", "--filter", f"reference={project}-{suffix}"],
                      "preflight").stdout.strip():
                raise Failure("preflight", "occupied")
        exclude = ["--exclude=.env", "--exclude=.env.*", "--exclude=*/.env", "--exclude=*/.env.*",
                   "--exclude=*.db", "--exclude=*.db-*", "--exclude=*.sqlite", "--exclude=*.sqlite-*",
                   "--exclude=*.sqlite3", "--exclude=*.sqlite3-*", "--exclude=*.dump", "--exclude=backend/backups"]
        extractor = [*orb, "tar", *exclude, "-xf", "-", "-C", source_dir]
        try:
            transfer(["git", "archive", "--format=tar", "HEAD", "Dockerfile", ".dockerignore", "backend", "frontend"],
                     extractor, "git_archive")
            result["steps"].append({"stage": "git_archive", "exit_status": 0})
            overlay = [*orb, "tar", "-xf", "-", "-C", source_dir]
            transfer(["tar", "-cf", "-", "Dockerfile", ".dockerignore", "docker-compose.multi.yml", ".env.example",
                      "backend/alembic/env.py", "backend/docker-entrypoint.sh",
                      "backend/app/services/backups/sqlite_restore.py"], overlay, "c7_overlay")
            result["steps"].append({"stage": "c7_overlay", "exit_status": 0})
            probes = REPORT_DIR.relative_to(ROOT).as_posix()
            transfer(["tar", "-cf", "-", "backend/tests/c7_container_smoke.py",
                      "backend/tests/c7_container_diagnostics.py", f"{probes}/guide_ui_probe.py",
                      f"{probes}/guide_peer_probe.py"], [*orb, "tar", "-xf", "-", "-C", tooling], "probes")
            result["steps"].append({"stage": "probes", "exit_status": 0})
        except BaseException:
            uncertain = True
            raise

        ignore_sha = hashlib.sha256((ROOT / ".dockerignore").read_bytes()).hexdigest()
        remote_ignore = step("dockerignore_sha", [*orb, "sha256sum", f"{source_dir}/.dockerignore"])
        if remote_ignore.stdout.split()[:1] != [ignore_sha]:
            raise Failure("dockerignore_sha", "mismatch")
        result["dockerignore_sha256"] = ignore_sha

        create_env = """import base64,os,pathlib,re,secrets,shutil,socket,sys
p=pathlib.Path(sys.argv[1]); (p/'backups').mkdir()
s=[]
for _ in range(2):
 x=socket.socket(); x.bind(('127.0.0.1',0)); s.append(x)
a,b=[x.getsockname()[1] for x in s]
for x in s: x.close()
f=p/'source'/'.env'; shutil.copyfile(p/'source'/'.env.example', f); f.chmod(0o600)
value=f.read_text(encoding='utf-8')
updates={'POSTGRES_USER':'c7_probe','POSTGRES_DB':'c7_probe','POSTGRES_PASSWORD':secrets.token_hex(16),
 'JWT_SECRET':secrets.token_hex(32),'ENCRYPTION_KEY':base64.urlsafe_b64encode(os.urandom(32)).decode(),
 'ADMIN_USERNAME':'c7admin'+secrets.token_hex(4),'ADMIN_PASSWORD':secrets.token_urlsafe(24),
 'APP_PORT':'127.0.0.1:'+str(a),'APP2_PORT':'127.0.0.1:'+str(b),'BACKUP_DIR':str(p/'backups')}
for key, replacement in updates.items():
 value,count=re.subn('^'+re.escape(key)+'=.*$',lambda _:key+'='+replacement,value,flags=re.MULTILINE)
 if count!=1: raise ValueError('template field missing')
f.write_text(value, encoding='utf-8'); print(a,b)
"""
        try:
            env_result = step("test_env", [*orb, "/usr/bin/python3", "-c", create_env, workspace])
        except BaseException:
            uncertain = True
            raise
        ports = [int(value) for value in env_result.stdout.split()]
        if len(ports) != 2 or ports[0] == ports[1]:
            raise Failure("test_env", "invalid")

        step("compose_config", [*compose, "config", "--quiet"])
        started = True
        try:
            step("compose_up_build", [*compose, "up", "-d", "--build"], timeout=900)
        except BaseException:
            uncertain = True
            raise
        step("compose_ps", [*compose, "ps"])
        services = step("compose_services", [*compose, "ps", "--all", "--services"])
        if set(services.stdout.splitlines()) != {"app1", "app2", "db"}:
            raise Failure("compose_services", "invalid")
        step("compose_logs", [*compose, "logs", "--tail=100", "app1", "app2"])

        check_http = """import json,sys,time,urllib.request
result=[]
for port in sys.argv[1:]:
 for path in ('/healthz','/api/v1/auth/config'):
  ok=False
  for _ in range(180):
   try:
    with urllib.request.urlopen('http://127.0.0.1:'+port+path,timeout=2) as r:
     data=json.loads(r.read(4097)) if path.endswith('/config') else {}
     ok=(r.status==200 and (not path.endswith('/config') or
       (data.get('registration_enabled') is True and data.get('mcp_enabled') is True)))
     if ok: break
   except Exception: pass
   time.sleep(.5)
  result.append({'endpoint':'auth_config' if path.endswith('/config') else 'healthz','status':200 if ok else None})
print(json.dumps(result)); sys.exit(0 if all(item['status']==200 for item in result) else 1)
"""
        http = step("http", [*orb, "/usr/bin/python3", "-c", check_http, *map(str, ports)], timeout=390)
        result["http"] = json.loads(http.stdout)

        expected_sha = hashlib.sha256((ROOT / "backend/alembic/env.py").read_bytes()).hexdigest()
        for service in ("app1", "app2"):
            hashed = step("migration_sha", [*compose, "exec", "-T", service, "sha256sum",
                                            "/app/backend/alembic/env.py"])
            if hashed.stdout.split()[:1] != [expected_sha]:
                raise Failure("migration_sha", "mismatch")
        result["alembic_sha256"] = expected_sha
        helper_path = "backend/app/services/backups/sqlite_restore.py"
        helper_sha = hashlib.sha256((ROOT / helper_path).read_bytes()).hexdigest()
        for service in ("app1", "app2"):
            hashed = step("restore_helper_sha", [*compose, "exec", "-T", service, "sha256sum", f"/app/{helper_path}"])
            if hashed.stdout.split()[:1] != [helper_sha]:
                raise Failure("restore_helper_sha", "mismatch")
        result["sqlite_restore_sha256"] = helper_sha
        if hashlib.sha256((ROOT / ".dockerignore").read_bytes()).hexdigest() != ignore_sha:
            raise Failure("dockerignore_sha", "changed")

        ui_script = f"{tooling}/{probes}/guide_ui_probe.py"
        try:
            ui = invoke([*orb, "/opt/c7-playwright/bin/python", ui_script, "--env-file", env_path,
                         "--port", str(ports[0])], "admin_ui", timeout=120, check=False)
            ui_result = json.loads(ui.stdout)
            if (ui.returncode != 0 or not isinstance(ui_result, dict) or ui_result.get("ui_page") is not True
                    or ui_result.get("error") is not None):
                if isinstance(ui_result, dict) and ui_result.get("stage") in {
                    "credentials", "launch", "login", "settings", "workers",
                }:
                    result["ui_failure_stage"] = ui_result["stage"]
                raise Failure("admin_ui", ui.returncode)
        except BaseException:
            uncertain = True
            raise
        ui_workers = checked_workers(ui_result.get("workers"))
        result["steps"].append({"stage": "admin_ui", "exit_status": 0})

        peer_script = f"{tooling}/{probes}/guide_peer_probe.py"
        try:
            peer = invoke([*orb, "env", "-u", "DOCKER_CONTEXT", f"DOCKER_CONFIG={config_dir}",
                           "DOCKER_HOST=unix:///var/run/docker.sock", "PATH=/usr/bin:/bin", "/usr/bin/python3",
                           peer_script, "--env-file", env_path, "--compose-file", compose_file, "--project", project],
                          "peer_checks", timeout=240, check=False)
            peer_result = json.loads(peer.stdout)
            if (peer.returncode != 0 or not isinstance(peer_result, dict)
                    or peer_result.get("error") is not None
                    or peer_result.get("cleanup_confirmed") is not True
                    or peer_result.get("positive_control") is not True
                    or peer_result.get("trusted_before_after") is not True
                    or peer_result.get("external_blocked") is not True):
                if isinstance(peer_result, dict) and peer_result.get("stage") in {
                    "preflight", "workers", "query", "connectivity",
                }:
                    result["peer_failure_stage"] = peer_result["stage"]
                if isinstance(peer_result, dict) and peer_result.get("error") in {
                    "external_network_reachable", "external_network_unavailable", "worker_registration",
                    "invalid_state", "command_failed", "probe_failed", "cleanup_unconfirmed",
                }:
                    result["peer_failure_category"] = peer_result["error"]
                raise Failure("peer_checks", peer.returncode)
        except BaseException:
            uncertain = True
            raise
        peer_workers = checked_workers(peer_result.get("db_workers"), peer=True)
        if {(item["hostname"], item["pid"]) for item in peer_workers} != {
            (item["hostname"], item["pid"]) for item in ui_workers
        }:
            raise Failure("admin_peer_match", "mismatch")
        result["ui_workers"] = ui_workers
        result["peer_workers"] = peer_workers
        result["peer_checks"] = {"trusted_before_after": True, "positive_control": True,
                                 "external_blocked": True, "cleanup_confirmed": True}
        result["steps"].append({"stage": "peer_checks", "exit_status": 0})
        if hashlib.sha256((ROOT / helper_path).read_bytes()).hexdigest() != helper_sha:
            raise Failure("restore_helper_sha", "changed")
    except Failure as exc:
        result["error"] = {"stage": exc.stage, "status": exc.status}
    except KeyboardInterrupt:
        uncertain = True
        result["error"] = {"stage": "interrupted", "status": "KeyboardInterrupt"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result["error"] = {"stage": "unexpected", "status": type(exc).__name__}
    finally:
        cleanup_ok = True
        if started:
            try:
                invoke([*compose, "down", "--volumes", "--remove-orphans"], "cleanup", timeout=180)
                for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"]), ("volume", ["ls", "-q"])):
                    if invoke([*docker, kind, *extra, "--filter", f"label=com.docker.compose.project={project}"],
                              "cleanup").stdout.strip():
                        cleanup_ok = False
                for suffix in ("app1", "app2"):
                    tag = f"{project}-{suffix}"
                    if invoke([*docker, "image", "ls", "-q", "--filter", f"reference={tag}"],
                              "cleanup").stdout.strip():
                        invoke([*docker, "image", "rm", tag], "cleanup")
                    if invoke([*docker, "image", "ls", "-q", "--filter", f"reference={tag}"],
                              "cleanup").stdout.strip():
                        cleanup_ok = False
            except (Failure, OSError):
                cleanup_ok = False
        if workspace_attempted:
            try:
                owner_status = invoke([*orb, "sh", "-c", VERIFY_OWNER, "sh", workspace, owner],
                                      "workspace_ownership", check=False)
                if owner_status.returncode == 0:
                    invoke([*orb, "sh", "-c", REMOVE_OWNER, "sh", workspace, owner], "workspace_cleanup")
                    invoke([*orb, "sh", "-c", ABSENT, "sh", workspace], "workspace_absent")
                else:
                    cleanup_ok = False
            except (Failure, OSError):
                cleanup_ok = False
        result["cleanup_confirmed"] = cleanup_ok and not uncertain
        if workspace_attempted and not result["cleanup_confirmed"]:
            result["workspace"] = {"machine": args.machine, "path": workspace}
        try:
            report = persist_result(args.report_dir, result)
        except OSError:
            print("文档验收结果无法保存", file=sys.stderr)
            return 1

    print(f"文档验收记录：{report}", flush=True)
    if workspace_attempted and not result["cleanup_confirmed"]:
        print(f"临时目录清理未确认，请核对 {args.machine}:{workspace}", file=sys.stderr)
    if result["error"] or not result["cleanup_confirmed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
