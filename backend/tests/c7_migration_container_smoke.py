"""隔离验收三个容器竞争迁移，以及历史 DDL 中途失败后的事务回滚。

先构建本次检出的镜像，再执行：
    python tests/c7_migration_container_smoke.py --image <本次镜像标签>
运行需要 Docker 与 postgres:16-alpine；不会读取项目 .env 或使用已有数据库。
"""

import argparse
import base64
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from c7_container_diagnostics import DEFAULT_REPORT_DIR, Diagnostics, ProbeFailure, checked_run, wait_ready

ROOT = Path(__file__).resolve().parents[2]
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
FAILURE = "C7 injected migration failure"
SEED = (
    "INSERT INTO users(id,username,password_hash) VALUES (1,'c7-legacy','hash');"
    "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
    "VALUES (1,1,'c7-legacy-project','','','','planning');"
)


def run(args, *, env, timeout=120, check=True):
    return checked_run(args, cwd=ROOT, env=env, timeout=timeout, check=check)


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def write_env(path, entries):
    path.write_text("".join(f"{key}={value}\n" for key, value in entries.items()))
    path.chmod(0o600)


def create_container(args, *, env):
    return run(["docker", "run", "-d", *args], env=env)


def create_three(args, *, env):
    processes = []
    containers = []
    errors = []
    try:
        for arguments in args:
            processes.append(subprocess.Popen(
                ["docker", "run", "-d", *arguments], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            ))
        for process in processes:
            stdout, _stderr = process.communicate(timeout=120)
            if process.returncode:
                errors.append(process.returncode)
            else:
                containers.append(stdout.strip())
    except BaseException:
        # 必须先停止并等待所有 Docker CLI，之后外层才能按标签清理它们创建的容器。
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            if process.poll() is None:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)
        raise
    if errors:
        for container in containers:
            run(["docker", "rm", "-f", container], env=env, check=False)
        raise ProbeFailure("container_start_failed", exit_status=errors[0])
    return containers


def remove_containers(containers, env):
    for container in containers:
        run(["docker", "rm", "-f", container], env=env)


def _private_file(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def preserve_databases(directory, pg, env, diagnostic):
    """仅保存本脚本创建的两个 SQLite 文件及两个 PostgreSQL 数据库。"""
    sqlite_files = [(name, directory / "sqlite" / f"{name}.db") for name in ("empty", "old")]
    sqlite_files = [(name, path) for name, path in sqlite_files if path.is_file()]
    databases = []
    if pg:
        try:
            sql = "SELECT datname FROM pg_database WHERE datname IN ('c7_empty','c7_old') ORDER BY datname"
            names = pg_sql(pg, "postgres", sql, env).splitlines()
            databases = [name for name in names if name in {"c7_empty", "c7_old"}]
        except (OSError, ProbeFailure, subprocess.TimeoutExpired):
            diagnostic.snapshot_failed = True
    for name, path in sqlite_files:
        filename = f"sqlite-{name}.sqlite3"
        destination = diagnostic.snapshot_dir / filename
        try:
            descriptor = _private_file(destination)
            os.close(descriptor)
            with (
                sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as source,
                sqlite3.connect(destination) as target,
            ):
                source.backup(target)
            diagnostic.record_snapshot("sqlite", filename)
        except (OSError, sqlite3.Error):
            diagnostic.snapshot_failed = True
    for database in databases:
        filename = f"pg-{database}.dump"
        destination = diagnostic.snapshot_dir / filename
        try:
            dump = subprocess.run(["docker", "exec", pg, "pg_dump", "-U", "c7_probe", "--format=custom", database],
                                  cwd=ROOT, env=env, capture_output=True, timeout=120)
            if dump.returncode:
                diagnostic.snapshot_failed = True
                continue
            descriptor = _private_file(destination)
            with os.fdopen(descriptor, "wb") as output:
                output.write(dump.stdout)
            diagnostic.record_snapshot("postgresql", filename)
        except (OSError, subprocess.TimeoutExpired):
            diagnostic.snapshot_failed = True


@contextmanager
def isolated_directory(pg_getter, env, diagnostic):
    with tempfile.TemporaryDirectory(prefix="ainovel-c7-migrate-") as temporary:
        directory = Path(temporary)
        try:
            yield directory
        except BaseException:
            try:
                preserve_databases(directory, pg_getter(), env, diagnostic)
            except Exception:
                diagnostic.snapshot_failed = True
            raise


def assert_http_closed(ports, diagnostic):
    for index, port in enumerate(ports):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                diagnostic.record_http(index, "healthz", response.status, "ok")
                raise ProbeFailure("unexpected_http")
        except urllib.error.HTTPError as exc:
            diagnostic.record_http(index, "healthz", exc.code, "http_error")
            raise ProbeFailure("unexpected_http") from None
        except (OSError, urllib.error.URLError):
            diagnostic.record_http(index, "healthz", None, "connection_error")


def wait_without_http(containers, ports, env, diagnostic):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        assert_http_closed(ports, diagnostic)
        states = run(["docker", "inspect", "--format", "{{.State.Running}}", *containers], env=env).splitlines()
        if len(states) != len(containers):
            raise ProbeFailure("invalid_state")
        if all(state == "false" for state in states):
            return
        time.sleep(0.2)
    raise ProbeFailure("timeout")


def schema_sqlite(path):
    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] if any(
            row[1] == "alembic_version" for row in schema
        ) else ""
        seed = connection.execute(
            "SELECT users.username,users.password_hash,projects.title,projects.genre,projects.premise,projects.status "
            "FROM users JOIN projects ON projects.user_id=users.id ORDER BY users.id"
        ).fetchall() if any(
            row[1] == "users" for row in schema
        ) else []
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ProbeFailure("schema_mismatch")
        return schema, version, seed, connection.execute("PRAGMA foreign_key_check").fetchall()


