"""在 C8 独立复核夹具中准备 0043 真实前端，不修改夹具数据库。"""

from __future__ import annotations

import argparse
import io
import json
import os
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

import c8_drill as c8
import fixture_backend
import fixture_process

REPO = next(parent for parent in Path(__file__).resolve().parents if parent.name == ".trellis").parent
OLD = "4501f18da97502eb3e352bf85b1853bb0903d60e"


def main(metadata: Path) -> None:
    metadata = metadata.resolve(strict=True)
    root = metadata.parent
    if not root.name.startswith("ainovel-c8-drill-") or metadata.name != "fixture.json":
        raise ValueError("不是 C8 独立夹具")
    info = json.loads(metadata.read_text(encoding="utf-8"))
    if Path(info["root"]).resolve(strict=True) != root or info["shape"] != "sqlite":
        raise ValueError("夹具目录或数据库类型不匹配")
    if urlsplit(info["old_app_url"]).hostname != "127.0.0.1":
        raise ValueError("旧版后端不在本机回环地址")
    fixture_backend.verify(root, root / "old/backend", info["old_app_url"], info["old_app_pid"])
    old = root / "old"
    frontend = old / "frontend"
    if frontend.exists():
        raise FileExistsError("前端目录已存在")
    if info.get("old_frontend_pid"):
        raise RuntimeError("夹具已记录旧前端，须先由原监督进程清理")
    archive = fixture_process.command(["git", "archive", OLD, "frontend"], cwd=REPO, env=c8.safe_env(), capture=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        source.extractall(old, filter="data")
    if (frontend / ".env").exists():
        raise RuntimeError("旧版前端导出包含 .env")
    env = c8.safe_env()
    env.update(LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8", VITE_BACKEND_URL=info["old_app_url"], NO_PROXY="*")
    fixture_process.command(
        ["npm", "ci", "--no-audit", "--no-fund"],
        cwd=frontend,
        env=env,
        limit_s=300,
    )
    info.update(old_frontend_path=str(frontend))
    metadata.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(metadata, 0o600)
    print("旧版前端源码及锁定依赖已准备；维护代理将启动该前端。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, help="C8 夹具的私有 fixture.json 路径")
    main(parser.parse_args().metadata)
