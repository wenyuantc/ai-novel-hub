"""独立夹具仅允许访问本轮 backend 工作目录里的回环监听。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import c8_drill as c8


def verify(root: Path, backend: Path, url: str, pid: int, *, client_port: int | None = None) -> None:
    root = root.resolve(strict=True)
    backend = backend.resolve(strict=True)
    if not root.name.startswith("ainovel-c8-drill-") or backend not in (root / "old/backend", root / "new/backend"):
        raise RuntimeError("fixture_backend_path_mismatch")
    target = urlsplit(url)
    if (
        target.scheme != "http"
        or target.hostname != "127.0.0.1"
        or target.port is None
        or target.username is not None
        or target.password is not None
        or target.path not in ("", "/")
        or target.query
        or target.fragment
        or not isinstance(pid, int)
        or pid <= 1
    ):
        raise RuntimeError("fixture_backend_address_invalid")

    def lsof(*args: str) -> list[str]:
        try:
            result = subprocess.run(
                ["lsof", *args],
                env=c8.safe_env(),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("fixture_backend_identity_unverified") from exc
        if result.returncode != 0:
            raise RuntimeError("fixture_backend_identity_unverified")
        return result.stdout.splitlines()

    cwd = lsof("-a", "-p", str(pid), "-d", "cwd", "-Fn")
    if [Path(line[1:]).resolve() for line in cwd if line.startswith("n")] != [backend]:
        raise RuntimeError("fixture_backend_cwd_mismatch")
    state = "ESTABLISHED" if client_port is not None else "LISTEN"
    entries = lsof("-nP", "-a", "-p", str(pid), f"-iTCP:{target.port}", f"-sTCP:{state}", "-Fn")
    expected = f"127.0.0.1:{target.port}"
    if client_port is not None:
        expected += f"->127.0.0.1:{client_port}"
    if expected not in [line[1:] for line in entries if line.startswith("n")]:
        raise RuntimeError("fixture_backend_socket_mismatch")