def schema_pg(database, pg, env):
    result = run(["docker", "exec", pg, "pg_dump", "-U", "c7_probe", "--schema-only", "--no-owner",
                  "--no-privileges", database], env=env)
    return "\n".join(line for line in result.splitlines() if not line.startswith(("\\restrict ", "\\unrestrict ")))


def pg_sql(pg, database, sql, env):
    return run(["docker", "exec", pg, "psql", "-U", "c7_probe", "-d", database, "-v", "ON_ERROR_STOP=1",
                "-At", "-c", sql], env=env)


def capture_exits(containers, env, diagnostic):
    for index, container in enumerate(containers):
        try:
            value = run(["docker", "inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", container],
                        env=env, check=False)
            status, code = value.split("|", 1)
            diagnostic.record_exit(index, int(code) if status in {"exited", "dead"} else None, status)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue


def cleanup_scenario(containers, env, diagnostic, phase):
    had_error = sys.exc_info()[0] is not None
    capture_exits(containers, env, diagnostic)
    for index, container in enumerate(containers):
        try:
            logs = subprocess.run(["docker", "logs", "--tail", "1000", container], cwd=ROOT, env=env,
                                  capture_output=True, text=True, timeout=30)
            diagnostic.record_migration_log(index, phase, logs.stdout + logs.stderr if logs.returncode == 0 else None)
        except (OSError, subprocess.TimeoutExpired):
            diagnostic.record_migration_log(index, phase, None)
    try:
        remove_containers(containers, env)
    except (OSError, ProbeFailure, subprocess.TimeoutExpired):
        diagnostic.cleanup_confirmed = False
        if not had_error:
            diagnostic.set_stage("cleanup")
            raise ProbeFailure("cleanup_unconfirmed") from None


def cleanup_owned(run_id, env):
    """所有 CLI 结束后回收本次标签，移除网络，再次确认没有迟到容器。"""
    label = f"label=ainovel.c7={run_id}"
    for _ in range(12):
        try:
            owned = run(["docker", "ps", "-aq", "--filter", label], env=env).splitlines()
            remove_containers(owned, env)
            networks = run(["docker", "network", "ls", "-q", "--filter", label], env=env).splitlines()
            for network in networks:
                run(["docker", "network", "rm", network], env=env)
            if not run(["docker", "ps", "-aq", "--filter", label], env=env) and not run(
                ["docker", "network", "ls", "-q", "--filter", label], env=env,
            ):
                time.sleep(0.25)
                if not run(["docker", "ps", "-aq", "--filter", label], env=env) and not run(
                    ["docker", "network", "ls", "-q", "--filter", label], env=env,
                ):
                    return
        except (OSError, ProbeFailure, subprocess.TimeoutExpired):
            pass
        time.sleep(0.25)
    raise ProbeFailure("cleanup_unconfirmed")


