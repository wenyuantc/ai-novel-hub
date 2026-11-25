"""验证关闭测试应用进程组不会终止独立控制器及其本地假模型。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import c8_drill as c8
import httpx

SERVER = """import os, socket, subprocess, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

if os.environ.get("C8_TEST_CHILD") == "1":
    source = "import signal,sys,time; "
    if os.environ.get("C8_TEST_IGNORE_TERM") == "1":
        source += "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    source += "from pathlib import Path; Path(sys.argv[1]).touch(); time.sleep(90)"
    child = subprocess.Popen([sys.executable, "-c", source, os.environ["C8_TEST_CHILD_READY"]])
    Path(os.environ["C8_TEST_CHILD_FILE"]).write_text(str(child.pid))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass

server = HTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
server.socket.close()
server.socket = socket.socket(fileno=int(sys.argv[sys.argv.index("--fd") + 1]))
server.server_address = server.socket.getsockname()
server.serve_forever()
"""


async def no_live_process(pid: int) -> None:
    async with asyncio.timeout(10):
        while True:
            result = await asyncio.create_subprocess_exec(
                "ps", "-p", str(pid), "-o", "stat=", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await result.communicate()
            if result.returncode != 0 or out.decode().strip().startswith("Z"):
                return
            await asyncio.sleep(0.1)


async def no_listener(base: str) -> None:
    port = urlsplit(base).port
    assert port is not None
    async with asyncio.timeout(5):
        while True:
            try:
                _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
            except OSError as exc:
                if exc.errno == errno.ECONNREFUSED:
                    return
                raise
            except TimeoutError:
                pass
            else:
                writer.close()
                await writer.wait_closed()
            await asyncio.sleep(0.1)


async def test_mode(root: Path, backend: Path, model: c8.Model, model_url: str, mode: str) -> None:
    controller_pid = os.getpid()
    controller_group = os.getpgrp()
    child_file = root / f"{mode}.pid"
    ready_file = root / f"{mode}.ready"
    env = dict(
        c8.safe_env(),
        PYTHONPATH=str(backend),
        C8_TEST_CHILD="1",
        C8_TEST_IGNORE_TERM="1" if mode == "stubborn" else "0",
        C8_TEST_CHILD_FILE=str(child_file),
        C8_TEST_CHILD_READY=str(ready_file),
    )
    async with c8.application(root, backend, env, f"isolation-{mode}") as (base, process):
        assert os.getpgid(process.pid) == process.pid != controller_group
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(base + "/healthz")).status_code == 200
        child_pid = int(child_file.read_text())
        async with asyncio.timeout(5):
            while True:
                if await asyncio.to_thread(ready_file.exists):
                    break
                await asyncio.sleep(0.02)
        if mode == "term":
            os.killpg(process.pid, signal.SIGTERM)
            assert await asyncio.wait_for(process.wait(), 10) == -signal.SIGTERM
        elif mode == "kill":
            process.kill()
            assert await asyncio.wait_for(process.wait(), 10) == -signal.SIGKILL
            assert child_pid in await c8.group_members(process.pid)
        elif mode == "stubborn":
            # 先只终止组长；测试已就绪的子进程忽略 SIGTERM，必须升级到 SIGKILL。
            process.terminate()
            assert await asyncio.wait_for(process.wait(), 10) == -signal.SIGTERM
            assert child_pid in await c8.group_members(process.pid)
            await c8.stop_application_group(process, grace_s=0.3)
            await no_live_process(child_pid)
        if mode != "context":
            await no_listener(base)
            assert os.getpid() == controller_pid and os.getpgrp() == controller_group
            call = model.expect()
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    model_url + "/v1/chat/completions", headers={"Authorization": "Bearer " + c8.MODEL_KEY}, json={}
                )
            assert response.status_code == 200 and call.entered.is_set() and call.closed.is_set()
    await no_live_process(child_pid)
    if mode == "context":
        assert process.returncode == -signal.SIGTERM
        await no_listener(base)
        assert os.getpid() == controller_pid and os.getpgrp() == controller_group
        call = model.expect()
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                model_url + "/v1/chat/completions", headers={"Authorization": "Bearer " + c8.MODEL_KEY}, json={}
            )
        assert response.status_code == 200 and call.entered.is_set() and call.closed.is_set()
    c8.mark(
        "应用进程组隔离验证",
        mode=mode,
        app_exit=process.returncode,
        app_port_closed=True,
        model_http=200,
        controller_alive=True,
        descendant_stopped=True,
        ignored_term_escalated=mode == "stubborn",
    )


async def main(root: Path) -> None:
    backend = root / "backend"
    executable = backend / ".venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    (backend / "uvicorn.py").write_text(SERVER)
    model = c8.Model()
    async with c8.fake_model(model) as model_url:
        for mode in ("term", "kill", "stubborn", "context"):
            await test_mode(root, backend, model, model_url, mode)
        assert len(model.valid_keys) == 4 and all(model.valid_keys) and model.unexpected == 0
        c8.mark("进程隔离检查完成", cases=4, unexpected_model_calls=0)


if __name__ == "__main__":
    c8.mark(
        "进程隔离脚本版本",
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
    )
    with TemporaryDirectory(prefix="ainovel-c8-isolation-") as temp:
        asyncio.run(main(Path(temp).resolve()))
