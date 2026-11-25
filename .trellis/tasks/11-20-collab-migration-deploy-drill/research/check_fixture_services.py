"""实跑固定新旧前端、维护代理及认证监督；后端为本机最小替身。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

import c8_drill as c8
import fixture_backend
import fixture_process
import fixture_supervisor
import prepare_maintenance_proxy
import prepare_new_frontend
import prepare_old_frontend

BACKEND = """import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with Path(sys.argv[2]).open('a') as counter: counter.write('GET\\n')
        self.send_response(401 if self.path.startswith('/api/v1/admin/') else 200)
        self.end_headers(); self.wfile.write(b'{}')
    def do_POST(self):
        with Path(sys.argv[2]).open('a') as counter: counter.write('POST\\n')
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_response(401); self.end_headers(); self.wfile.write(b'{}')
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


def request(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="ainovel-c8-drill-")).resolve()
    for directory in (root / "old/backend", root / "new/backend", root / "other"):
        directory.mkdir(parents=True)
    backends = []

    def start_backend(directory: Path, counter: Path, port: int | None = None):
        if port is None:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        backend = fixture_process.spawn(
            [sys.executable, "-c", BACKEND, str(port), str(counter)],
            cwd=directory,
            env=c8.safe_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        backends.append(backend)
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if backend.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("fake backend not ready") from None
                time.sleep(0.05)
        return backend, f"http://127.0.0.1:{port}", port

    failure = False
    try:
        old_counter, new_counter = root / "old.counter", root / "new.counter"
        old, old_url, old_port = start_backend(root / "old/backend", old_counter)
        new, new_url, _ = start_backend(root / "new/backend", new_counter)
        expired, _, _ = start_backend(root / "old/backend", root / "expired.counter")
        expired.stop()
        metadata = root / "fixture.json"
        info = {
            "root": str(root),
            "shape": "sqlite",
            "old_app_url": old_url,
            "old_app_pid": old.pid,
            "new_app_url": new_url,
            "new_app_pid": new.pid,
        }

        for label, updates in (
            ("wrong_pid", {"old_app_pid": new.pid}),
            ("wrong_port", {"old_app_url": new_url}),
            ("wrong_cwd", {"old_app_pid": new.pid, "old_app_url": new_url}),
            ("exited", {"old_app_pid": expired.pid}),
        ):
            metadata.write_text(json.dumps(dict(info, **updates)))
            try:
                prepare_maintenance_proxy.install(metadata)
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                raise AssertionError(f"unsafe backend accepted: {label}")
            assert not old_counter.exists() and not new_counter.exists()
        metadata.write_text(json.dumps(dict(info, new_app_pid=old.pid)))
        try:
            prepare_new_frontend.main(metadata)
        except (OSError, RuntimeError, ValueError):
            pass
        else:
            raise AssertionError("unsafe new backend accepted")
        assert not old_counter.exists() and not new_counter.exists()
        c8.mark("无关后端拒绝验证", wrong_pid=True, wrong_port=True, wrong_cwd=True, exited=True, forwarded_requests=0)
        metadata.write_text(json.dumps(info))
        metadata.chmod(0o600)
        with redirect_stdout(io.StringIO()):
            prepare_old_frontend.main(metadata)
            prepare_maintenance_proxy.install(metadata)
            prepare_new_frontend.main(metadata)
        info = json.loads(metadata.read_text())
        assert request(info["old_frontend_url"] + "/") == 200
        assert request(info["new_frontend_url"] + "/") == 200
        assert request(info["old_frontend_url"] + "/api/v1/auth/config") == 200
        assert request(info["new_frontend_url"] + "/api/v1/auth/config") == 200
        assert Path(info["old_supervisor_control"]).stat().st_mode & 0o077 == 0
        assert Path(info["new_supervisor_control"]).stat().st_mode & 0o077 == 0
        c8.mark("固定前端和代理夹具验证", old_ui=200, new_ui=200, api_proxy=200, private_controls=True)

        old.stop()
        unrelated_counter = root / "unrelated.counter"
        _, _, _ = start_backend(root / "other", unrelated_counter, old_port)
        assert request(info["maintenance_proxy_url"] + "/api/v1/auth/config") == 503
        assert not unrelated_counter.exists()
        c8.mark("旧端口被其他进程复用", proxy_http=503, unrelated_requests=0)

        restored, restored_url, _ = start_backend(root / "old/backend", root / "restored.counter")
        Path(info["maintenance_marker"]).touch()
        previous_control = Path(info["old_supervisor_control"])
        with redirect_stdout(io.StringIO()):
            prepare_maintenance_proxy.retarget(metadata, restored_url, restored.pid)
        updated = json.loads(metadata.read_text())
        assert Path(updated["maintenance_marker"]).is_file()
        assert json.loads(previous_control.read_text())["status"] == "stopped"
        assert updated["old_supervisor_control"] != info["old_supervisor_control"]
        assert request(updated["maintenance_proxy_url"] + "/api/v1/auth/config") == 200
        assert prepare_maintenance_proxy.status(updated["maintenance_proxy_url"] + "/api/v1/projects", "POST") == 503
        c8.mark("回滚代理认证重定向", maintenance_preserved=True, blocked_post=503, old_control_stopped=True)
    except BaseException:
        failure = True
        raise
    finally:
        cleanup_errors = []
        for state in root.glob("*-supervisor-state.json"):
            try:
                fixture_supervisor.stop(state)
            except BaseException:
                cleanup_errors.append(state.name)
        for backend in backends:
            try:
                backend.stop()
            except BaseException:
                cleanup_errors.append(f"backend:{backend.pid}")
        if cleanup_errors:
            c8.mark("夹具清理未确认", fixture=str(root), components=cleanup_errors)
            raise RuntimeError("fixture_cleanup_unverified")
        shutil.rmtree(root)
        c8.mark("夹具认证清理完成", old=True, new=True, backend=True, removed=True, run_failed=failure)


if __name__ == "__main__":
    c8.mark(
        "夹具集成脚本版本",
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
        helpers_sha256={
            module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (
                fixture_backend,
                fixture_process,
                fixture_supervisor,
                prepare_maintenance_proxy,
                prepare_old_frontend,
                prepare_new_frontend,
            )
        },
    )
    main()
