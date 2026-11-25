"""为 C8 独立复核夹具准备固定新版真实前端；不访问数据库。"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

import c8_drill as c8
import fixture_backend
import fixture_process
import fixture_supervisor

REPO = next(parent for parent in Path(__file__).resolve().parents if parent.name == ".trellis").parent
NEW = "b7b3d248a7462deedd0507275a0e6fd0a6ec8e0e"


def main(metadata: Path) -> None:
    metadata = metadata.resolve(strict=True)
    root = metadata.parent
    if not root.name.startswith("ainovel-c8-drill-") or metadata.name != "fixture.json":
        raise ValueError("不是 C8 独立夹具")
    info = json.loads(metadata.read_text(encoding="utf-8"))
    if Path(info["root"]).resolve(strict=True) != root or info["shape"] != "sqlite":
        raise ValueError("夹具目录或数据库类型不匹配")
    if urlsplit(info["new_app_url"]).hostname != "127.0.0.1":
        raise ValueError("新版后端不在本机回环地址")
    fixture_backend.verify(root, root / "new/backend", info["new_app_url"], info["new_app_pid"])
    new = root / "new"
    frontend = new / "frontend"
    if frontend.exists():
        raise FileExistsError("新版前端目录已存在")
    if info.get("new_frontend_pid"):
        raise RuntimeError("夹具已有新版前端，须由原监督进程清理")
    archive = fixture_process.command(["git", "archive", NEW, "frontend"], cwd=REPO, env=c8.safe_env(), capture=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        source.extractall(new, filter="data")
    if (frontend / ".env").exists():
        raise RuntimeError("新版前端导出包含 .env")
    env = c8.safe_env()
    env.update(LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8", NO_PROXY="*")
    fixture_process.command(
        ["npm", "ci", "--no-audit", "--no-fund"],
        cwd=frontend,
        env=env,
        limit_s=300,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        proxy_port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    marker = root / "new-proxy-maintenance.enabled"
    if marker.exists():
        raise FileExistsError("新版测试代理标记已存在")
    proxy_env = dict(
        env,
        C8_UPSTREAM=info["new_app_url"],
        C8_MARKER=str(marker),
        C8_PROXY_PORT=str(proxy_port),
        C8_FIXTURE_ROOT=str(root),
        C8_UPSTREAM_CWD=str(root / "new/backend"),
        C8_UPSTREAM_PID=str(info["new_app_pid"]),
    )
    frontend_env = dict(env, VITE_BACKEND_URL=proxy_url)
    control_path = None
    try:
        state, control_path = fixture_supervisor.start(
            root,
            "new",
            [
                {
                    "args": [sys.executable, str(Path(__file__).with_name("prepare_maintenance_proxy.py")), "--serve"],
                    "cwd": str(root),
                    "env": proxy_env,
                    "log": "new-proxy.log",
                    "ready": [proxy_url + "/api/v1/auth/config"],
                },
                {
                    "args": ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                    "cwd": str(frontend),
                    "env": frontend_env,
                    "log": "new-frontend.log",
                    "ready": [url + "/", url + "/api/v1/auth/config"],
                },
            ],
        )
        proxy_pid, pid = state["children"]
        info.update(
            new_frontend_url=url,
            new_frontend_pid=pid,
            new_frontend_path=str(frontend),
            new_proxy_url=proxy_url,
            new_proxy_pid=proxy_pid,
            new_supervisor_control=str(control_path),
        )
        metadata.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(metadata, 0o600)
        print(f"新版前端就绪：{url}；进程组 {pid}；页面和后端代理均返回 200。")
    except BaseException:
        if control_path is not None:
            fixture_supervisor.stop(control_path)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, help="C8 夹具的私有 fixture.json 路径")
    main(parser.parse_args().metadata)
