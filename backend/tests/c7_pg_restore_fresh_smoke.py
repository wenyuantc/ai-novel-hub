"""使用隔离 PostgreSQL 和虚构数据验证恢复到全新数据库。"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")
USER = "c7_restore"
SOURCE = "c7_source"
SEED = """
CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY);
INSERT INTO alembic_version VALUES ('0070');
CREATE TABLE users (id integer PRIMARY KEY, username text NOT NULL, password_hash text NOT NULL);
INSERT INTO users VALUES (1, 'synthetic-user', 'synthetic-hash');
CREATE TABLE projects (id integer PRIMARY KEY, user_id integer REFERENCES users(id), title text NOT NULL);
INSERT INTO projects VALUES (1, 1, 'before-snapshot');
CREATE TABLE zz_collision (value text);
INSERT INTO zz_collision VALUES ('synthetic');
"""
STATE = """
SELECT version_num || '|' || users.username || '|' || projects.title || '|' ||
       COALESCE(to_regclass('public.source_only_unbacked')::text, '')
FROM alembic_version, users JOIN projects ON projects.user_id = users.id;
"""


def command(args, *, env, input_data=None, timeout=120, check=True):
    result = subprocess.run(args, cwd=ROOT, env=env, input=input_data, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"test command failed: {result.returncode}")
    return result


def main():
    env = {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
    run_id = secrets.token_hex(8)
    name = f"c7restore{run_id}"
    label = f"ainovel.c7.restore={run_id}"
    filters = (["docker", "ps", "-aq", "--filter", f"label={label}"],
               ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"])
    for args in filters:
        if command(args, env=env).stdout.strip():
            raise RuntimeError("test container already exists")

    attempted = False
    uncertain = False
    try:
        with tempfile.TemporaryDirectory(prefix="c7-fresh-restore-") as temporary:
            directory = Path(temporary)
            envfile = directory / "postgres.env"
            envfile.write_text(f"POSTGRES_USER={USER}\nPOSTGRES_DB=postgres\nPOSTGRES_PASSWORD={secrets.token_hex(24)}\n")
            envfile.chmod(0o600)
            attempted = True
            try:
                started = command(["docker", "run", "-d", "--pull=never", "--name", name, "--label", label,
                                   "--network", "none", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
                                   "--env-file", str(envfile), "postgres:16-alpine"], env=env, timeout=120)
            except BaseException:
                uncertain = True
                raise
            container = started.stdout.decode().strip()
            inspect = command(["docker", "inspect", "--format",
                               "{{json .HostConfig.NetworkMode}}|{{json .HostConfig.Tmpfs}}|{{json .Mounts}}",
                               container], env=env).stdout.decode().strip().split("|", 2)
            network, tmpfs, mounts = (json.loads(value) for value in inspect)
            if (network != "none" or "/var/lib/postgresql/data" not in (tmpfs or {})
                    or any(mount.get("Type") == "volume" for mount in mounts)):
                raise RuntimeError("isolated PostgreSQL storage or network differs")

            def pg(program, *args, database=None, input_data=None, check=True, timeout=120):
                actual = [program, "-h", "127.0.0.1", "-U", USER]
                if database is not None:
                    actual += ["-d", database]
                actual += list(args)
                return command(["docker", "exec", "-i", container, "sh", "-c",
                                'PGPASSWORD="$POSTGRES_PASSWORD" exec "$@"', "sh", *actual],
                               env=env, input_data=input_data, timeout=timeout, check=check)

            def sql(database, statement):
                return pg("psql", "-v", "ON_ERROR_STOP=1", "-At", "-c", statement,
                          database=database).stdout.decode().strip()

            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if pg("psql", "-At", "-c", "SELECT 1", database="postgres", check=False, timeout=5).returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("isolated PostgreSQL was not ready")

            pg("createdb", "--maintenance-db=postgres", "--template=template0", "--", SOURCE)
            sql(SOURCE, SEED)
            active = sql(SOURCE, "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                         "AND pid <> pg_backend_pid()")
            if active != "0":
                raise RuntimeError("test database has unexpected connections")
            backup = pg("pg_dump", "--format=custom", "--no-owner", "--no-privileges", SOURCE).stdout
            snapshot = directory / "synthetic.dump"
            descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(backup)
            command(["docker", "cp", str(snapshot), f"{container}:/tmp/c7-synthetic.dump"], env=env)
            sql(SOURCE, "UPDATE projects SET title='after-snapshot'; "
                "CREATE TABLE source_only_unbacked (value text); INSERT INTO source_only_unbacked VALUES ('new');")
            source_state = sql(SOURCE, STATE)
            if source_state != "0070|synthetic-user|after-snapshot|source_only_unbacked":
                raise RuntimeError("synthetic source setup failed")

            calls = []

            def fresh_restore(target, *, collision=False):
                create = pg("createdb", "--maintenance-db=postgres", "--template=template0", "--", target,
                            check=False)
                if create.returncode:
                    return create.returncode, None
                if collision:
                    sql(target, "CREATE TABLE zz_collision (value text)")
                calls.append(target)
                restored = pg("pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
                              "/tmp/c7-synthetic.dump", database=target, check=False)
                return 0, restored.returncode

            created, restored = fresh_restore("c7_restored")
            if (created, restored) != (0, 0):
                raise RuntimeError("fresh restore did not succeed")
            if sql("c7_restored", STATE) != "0070|synthetic-user|before-snapshot|":
                raise RuntimeError("restored version or synthetic business data differs")
            if sql(SOURCE, STATE) != source_state:
                raise RuntimeError("source changed during successful restore")

            pg("createdb", "--maintenance-db=postgres", "--template=template0", "--", "c7_existing")
            sql("c7_existing", "CREATE TABLE keeper (value text); INSERT INTO keeper VALUES ('untouched')")
            prior = len(calls)
            created, restored = fresh_restore("c7_existing")
            if created == 0 or restored is not None or len(calls) != prior:
                raise RuntimeError("existing destination was not rejected")
            if sql("c7_existing", "SELECT value FROM keeper") != "untouched" or sql(SOURCE, STATE) != source_state:
                raise RuntimeError("existing destination or source changed")

            created, restored = fresh_restore("c7_failed", collision=True)
            if created != 0 or restored == 0 or restored is None:
                raise RuntimeError("injected restore failure was not observed")
            if sql("c7_failed", "SELECT (to_regclass('public.alembic_version') IS NOT NULL)::text") != "true":
                raise RuntimeError("failure did not occur after DDL")
            if sql(SOURCE, STATE) != source_state:
                raise RuntimeError("source changed during failed restore")
            print("新库恢复成功：0070 与虚构业务数据匹配，快照外对象不进入新库，原库保持不变")
            print("目标已存在被拒绝；恢复在 DDL 后失败时退出非零，原库和已有目标均未改动")
    finally:
        if attempted:
            try:
                for container in command(filters[0], env=env).stdout.decode().splitlines():
                    command(["docker", "rm", "-f", "-v", container], env=env)
                if any(command(args, env=env).stdout.strip() for args in filters):
                    uncertain = True
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                uncertain = True
            if uncertain:
                print(f"测试容器清理未确认，请核对 {name} 和 {label}", file=sys.stderr)
                raise RuntimeError("cleanup unconfirmed") from None


if __name__ == "__main__":
    main()
