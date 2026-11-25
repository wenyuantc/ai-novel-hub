"""夹具进程长驻监督：私有控制凭证、认证进程组清理和完成标记。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import c8_drill as c8
import fixture_process


def signature(token: str, purpose: str, nonce: str, payload: str = "") -> str:
    message = f"{purpose}\0{nonce}\0{payload}".encode()
    return hmac.new(bytes.fromhex(token), message, hashlib.sha256).hexdigest()


def encoded(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def read_frame(stream, limit: int = 1024) -> dict:
    frame = stream.readline(limit + 1)
    if len(frame) > limit or not frame.endswith(b"\n"):
        raise ValueError("invalid control frame")
    data = json.loads(frame)
    if not isinstance(data, dict):
        raise ValueError("control request is not an object")
    return data


def signed_response(peer: socket.socket, token: str, nonce: str, action: str, data: dict) -> None:
    envelope = dict(data, proof=signature(token, "response:" + action, nonce, encoded(data)))
    peer.sendall((encoded(envelope) + "\n").encode())


def private_write(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ready(url: str, process: fixture_process.FixtureProcess, stopping) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if stopping():
            raise RuntimeError("fixture_supervisor_interrupted")
        if process.process.poll() is not None:
            raise RuntimeError(f"fixture_service_exited:{process.pid}")
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise TimeoutError(f"fixture_service_not_ready:{process.pid}")


def serve(config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent.resolve(strict=True)
    if not root.name.startswith("ainovel-c8-drill-"):
        raise ValueError("not a C8 fixture")
    if (root / "finish").exists():
        raise RuntimeError("fixture_already_finished")
    state_path = root / config["state_file"]
    processes = []
    server = socket.socket()
    peer = None
    failures = []
    service_exits = []
    state = None
    received_signal = None
    control_nonce = None
    control_action = None

    def interrupt(signum, _frame):
        nonlocal received_signal
        received_signal = signum

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(0.25)
        token = secrets.token_hex(32)
        for entry in config["services"]:
            cwd = Path(entry["cwd"]).resolve(strict=True)
            if root not in cwd.parents and cwd != root:
                raise ValueError("fixture cwd mismatch")
            with (root / entry["log"]).open("w", encoding="utf-8") as log:
                process = fixture_process.spawn(
                    entry["args"],
                    cwd=cwd,
                    env=entry["env"],
                    stdout=log,
                    stderr=log,
                )
            processes.append(process)
            for url in entry["ready"]:
                ready(url, process, lambda: received_signal is not None)
        state = {
            "host": "127.0.0.1",
            "port": server.getsockname()[1],
            "token": token,
            "supervisor_pid": os.getpid(),
            "children": [process.pid for process in processes],
            "status": "running",
        }
        private_write(state_path, state)
        while True:
            service_exits = [
                {"pid": process.pid, "exit_code": process.process.returncode}
                for process in processes
                if process.process.poll() is not None
            ]
            if service_exits or received_signal is not None or (root / "finish").exists():
                break
            try:
                peer, address = server.accept()
            except TimeoutError:
                continue
            peer.settimeout(3)
            if address[0] != "127.0.0.1":
                peer.close()
                peer = None
                continue
            try:
                stream = peer.makefile("rb")
                hello = read_frame(stream)
                nonce = hello.get("nonce")
                if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
                    raise ValueError("invalid nonce")
                peer.sendall((encoded({"proof": signature(token, "server", nonce)}) + "\n").encode())
                request = read_frame(stream)
                action = request.get("action")
                if action not in ("ping", "stop") or not secrets.compare_digest(
                    request.get("proof", ""), signature(token, "client", nonce, action)
                ):
                    raise ValueError("invalid client proof")
                control_nonce, control_action = nonce, action
                if action == "stop":
                    break
                service_exits = [
                    {"pid": process.pid, "exit_code": process.process.returncode}
                    for process in processes
                    if process.process.poll() is not None
                ]
                if service_exits or received_signal is not None:
                    break
                signed_response(peer, token, nonce, action, {"status": "running"})
                peer.close()
                peer = None
                continue
            except (OSError, ValueError, TypeError, RecursionError):
                pass
            try:
                peer.sendall(b'{"status":"denied"}\n')
            except OSError:
                pass
            peer.close()
            peer = None
    finally:
        if not service_exits:
            service_exits = [
                {"pid": process.pid, "exit_code": process.process.returncode}
                for process in processes
                if process.process.poll() is not None
            ]
        for process in reversed(processes):
            try:
                process.stop()
            except BaseException:
                failures.append(process.pid)
        if state is not None:
            state["status"] = (
                "failed" if failures or service_exits else "interrupted" if received_signal is not None else "stopped"
            )
            state["unverified"] = failures
            state["service_exits"] = service_exits
            state["signal"] = received_signal
            private_write(state_path, state)
        if peer is not None and control_nonce is not None and control_action is not None:
            try:
                signed_response(
                    peer,
                    token,
                    control_nonce,
                    control_action,
                    {
                        "status": "failed"
                        if failures or service_exits
                        else ("interrupted" if received_signal is not None else "stopped"),
                        "unverified": failures,
                        "service_exits": service_exits,
                    },
                )
            except OSError:
                pass
            peer.close()
        server.close()
        if failures or service_exits:
            raise RuntimeError(f"fixture_supervisor_failed:{service_exits}:{failures}")


def start(root: Path, name: str, services: list[dict]) -> tuple[dict, Path]:
    if (root / "finish").exists():
        raise RuntimeError("fixture_already_finished")
    config_path = root / f"{name}-supervisor-config.json"
    state_path = root / f"{name}-supervisor-state.json"
    if config_path.exists() or state_path.exists():
        raise FileExistsError("fixture supervisor already exists")
    private_write(config_path, {"state_file": state_path.name, "services": services})
    with (root / f"{name}-supervisor.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve", str(config_path)],
            cwd=root,
            env=c8.safe_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("supervisor_pid") != process.pid or state.get("host") != "127.0.0.1":
                raise RuntimeError(f"fixture_supervisor_identity_mismatch:{process.pid}")
            if state.get("status") != "running" or process.poll() is not None or (root / "finish").exists():
                raise RuntimeError(f"fixture_supervisor_not_running:{process.pid}:{state_path}")
            try:
                response = control_request(state, "ping")
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                raise RuntimeError(f"fixture_supervisor_auth_unverified:{process.pid}:{state_path}") from exc
            if response.get("status") != "running":
                raise RuntimeError(f"fixture_supervisor_not_running:{process.pid}:{state_path}")
            if process.poll() is not None or (root / "finish").exists():
                raise RuntimeError(f"fixture_supervisor_not_running:{process.pid}:{state_path}")
            return state, state_path
        if process.poll() is not None:
            raise RuntimeError(f"fixture_supervisor_start_failed:{process.pid}:{process.returncode}")
        time.sleep(0.1)
    # 创建结果不明时，不按 PID 盲杀；报告身份及状态文件供后续认证。
    raise RuntimeError(f"fixture_supervisor_start_unverified:{process.pid}:{state_path}")


def control_request(state: dict, action: str) -> dict:
    if state.get("host") != "127.0.0.1":
        raise ValueError("fixture control is not loopback")
    with socket.create_connection(("127.0.0.1", int(state["port"])), timeout=5) as peer:
        peer.settimeout(60)
        stream = peer.makefile("rb")
        nonce = secrets.token_hex(32)
        token = state["token"]
        peer.sendall((encoded({"nonce": nonce}) + "\n").encode())
        hello = read_frame(stream)
        if not secrets.compare_digest(hello.get("proof", ""), signature(token, "server", nonce)):
            raise RuntimeError("fixture_supervisor_server_auth_failed")
        peer.sendall((encoded({"action": action, "proof": signature(token, "client", nonce, action)}) + "\n").encode())
        response = read_frame(stream, 4096)
        proof = response.pop("proof", "")
        if not secrets.compare_digest(proof, signature(token, "response:" + action, nonce, encoded(response))):
            raise RuntimeError("fixture_supervisor_response_auth_failed")
        return response


def stop(state_path: Path) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") == "stopped":
        return
    if state.get("status") == "failed":
        raise RuntimeError(f"fixture_supervisor_failed:{state.get('service_exits', [])}:{state.get('unverified', [])}")
    response = control_request(state, "stop")
    if response.get("status") != "stopped":
        raise RuntimeError(f"fixture_supervisor_cleanup_unverified:{response.get('unverified', [])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--stop", type=Path)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve.resolve(strict=True))
    elif args.stop:
        stop(args.stop.resolve(strict=True))
        print("夹具监督进程确认所属子进程组已停止。")
    else:
        parser.error("必须指定 --serve 或 --stop")
