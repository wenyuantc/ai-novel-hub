"""隔离验收环境：临时 SQLite + 生产前端包 + 本地假模型，全部绑定 127.0.0.1 的随机端口。

用法（任意 Python 3.10+ 均可运行本脚本；后端与迁移用 backend/.venv 的解释器）：
  python serve.py start [--dir DIR] [--auto-analyze]   在 DIR（缺省时新建 mktemp 目录）后台启动，就绪后打印 state.json
  python serve.py status --dir DIR                     打印 state.json 并检查两个端口是否在响应
  python serve.py stop --dir DIR                       停止该目录下的后端与假模型

运行数据（库、备份、日志、state.json、requests.jsonl）都在 DIR 内，不写入仓库。
后端工作目录设为 DIR、以 PYTHONPATH 指向 backend/，因此不会读取产品 .env。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
REPO = HARNESS.parents[4]
BACKEND = REPO / "backend"
DIST = REPO / "frontend" / "dist"
PYTHON = BACKEND / ".venv" / "bin" / "python"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fernet_key() -> str:
    import base64

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def app_env(root: Path, state: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "PYTHONHOME"))}
    env.update(
        PYTHONPATH=str(BACKEND),
        DATABASE_URL=f"sqlite+aiosqlite:///{root / 'qa.db'}",
        JWT_SECRET=state["jwt_secret"],
        ENCRYPTION_KEY=state["encryption_key"],
        BACKUP_DIR=str(root / "backups"),
        FRONTEND_DIST_DIR=str(DIST),
        REGISTRATION_ENABLED="true",
        COOKIE_SECURE="false",
        ALLOW_PRIVATE_AI_ENDPOINTS="true",
        AUTO_ANALYZE_ENABLED="true" if state["auto_analyze"] else "false",
        ADMIN_USERNAME=state["admin_username"],
        ADMIN_PASSWORD=state["admin_password"],
    )
    return env


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status < 500
    except Exception:  # noqa: BLE001
        return False


def supervise(root: Path) -> None:
    """前台监督进程：迁移 → 启动假模型与 uvicorn → 收到 SIGTERM 时停止两者。"""
    state = json.loads((root / "state.json").read_text())
    env = app_env(root, state)
    log = open(root / "serve.log", "ab", buffering=0)
    migrate = subprocess.run([str(PYTHON), "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
                             cwd=root, env=env, stdout=log, stderr=log)
    if migrate.returncode != 0:
        state["error"] = "alembic upgrade head 失败，见 serve.log"
        (root / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
        sys.exit(1)
    head = subprocess.run([str(PYTHON), "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), "current"],
                          cwd=root, env=env, capture_output=True, text=True)
    state["migration_head"] = head.stdout.strip().split(" ")[0] if head.stdout.strip() else ""
    fake = subprocess.Popen([str(PYTHON), str(HARNESS / "fake_model.py"), "--port", str(state["model_port"]),
                             "--dir", str(root)], cwd=root, env=env, stdout=log, stderr=log)
    api = subprocess.Popen([str(PYTHON), "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND),
                            "--host", "127.0.0.1", "--port", str(state["api_port"]), "--log-level", "warning"],
                           cwd=root, env=env, stdout=log, stderr=log)
    state.update(supervisor_pid=os.getpid(), api_pid=api.pid, model_pid=fake.pid)
    (root / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def shutdown(*_):
        for proc in (api, fake):
            if proc.poll() is None:
                proc.terminate()
        for proc in (api, fake):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while True:
        if api.poll() is not None or fake.poll() is not None:
            shutdown()
        time.sleep(1)


def start(args) -> None:
    root = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="ainovel-accept-"))
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "state.json").exists():
        old = json.loads((root / "state.json").read_text())
        if old.get("supervisor_pid") and alive(old["supervisor_pid"]):
            sys.exit(f"{root} 已有运行中的环境（pid {old['supervisor_pid']}），先 stop")
    if not (DIST / "index.html").exists():
        sys.exit("frontend/dist 不存在：先在 frontend/ 执行 npm run build")
    os.chmod(root, 0o700)
    state = {
        "dir": str(root),
        "api_port": free_port(),
        "model_port": free_port(),
        "jwt_secret": secrets.token_hex(32),
        "encryption_key": fernet_key(),
        "admin_username": "qa_super",
        "admin_password": "Qa-" + secrets.token_urlsafe(12),
        "auto_analyze": bool(args.auto_analyze),
        "repo": str(REPO),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state["origin"] = f"http://127.0.0.1:{state['api_port']}"
    state["model_base_url"] = f"http://127.0.0.1:{state['model_port']}/v1"
    (root / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.chmod(root / "state.json", 0o600)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_supervise", "--dir", str(root)],
                     cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    deadline = time.time() + 180
    while time.time() < deadline:
        current = json.loads((root / "state.json").read_text())
        if current.get("error"):
            sys.exit(current["error"])
        if (current.get("api_pid") and http_ok(f"{state['origin']}/api/v1/auth/config")
                and http_ok(f"{state['model_base_url']}/models")):
            print(json.dumps({k: v for k, v in current.items() if k not in ("jwt_secret", "encryption_key")},
                             ensure_ascii=False, indent=2))
            return
        time.sleep(1)
    sys.exit(f"启动超时，见 {root / 'serve.log'}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(args) -> None:
    root = Path(args.dir).resolve()
    state = json.loads((root / "state.json").read_text())
    print(json.dumps({k: v for k, v in state.items() if k not in ("jwt_secret", "encryption_key")},
                     ensure_ascii=False, indent=2))
    print("api:", "up" if http_ok(f"{state['origin']}/api/v1/auth/config") else "down")
    print("model:", "up" if http_ok(f"{state['model_base_url']}/models") else "down")


def stop(args) -> None:
    root = Path(args.dir).resolve()
    state = json.loads((root / "state.json").read_text())
    pid = state.get("supervisor_pid")
    if pid and alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not alive(pid):
                break
            time.sleep(0.5)
    for key in ("api_pid", "model_pid"):
        if state.get(key) and alive(state[key]):
            os.kill(state[key], signal.SIGTERM)
    print("stopped", root)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("--dir")
    p.add_argument("--auto-analyze", action="store_true")
    for name in ("status", "stop", "_supervise"):
        sub.add_parser(name).add_argument("--dir", required=True)
    args = parser.parse_args()
    if args.cmd == "start":
        start(args)
    elif args.cmd == "status":
        status(args)
    elif args.cmd == "stop":
        stop(args)
    else:
        supervise(Path(args.dir).resolve())


if __name__ == "__main__":
    main()
