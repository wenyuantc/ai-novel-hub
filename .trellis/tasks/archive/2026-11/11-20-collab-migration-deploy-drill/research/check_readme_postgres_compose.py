"""在专属 Compose 项目实跑 README 的 PostgreSQL 快照命令。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from tempfile import mkdtemp

from check_readme_commands import repository, shell_blocks


def invoke(
    arguments: list[str], *, cwd: Path, env: dict[str, str], data: bytes | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(arguments, cwd=cwd, env=env, input=data, capture_output=True, check=False, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"命令失败：{arguments[0]}，退出码 {result.returncode}")
    return result


def main() -> None:
    document = (repository() / "README.md").read_bytes()
    command = next(block for block in shell_blocks(document.decode()) if "ainovel-before-collaboration.dump" in block)
    project = f"ainovel-c8-readme-{uuid.uuid4().hex[:12]}"
    env = {
        "PATH": os.environ["PATH"],
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "COMPOSE_PROJECT_NAME": project,
    }
    root = Path(mkdtemp(prefix="ainovel-c8-compose-"))
    try:
        (root / "compose.yaml").write_text(
            """services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: c8test
      POSTGRES_PASSWORD: isolated-c8-test-password
      POSTGRES_DB: c8source
    tmpfs:
      - /var/lib/postgresql/data
    healthcheck:
      test:
        - CMD-SHELL
        - >-
          PGPASSWORD=$$POSTGRES_PASSWORD psql -h 127.0.0.1 -U $$POSTGRES_USER
          -d $$POSTGRES_DB -tAc 'SELECT 1'
      interval: 1s
      timeout: 3s
      retries: 60
  app:
    image: postgres:16-alpine
    entrypoint: ["sh", "-c", "sleep 600"]
    tmpfs:
      - /var/lib/postgresql/data
"""
        )
        compose = ["docker", "compose", "--env-file", "/dev/null"]
        try:
            invoke(compose + ["up", "-d", "--wait", "--pull", "never", "db"], cwd=root, env=env, timeout=150)
            invoke(compose + ["up", "-d", "--pull", "never", "app"], cwd=root, env=env)
            container = invoke(compose + ["ps", "-q", "db"], cwd=root, env=env).stdout.decode().strip()
            assert container
            label = (
                invoke(
                    ["docker", "inspect", "-f", '{{ index .Config.Labels "com.docker.compose.project" }}', container],
                    cwd=root,
                    env=env,
                )
                .stdout.decode()
                .strip()
            )
            assert label == project
            image_id = (
                invoke(["docker", "inspect", "-f", "{{.Image}}", container], cwd=root, env=env).stdout.decode().strip()
            )
            assert image_id.startswith("sha256:")
            sql = (
                "CREATE TABLE alembic_version(version_num text NOT NULL);"
                "INSERT INTO alembic_version VALUES ('0043');"
                "CREATE TABLE c8_marker(value text NOT NULL);"
                "INSERT INTO c8_marker VALUES ('isolated-fixture');"
            )
            psql = (
                'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -U "$POSTGRES_USER" '
                '-d "$1" -v ON_ERROR_STOP=1 -tAc "$2"'
            )
            invoke(compose + ["exec", "-T", "db", "sh", "-c", psql, "sh", "c8source", sql], cwd=root, env=env)
            result = subprocess.run(
                ["bash", "-eu"],
                cwd=root,
                env=env,
                input=command,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            assert result.returncode == 0 and "最终快照已生成" in result.stdout
            snapshot = root / "backups/ainovel-before-collaboration.dump"
            assert snapshot.stat().st_size > 0
            running = invoke(compose + ["ps", "--status", "running", "--services"], cwd=root, env=env).stdout.decode()
            assert running.splitlines() == ["db"]
            create = (
                'PGPASSWORD="$POSTGRES_PASSWORD" exec createdb -h 127.0.0.1 -U "$POSTGRES_USER" '
                '--maintenance-db=postgres --template=template0 -- "$1"'
            )
            restore = (
                'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" '
                '--exit-on-error --no-owner --no-privileges --dbname="$1"'
            )
            invoke(compose + ["exec", "-T", "db", "sh", "-c", create, "sh", "c8restore"], cwd=root, env=env)
            invoke(
                compose + ["exec", "-T", "db", "sh", "-c", restore, "sh", "c8restore"],
                cwd=root,
                env=env,
                data=snapshot.read_bytes(),
            )
            query = "SELECT version_num || ':' || (SELECT value FROM c8_marker) FROM alembic_version"
            observed = (
                invoke(compose + ["exec", "-T", "db", "sh", "-c", psql, "sh", "c8restore", query], cwd=root, env=env)
                .stdout.decode()
                .strip()
            )
            assert observed == "0043:isolated-fixture"
            outcome = {
                "readme_sha256": hashlib.sha256(document).hexdigest(),
                "postgres_image_id": image_id,
                "snapshot_bytes": snapshot.stat().st_size,
                "application_stopped": True,
                "restored_revision": "0043",
                "restored_fixture": True,
            }
        finally:
            try:
                invoke(compose + ["down", "-v", "--remove-orphans"], cwd=root, env=env, timeout=120)
                assert not invoke(compose + ["ps", "-aq"], cwd=root, env=env).stdout.strip()
            except BaseException:
                print(f"清理失败，专属 Compose 项目为 {project}，配置保留在 {root}", flush=True)
                raise
            else:
                shutil.rmtree(root)
    except BaseException:
        raise
    else:
        print(json.dumps(outcome, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
