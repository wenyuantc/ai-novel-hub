"""用真实子进程核验夹具监督与失败关闭。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

import c8_drill as c8
import fixture_process
import fixture_supervisor
import prepare_maintenance_proxy
from prepare_maintenance_proxy import remove_owned_marker, terminate_fixture_frontend

SERVER = """import os, signal, socket, subprocess, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
child_code = ('import os,signal,sys,time; from pathlib import Path; '
              'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
              'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)')
subprocess.Popen([sys.executable, '-c', child_code, sys.argv[2]])
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""

REUSE_SERVER = """import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers()
    def log_message(self, *args): pass
server = HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)
server.timeout = 0.1
while not Path(sys.argv[2]).exists(): server.handle_request()
"""


def expect_refused(pid: int, frontend: Path) -> None:
    try:
        terminate_fixture_frontend(pid, frontend)
    except RuntimeError:
        return
    raise AssertionError("未拒绝不属于夹具的进程")


def main() -> None:
    with TemporaryDirectory(prefix="ainovel-c8-drill-") as temp:
        root = Path(temp).resolve()
        frontend = root / "old" / "frontend"
        other = root / "other"
        frontend.mkdir(parents=True)
        other.mkdir()
        marker = root / "maintenance.enabled"
        replacement = root / "replacement"
        marker.touch()
        old = marker.lstat()
        replacement.touch()
        os.replace(replacement, marker)
        current = marker.lstat()
        assert (old.st_dev, old.st_ino) != (current.st_dev, current.st_ino)
        remove_owned_marker(marker, (old.st_dev, old.st_ino))
        assert marker.exists()
        remove_owned_marker(marker, (current.st_dev, current.st_ino))
        assert not marker.exists()
        command = [sys.executable, "-c", "import time; time.sleep(60)"]
        own = subprocess.Popen(command, cwd=frontend, start_new_session=True)
        nonleader = subprocess.Popen(command, cwd=frontend)
        try:
            expect_refused(own.pid, other)
            assert own.poll() is None
            expect_refused(nonleader.pid, frontend)
            assert nonleader.poll() is None
            expect_refused(own.pid, frontend)
            assert own.poll() is None
        finally:
            for process in (own, nonleader):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        child_file = root / "child.pid"
        url = f"http://127.0.0.1:{port}"
        state, control = fixture_supervisor.start(
            root,
            "test",
            [
                {
                    "args": [sys.executable, "-c", SERVER, str(port), str(child_file)],
                    "cwd": str(frontend),
                    "env": c8.safe_env(),
                    "log": "test-server.log",
                    "ready": [url],
                }
            ],
        )
        try:
            deadline = time.monotonic() + 5
            while not child_file.exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("child did not start")
                time.sleep(0.02)
            child_pid = int(child_file.read_text())
            assert control.stat().st_mode & 0o077 == 0
            for frame in (
                b'{"action":"stop","token":"wrong"}\n',
                b"[]\n",
                b"null\n",
                b'"text"\n',
                b"x" * 1024 + b"\n",
                b'{"action":',
            ):
                with socket.create_connection(("127.0.0.1", state["port"]), timeout=2) as peer:
                    peer.sendall(frame)
                    peer.shutdown(socket.SHUT_WR)
                    assert json.loads(peer.makefile("rb").readline())["status"] == "denied"
                assert urllib.request.urlopen(url, timeout=2).status == 200
            c8.mark("监督无效请求拒绝", invalid_frames=6, service_alive=True, plaintext_token_sent=False)
        finally:
            fixture_supervisor.stop(control)
        assert child_pid not in fixture_process_members(state["children"][0])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                raise AssertionError("fixture server still listening")
        except ConnectionRefusedError:
            pass
        fixture_supervisor.stop(control)

        (root / "finish").touch()
        try:
            fixture_supervisor.start(root, "finished", [])
        except RuntimeError as exc:
            assert "fixture_already_finished" in str(exc)
        else:
            raise AssertionError("finished fixture unexpectedly started")
        (root / "finish").unlink()

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            failed_port = probe.getsockname()[1]
        failed_child = root / "failed-child.pid"
        short_server = SERVER.replace(
            "HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()",
            "import threading; threading.Timer(2, lambda: os._exit(23)).start(); "
            "HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()",
        )
        failed_state, failed_control = fixture_supervisor.start(
            root,
            "failed",
            [
                {
                    "args": [sys.executable, "-c", short_server, str(failed_port), str(failed_child)],
                    "cwd": str(frontend),
                    "env": c8.safe_env(),
                    "log": "failed-server.log",
                    "ready": [f"http://127.0.0.1:{failed_port}"],
                }
            ],
        )
        deadline = time.monotonic() + 10
        while True:
            actual = json.loads(failed_control.read_text())
            if actual["status"] != "running":
                break
            if time.monotonic() > deadline:
                raise TimeoutError("failed service not detected")
            time.sleep(0.05)
        assert actual["status"] == "failed" and actual["service_exits"] == [
            {"pid": failed_state["children"][0], "exit_code": 23}
        ]
        assert actual["unverified"] == []
        assert not fixture_process_members(failed_state["children"][0])
        c8.mark("受控服务自退出", recorded_status="failed", exit_code=23, remaining_group_members=0)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            signal_port = probe.getsockname()[1]
        signal_state = root / "signal-supervisor-state.json"
        signal_config = root / "signal-supervisor-config.json"
        fixture_supervisor.private_write(
            signal_config,
            {
                "state_file": signal_state.name,
                "services": [
                    {
                        "args": [sys.executable, "-c", SERVER, str(signal_port), str(root / "signal-child.pid")],
                        "cwd": str(frontend),
                        "env": c8.safe_env(),
                        "log": "signal-server.log",
                        "ready": [f"http://127.0.0.1:{signal_port}"],
                    }
                ],
            },
        )
        with (root / "signal-supervisor.log").open("w") as log:
            supervisor = subprocess.Popen(
                [sys.executable, str(Path(fixture_supervisor.__file__)), "--serve", str(signal_config)],
                cwd=root,
                env=c8.safe_env(),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        try:
            deadline = time.monotonic() + 10
            while not signal_state.exists():
                assert supervisor.poll() is None
                if time.monotonic() > deadline:
                    raise TimeoutError("signal fixture not ready")
                time.sleep(0.05)
            signal_info = json.loads(signal_state.read_text())
            assert fixture_supervisor.control_request(signal_info, "ping")["status"] == "running"
            assert supervisor.poll() is None and supervisor.pid == signal_info["supervisor_pid"]
            supervisor.send_signal(signal.SIGTERM)
            supervisor.wait(timeout=15)
            final = json.loads(signal_state.read_text())
            assert final["status"] == "interrupted" and final["signal"] == signal.SIGTERM
            assert final["unverified"] == [] and final["service_exits"] == []
            assert not fixture_process_members(final["children"][0])
            c8.mark("监督进程 SIGTERM", recorded_status="interrupted", remaining_group_members=0)
        finally:
            if supervisor.poll() is None:
                supervisor.send_signal(signal.SIGTERM)
                supervisor.wait(timeout=15)

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            orphan_port = probe.getsockname()[1]
        orphan_state = root / "orphan-supervisor-state.json"
        orphan_config = root / "orphan-supervisor-config.json"
        orphan_finish = root / "orphan-service-finish"
        fixture_supervisor.private_write(
            orphan_config,
            {
                "state_file": orphan_state.name,
                "services": [
                    {
                        "args": [sys.executable, "-c", REUSE_SERVER, str(orphan_port), str(orphan_finish)],
                        "cwd": str(frontend),
                        "env": c8.safe_env(),
                        "log": "orphan-server.log",
                        "ready": [f"http://127.0.0.1:{orphan_port}"],
                    }
                ],
            },
        )
        with (root / "orphan-supervisor.log").open("w") as log:
            supervisor = subprocess.Popen(
                [sys.executable, str(Path(fixture_supervisor.__file__)), "--serve", str(orphan_config)],
                cwd=root,
                env=c8.safe_env(),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        orphan = None
        try:
            deadline = time.monotonic() + 10
            while not orphan_state.exists():
                assert supervisor.poll() is None
                if time.monotonic() > deadline:
                    raise TimeoutError("orphan fixture not ready")
                time.sleep(0.05)
            orphan = json.loads(orphan_state.read_text())
            assert supervisor.poll() is None and orphan["supervisor_pid"] == supervisor.pid
            supervisor.kill()
            supervisor.wait(timeout=5)
            with socket.socket() as rogue:
                rogue.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                rogue.bind(("127.0.0.1", orphan["port"]))
                rogue.listen(1)
                captured = []

                def impersonate():
                    peer, _ = rogue.accept()
                    with peer:
                        captured.append(peer.recv(4096))
                        peer.sendall(b'{"status":"stopped"}\n')

                thread = threading.Thread(target=impersonate, daemon=True)
                thread.start()
                try:
                    fixture_supervisor.stop(orphan_state)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("forged supervisor accepted")
                thread.join(timeout=5)
                assert not thread.is_alive() and captured and orphan["token"].encode() not in captured[0]
                assert urllib.request.urlopen(f"http://127.0.0.1:{orphan_port}", timeout=2).status == 200
                c8.mark(
                    "监督端口冒充", forged_response_rejected=True, plaintext_token_sent=False, service_still_alive=True
                )
        finally:
            orphan_finish.touch()
            if supervisor.poll() is None:
                supervisor.send_signal(signal.SIGTERM)
                supervisor.wait(timeout=15)
            if orphan is not None:
                deadline = time.monotonic() + 10
                while fixture_process_members(orphan["children"][0]):
                    if time.monotonic() > deadline:
                        raise TimeoutError("orphan test service did not exit")
                    time.sleep(0.05)
    c8.mark(
        "夹具监督故障检查完成", stopped_ignoring_term=True, unverified_pid_refused=True, marker_identity_checked=True
    )


def fixture_process_members(pgid: int) -> set[int]:
    probe = subprocess.run(
        ["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
        capture_output=True,
        env=c8.safe_env(),
        timeout=5,
        check=True,
    )
    result = set()
    for line in probe.stdout.decode().splitlines():
        pid, group, stat = line.split()
        if int(group) == pgid and not stat.startswith("Z"):
            result.add(int(pid))
    return result


if __name__ == "__main__":
    c8.mark(
        "夹具监督故障脚本版本",
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
        supervisor_sha256=hashlib.sha256(Path(fixture_supervisor.__file__).read_bytes()).hexdigest(),
        process_sha256=hashlib.sha256(Path(fixture_process.__file__).read_bytes()).hexdigest(),
        proxy_sha256=hashlib.sha256(Path(prepare_maintenance_proxy.__file__).read_bytes()).hexdigest(),
    )
    main()