def exercise(*, image, env, network, directory, run_id, database_url, mounts, fault, state, expected_versions,
             diagnostic):
    envfile = directory / f"{run_id}.env"
    write_env(envfile, {
        "DATABASE_URL": database_url,
        "JWT_SECRET": secrets.token_hex(32),
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "TASK_PEER_ENABLED": "false",
    })
    diagnostic.set_stage("fault_migration")
    before = state()
    ports = free_ports(3)
    common = [
        "--network", network, "--env-file", str(envfile), "--label", f"ainovel.c7={run_id.split('-')[0]}", *mounts,
    ]
    fault_mount = [
        "--mount", f"type=bind,src={fault},dst=/app/backend/alembic/versions/0037_setting_ai_visibility.py,readonly",
    ]
    failed_args = [
        ["--name", f"{run_id}-fail-{index}", *common, *fault_mount, "-p", f"127.0.0.1:{port}:8000", image]
        for index, port in enumerate(ports)
    ]
    try:
        failed = create_three(failed_args, env=env)
    except BaseException:
        diagnostic.cleanup_uncertain = True
        raise
    try:
        wait_without_http(failed, ports, env, diagnostic)
        for index, container in enumerate(failed):
            code = run(["docker", "wait", container], env=env, timeout=180)
            diagnostic.record_exit(index, int(code))
            # Docker logs 把异常输出到 stderr，单独读取以确定每个实例确实走到注入点。
            if code == "0":
                raise ProbeFailure("migration_failure", exit_status=0)
            detail = subprocess.run(["docker", "logs", container], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=30)
            if FAILURE not in detail.stderr + detail.stdout:
                raise ProbeFailure("migration_failure")
        assert_http_closed(ports, diagnostic)
        diagnostic.set_stage("rollback_check")
        if state() != before:
            raise ProbeFailure("schema_mismatch")
        print(f"{run_id}：3 个容器在真实 DDL 后均失败，HTTP 不可达，数据库状态不变")
    finally:
        cleanup_scenario(failed, env, diagnostic, "fault")

    diagnostic.set_stage("retry_migration")
    success_args = [
        ["--name", f"{run_id}-success-{index}", *common, "-p", f"127.0.0.1:{port}:8000", image]
        for index, port in enumerate(ports)
    ]
    try:
        successful = create_three(success_args, env=env)
    except BaseException:
        diagnostic.cleanup_uncertain = True
        raise
    try:
        diagnostic.set_stage("readiness")
        wait_ready(ports, diagnostic)
        diagnostic.set_stage("success_verify")
        counts = []
        for container in successful:
            detail = subprocess.run(["docker", "logs", container], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=30)
            counts.append((detail.stdout + detail.stderr).count("Running upgrade"))
        if sorted(counts) != [0, 0, expected_versions]:
            raise ProbeFailure("migration_count")
        after = state()
        if after[1] != "0070" or after[2] != before[2] or (len(after) > 3 and after[3]):
            raise ProbeFailure("schema_mismatch")
        print(f"{run_id}：干净重试后 3 个实例就绪，迁移执行次数={counts}")
    finally:
        cleanup_scenario(successful, env, diagnostic, "retry")


