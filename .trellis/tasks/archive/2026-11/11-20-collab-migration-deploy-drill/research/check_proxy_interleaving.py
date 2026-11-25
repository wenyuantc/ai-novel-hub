"""强制后端端口在代理检查与发送之间复用，检查无业务 HTTP 发往无关服务。"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import c8_drill as c8
import fixture_backend
import httpx
import prepare_maintenance_proxy

SERVER = """import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Server(HTTPServer):
    def get_request(self):
        sock, address = super().get_request()
        with Path(sys.argv[2]).open('a') as output: output.write('accept\\n')
        return sock, address
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with Path(sys.argv[3]).open('a') as output: output.write('http\\n')
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *args): pass
Server(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


async def wait_listener(pid: int, port: int) -> None:
    async with asyncio.timeout(10):
        while True:
            probe = await asyncio.create_subprocess_exec(
                "lsof",
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fn",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=c8.safe_env(),
            )
            output, _ = await asyncio.wait_for(probe.communicate(), 5)
            if probe.returncode == 0 and f"n127.0.0.1:{port}" in output.decode().splitlines():
                return
            await asyncio.sleep(0.05)


async def scenario(root: Path, stage: str) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    old_cwd = root / "old/backend"
    other_cwd = root / "other"
    old_accept, old_requests = root / f"{stage}-old-accept", root / f"{stage}-old-http"
    other_accept, other_requests = root / f"{stage}-other-accept", root / f"{stage}-other-http"
    old = await c8.spawn_owned_process(
        [sys.executable, "-c", SERVER, str(port), str(old_accept), str(old_requests)],
        cwd=old_cwd,
        env=c8.safe_env(),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    other = None
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate(current: str) -> None:
        if current == stage:
            entered.set()
            await asyncio.wait_for(release.wait(), 15)

    try:
        await wait_listener(old.pid, port)
        app = prepare_maintenance_proxy.create_app(
            f"http://127.0.0.1:{port}",
            root / "maintenance-disabled",
            root,
            old_cwd,
            old.pid,
            test_stage=gate,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture", timeout=20
        ) as client:
            task = asyncio.create_task(client.get("/api/v1/auth/config"))
            await asyncio.wait_for(entered.wait(), 10)
            await c8.cleanup_owned(old, grace_s=0.5)
            other = await c8.spawn_owned_process(
                [sys.executable, "-c", SERVER, str(port), str(other_accept), str(other_requests)],
                cwd=other_cwd,
                env=c8.safe_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await wait_listener(other.pid, port)
            release.set()
            response = await asyncio.wait_for(task, 10)
            assert response.status_code == 503
            assert not other_requests.exists()
            observed_accepts = len(other_accept.read_text().splitlines()) if other_accept.exists() else 0
            assert observed_accepts == (1 if stage == "before_connect" else 0)
            c8.mark(
                "端口复用强制交错",
                stage=stage,
                proxy_http=503,
                unrelated_tcp_accepts=observed_accepts,
                unrelated_http_requests=0,
            )
    finally:
        release.set()
        await c8.cleanup_owned(old, grace_s=0.5)
        if other is not None:
            await c8.cleanup_owned(other, grace_s=0.5)


async def main(root: Path) -> None:
    (root / "old/backend").mkdir(parents=True)
    (root / "other").mkdir()
    for stage in ("before_connect", "after_verification"):
        await scenario(root, stage)


if __name__ == "__main__":
    c8.mark(
        "代理交错脚本版本",
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
        backend_sha256=hashlib.sha256(Path(fixture_backend.__file__).read_bytes()).hexdigest(),
        proxy_sha256=hashlib.sha256(Path(prepare_maintenance_proxy.__file__).read_bytes()).hexdigest(),
    )
    root = Path(tempfile.mkdtemp(prefix="ainovel-c8-drill-")).resolve()
    try:
        asyncio.run(main(root))
    except BaseException:
        c8.mark("代理交错检查失败", fixture=str(root))
        raise
    else:
        shutil.rmtree(root)
        c8.mark("代理交错检查完成", cases=2, removed=True)
