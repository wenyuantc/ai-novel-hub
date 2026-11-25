"""Verify PostgreSQL's temporary socket-only initialization cannot satisfy Compose readiness.

The script uses a new Compose project and a fresh volume for each configuration.
It does not read the repository .env or print credentials or database logs.
"""

import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = next(
    (parent for parent in Path(__file__).resolve().parents
     if (parent / "Dockerfile").is_file() and (parent / "backend/alembic/env.py").is_file()),
    None,
)
if ROOT is None:
    raise RuntimeError("Cannot locate the project root")
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")


def run(args, env, *, check=True, timeout=120):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {args[1] if len(args) > 1 else 'docker'} ({result.returncode})")
    return result


def verify(filename, temp, env):
    project = "c7health" + secrets.token_hex(8)
    envfile = temp / "isolated.env"
    envfile.write_text(
        f"POSTGRES_PASSWORD={secrets.token_hex(16)}\n"
        f"JWT_SECRET={secrets.token_hex(32)}\n"
        "ENCRYPTION_KEY=temporary-only\n"
    )
    envfile.chmod(0o600)
    hold = temp / "hold.sh"
    hold.write_text("echo C7_INIT_BLOCKED\nwhile [ ! -f /c7-state/release ]; do sleep 1; done\n")
    hold.chmod(0o644)
    state = temp / "state"
    state.mkdir()
    override = temp / "override.json"
    override.write_text(json.dumps({
        "services": {"db": {"volumes": [
            f"{hold}:/docker-entrypoint-initdb.d/99-c7.sh:ro",
            f"{state}:/c7-state",
        ]}},
    }))
    compose = ["docker", "compose", "--env-file", str(envfile), "-p", project,
               "-f", str(ROOT / filename), "-f", str(override)]
    started = False
    try:
        config = json.loads(run([*compose, "config", "--format", "json"], env).stdout)
        shell, _ = config["services"]["db"]["healthcheck"]["test"]
        if shell != "CMD-SHELL":
            raise RuntimeError("unexpected healthcheck type")
        started = True
        run([*compose, "up", "-d", "db"], env, timeout=180)
        container = run([*compose, "ps", "--all", "--quiet", "db"], env).stdout.strip()
        shell, health = json.loads(run(
            ["docker", "inspect", "--format", "{{json .Config.Healthcheck.Test}}", container], env,
        ).stdout)
        if shell != "CMD-SHELL":
            raise RuntimeError("unexpected running healthcheck type")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            logs = run(["docker", "logs", container], env, check=False)
            if "C7_INIT_BLOCKED" in logs.stdout + logs.stderr:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("temporary initialization did not start")

        socket_query = (
            'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" '
            '-d "$POSTGRES_DB" -tAc "SELECT 1"'
        )
        local = run([*compose, "exec", "-T", "db", "sh", "-c", socket_query], env, check=False)
        tcp = run([*compose, "exec", "-T", "db", "sh", "-c", health], env, check=False)
        if local.returncode != 0 or local.stdout.strip() != "1" or tcp.returncode == 0:
            raise RuntimeError("initialization readiness did not distinguish socket and TCP")

        (state / "release").touch()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container], env).stdout.strip()
            if status == "healthy":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("database did not become healthy over TCP")
        run([*compose, "exec", "-T", "db", "sh", "-c", health], env)
        print(f"{filename}: 初始化期间 socket 可查询、TCP 检查失败；正式启动后 TCP 检查通过")
    finally:
        if started:
            down = run([*compose, "down", "--volumes", "--remove-orphans"], env, check=False, timeout=180)
            label = f"label=com.docker.compose.project={project}"
            owned = [
                run(["docker", kind, *extra, "--filter", label], env, check=False)
                for kind, extra in (("ps", ["-aq"]), ("network", ["ls", "-q"]), ("volume", ["ls", "-q"]))
            ]
            if down.returncode != 0 or any(item.returncode != 0 or item.stdout.strip() for item in owned):
                raise RuntimeError("failed to confirm test project cleanup")


def main():
    env = {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
    for filename in ("docker-compose.yml", "docker-compose.multi.yml"):
        with tempfile.TemporaryDirectory(prefix="c7-postgres-health-") as temporary:
            verify(filename, Path(temporary), env)


if __name__ == "__main__":
    main()