def execute(args, diagnostic):
    env = {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
    run_id = "c7mig" + secrets.token_hex(8)
    label = f"label=ainovel.c7={run_id}"
    for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"])):
        if run(["docker", kind, *extra, "--filter", label], env=env):
            raise ProbeFailure("invalid_state")
    pg = None
    cleaned = False
    try:
        diagnostic.set_stage("network_create")
        try:
            network = run(["docker", "network", "create", "--label", f"ainovel.c7={run_id}", run_id], env=env)
        except BaseException:
            diagnostic.cleanup_uncertain = True
            raise
        with isolated_directory(lambda: pg, env, diagnostic) as directory:
            original = (ROOT / "backend/alembic/versions/0037_setting_ai_visibility.py").read_text()
            needle = '        op.add_column(table, sa.Column("ai_mode", sa.String(16), nullable=True))\n'
            if original.count(needle) != 1:
                raise ProbeFailure("invalid_state")
            fault = directory / "0037_fault.py"
            fault.write_text(original.replace(needle, needle + f'        raise RuntimeError("{FAILURE}")\n'))
            password = secrets.token_hex(16)
            pg_env = directory / "postgres.env"
            write_env(pg_env, {"POSTGRES_USER": "c7_probe", "POSTGRES_PASSWORD": password, "POSTGRES_DB": "postgres"})
            try:
                pg = create_container(["--name", f"{run_id}-db", "--network", network, "--network-alias", "c7db",
                                       "--label", f"ainovel.c7={run_id}", "--env-file", str(pg_env),
                                       "postgres:16-alpine"], env=env)
            except BaseException:
                diagnostic.cleanup_uncertain = True
                raise
            diagnostic.set_stage("database_ready")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                ready = subprocess.run(
                    ["docker", "exec", pg, "sh", "-c",
                     'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U c7_probe -d postgres -At -c "SELECT 1"'],
                    cwd=ROOT, env=env, capture_output=True, timeout=5,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise ProbeFailure("timeout")

            for target in ("empty", "old"):
                diagnostic.set_scenario(f"pg_{target}")
                diagnostic.set_stage("legacy_seed")
                database = f"c7_{target}"
                run(["docker", "exec", pg, "createdb", "-U", "c7_probe", database], env=env)
                url = f"postgresql+asyncpg://c7_probe:{password}@c7db:5432/{database}"
                envfile = directory / "pg-seed.env"
                write_env(envfile, {"DATABASE_URL": url})
                if target == "old":
                    try:
                        run(["docker", "run", "--rm", "--label", f"ainovel.c7={run_id}",
                             "--network", network, "--env-file", str(envfile),
                             "--entrypoint", "alembic", args.image, "upgrade", "0034"], env=env, timeout=180)
                    except BaseException:
                        diagnostic.cleanup_uncertain = True
                        raise
                    pg_sql(pg, database, SEED, env)
                def pg_state(database=database, target=target):
                    schema = schema_pg(database, pg, env)
                    exists = pg_sql(pg, database, "SELECT to_regclass('public.alembic_version')", env)
                    version = pg_sql(pg, database, "SELECT version_num FROM alembic_version", env) if exists else ""
                    data = pg_sql(
                        pg, database,
                        "SELECT users.username || ':' || users.password_hash || ':' || projects.title || ':' || "
                        "projects.genre || ':' || projects.premise || ':' || projects.status FROM users "
                        "JOIN projects ON projects.user_id=users.id ORDER BY users.id", env,
                    ) if target == "old" else ""
                    return schema, version, data

                exercise(
                    image=args.image, env=env, network=network, directory=directory, run_id=f"{run_id}-pg-{target}",
                    database_url=url, mounts=[], fault=fault, state=pg_state,
                    expected_versions=70 if target == "empty" else 36, diagnostic=diagnostic,
                )

            db_directory = directory / "sqlite"
            db_directory.mkdir()
            mount = ["--mount", f"type=bind,src={db_directory},dst=/data"]
            for target in ("empty", "old"):
                diagnostic.set_scenario(f"sqlite_{target}")
                diagnostic.set_stage("legacy_seed")
                db_file = db_directory / f"{target}.db"
                url = f"sqlite+aiosqlite:////data/{target}.db"
                envfile = directory / "sqlite-seed.env"
                write_env(envfile, {"DATABASE_URL": url})
                if target == "old":
                    try:
                        run(["docker", "run", "--rm", "--label", f"ainovel.c7={run_id}", *mount,
                             "--env-file", str(envfile), "--entrypoint", "alembic",
                             args.image, "upgrade", "0034"], env=env, timeout=180)
                    except BaseException:
                        diagnostic.cleanup_uncertain = True
                        raise
                    with sqlite3.connect(db_file) as connection:
                        connection.executescript(SEED)
                exercise(image=args.image, env=env, network=network, directory=directory,
                         run_id=f"{run_id}-sqlite-{target}", database_url=url, mounts=mount, fault=fault,
                         state=lambda db_file=db_file: schema_sqlite(db_file),
                         expected_versions=70 if target == "empty" else 36, diagnostic=diagnostic)
            try:
                cleanup_owned(run_id, env)
                diagnostic.cleanup_confirmed = not diagnostic.cleanup_uncertain
                cleaned = True
            except (OSError, ProbeFailure, subprocess.TimeoutExpired):
                diagnostic.cleanup_confirmed = False
                diagnostic.set_stage("cleanup")
                raise ProbeFailure("cleanup_unconfirmed") from None
    finally:
        if not cleaned:
            had_error = sys.exc_info()[0] is not None
            if isinstance(sys.exc_info()[1], (subprocess.TimeoutExpired, KeyboardInterrupt)):
                diagnostic.cleanup_uncertain = True
            try:
                cleanup_owned(run_id, env)
                diagnostic.cleanup_confirmed = not diagnostic.cleanup_uncertain
            except (OSError, ProbeFailure, subprocess.TimeoutExpired):
                diagnostic.cleanup_confirmed = False
                if not had_error:
                    diagnostic.set_stage("cleanup")
                    raise ProbeFailure("cleanup_unconfirmed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="本次检出的已构建镜像")
    parser.add_argument("--diagnostics-dir", type=Path, default=DEFAULT_REPORT_DIR, help="失败诊断目录")
    args = parser.parse_args()
    diagnostic = Diagnostics("migration", args.diagnostics_dir)
    try:
        execute(args, diagnostic)
    except BaseException as exc:
        try:
            report = diagnostic.persist(exc)
            print(f"容器迁移验收失败，结构化诊断：{report}", file=sys.stderr)
            if diagnostic.snapshots:
                print(f"隔离测试库快照：{diagnostic.snapshot_dir}；清理时删除该目录和诊断文件。", file=sys.stderr)
            if diagnostic.snapshot_failed:
                print("部分隔离测试库快照无法保存，请参考结构化诊断。", file=sys.stderr)
        except OSError:
            print("容器迁移验收失败，无法保存结构化诊断", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
