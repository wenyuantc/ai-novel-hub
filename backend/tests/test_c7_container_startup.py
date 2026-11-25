"""容器启动按顺序迁移、校验进程数，并把终止信号交给 Uvicorn。"""

import socket
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker-entrypoint.sh"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def startup_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    alembic = bin_dir / "alembic"
    alembic.write_text(
        '#!/bin/sh\nprintf "alembic %s\\n" "$*" >> "$TRACE_FILE"\nexit "${MIGRATION_EXIT:-0}"\n'
    )
    uvicorn = bin_dir / "uvicorn"
    uvicorn.write_text(
        '#!/bin/sh\nprintf "uvicorn %s\\n" "$*" >> "$TRACE_FILE"\n'
        'if [ "${WAIT_FOR_TERM:-0}" = 1 ]; then\n'
        '  printf "%s\\n" "$$" > "$PID_FILE"\n'
        '  trap \'printf "TERM\\n" >> "$TRACE_FILE"; exit 0\' TERM\n'
        '  while :; do sleep 1; done\n'
        'fi\n'
    )
    alembic.chmod(0o755)
    uvicorn.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TRACE_FILE": str(tmp_path / "trace"),
        "PID_FILE": str(tmp_path / "uvicorn.pid"),
    }


@pytest.mark.parametrize(("workers", "expected"), [(None, "1"), ("4", "4")], ids=["default", "four"])
def test_migrate_before_start_with_configured_worker_count(tmp_path, startup_env, workers, expected):
    env = startup_env | ({"UVICORN_WORKERS": workers} if workers is not None else {})
    result = subprocess.run(["sh", str(ENTRYPOINT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)

    assert result.returncode == 0, result.stderr
    assert Path(env["TRACE_FILE"]).read_text().splitlines() == [
        "alembic upgrade head",
        f"uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --workers {expected}",
    ]


@pytest.mark.parametrize("workers", ["", "0", "00", "-1", "+2", "1.5", " 4", "4x", "2\n3", "9" * 100])
def test_invalid_worker_count_fails_before_migration(tmp_path, startup_env, workers):
    env = startup_env | {"UVICORN_WORKERS": workers}
    result = subprocess.run(["sh", str(ENTRYPOINT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)

    assert result.returncode != 0
    assert "UVICORN_WORKERS" in result.stderr
    assert not Path(env["TRACE_FILE"]).exists()


def test_failed_migration_never_starts_uvicorn(tmp_path, startup_env):
    env = startup_env | {"MIGRATION_EXIT": "42"}
    result = subprocess.run(["sh", str(ENTRYPOINT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)

    assert result.returncode == 42
    assert Path(env["TRACE_FILE"]).read_text().splitlines() == ["alembic upgrade head"]


def test_uvicorn_receives_signal_as_the_entrypoint_process(tmp_path, startup_env):
    env = startup_env | {"WAIT_FOR_TERM": "1"}
    process = subprocess.Popen(["sh", str(ENTRYPOINT)], cwd=tmp_path, env=env, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        pid_file = Path(env["PID_FILE"])
        while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), process.stderr.read() if process.poll() is not None else "Uvicorn 未启动"
        assert int(pid_file.read_text()) == process.pid
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert Path(env["TRACE_FILE"]).read_text().splitlines()[-1] == "TERM"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.multi.yml"])
def test_postgres_healthcheck_waits_for_tcp_query(compose, tmp_path):
    config = yaml.safe_load((ROOT / compose).read_text())
    command, healthcheck = config["services"]["db"]["healthcheck"]["test"]
    assert command == "CMD-SHELL"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    psql = bin_dir / "psql"
    psql.write_text(
        '#!/usr/bin/env python3\n'
        'import os, socket, sys\n'
        'args = sys.argv[1:]\n'
        'if ("-h" not in args or args[args.index("-h") + 1] != "127.0.0.1"\n'
        '        or "SELECT 1" not in args or os.environ.get("PGPASSWORD") != "test password"):\n'
        '    sys.exit(3)\n'
        'try:\n'
        '    with socket.create_connection(("127.0.0.1", int(os.environ["TEST_PORT"])), timeout=1):\n'
        '        pass\n'
        'except OSError:\n'
        '    sys.exit(2)\n'
    )
    psql.chmod(0o755)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "POSTGRES_USER": "test-user",
            "POSTGRES_DB": "test-db",
            "POSTGRES_PASSWORD": "test password",
            "TEST_PORT": str(server.getsockname()[1]),
        }
        # Compose 将 $$ 转成 $ 后交给容器的 shell。
        args = ["sh", "-c", healthcheck.replace("$$", "$")]
        before = subprocess.run(args, env=env, capture_output=True, timeout=5)
        assert before.returncode != 0
        server.listen()
        after = subprocess.run(args, env=env, capture_output=True, timeout=5)
        assert after.returncode == 0
