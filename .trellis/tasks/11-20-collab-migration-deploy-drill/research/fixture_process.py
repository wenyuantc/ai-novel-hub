"""短生命周期夹具准备进程的认证进程组；成功交接后仅保留明确标识。"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import c8_drill as c8


class FixtureProcess:
    def __init__(self, process: subprocess.Popen, peer: socket.socket, token: str, guardian_pid: int):
        self.process = process
        self.peer = peer
        self.stream = peer.makefile("rb")
        self.token = token
        self.guardian_pid = guardian_pid
        self.detached = False
        self.cleaned = False

    @property
    def pid(self) -> int:
        return self.process.pid

    def request(self, action: str, expected: bytes) -> None:
        try:
            if os.getpgid(self.guardian_pid) != self.pid or os.getsid(self.guardian_pid) != self.pid:
                raise RuntimeError("fixture_guardian_group_mismatch")
            self.peer.sendall(f"{action} {self.token}\n".encode())
            if self.stream.readline() != expected:
                raise RuntimeError("fixture_guardian_auth_failed")
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"fixture_guardian_unavailable:{self.pid}") from exc

    def members(self) -> set[int]:
        query = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=c8.safe_env(),
            timeout=5,
            check=False,
        )
        if query.returncode:
            raise RuntimeError(f"fixture_group_query_failed:{self.pid}")
        members = set()
        for line in query.stdout.decode().splitlines():
            parts = line.split()
            if len(parts) != 3:
                raise RuntimeError(f"fixture_group_query_invalid:{self.pid}")
            if int(parts[1]) == self.pid and not parts[2].startswith("Z"):
                members.add(int(parts[0]))
        return members

    def stop(self, grace_s: float = 1) -> None:
        if self.cleaned:
            return
        if self.detached:
            raise RuntimeError(f"fixture_detached_cleanup_unverified:{self.pid}")
        try:
            self.request("ping", b"ok\n")
            self.request("term", b"term\n")
            deadline = time.monotonic() + grace_s
            while self.members() - {self.guardian_pid}:
                if time.monotonic() >= deadline:
                    self.request("ping", b"ok\n")
                    self.request("kill", b"kill\n")
                    break
                time.sleep(0.05)
            else:
                self.request("quit", b"bye\n")
            deadline = time.monotonic() + 10
            while self.members():
                if time.monotonic() >= deadline:
                    raise RuntimeError("fixture_processes_remain")
                time.sleep(0.05)
            self.process.wait(timeout=10)
            self.stream.close()
            self.peer.close()
            self.cleaned = True
        except BaseException as exc:
            raise RuntimeError(f"fixture_cleanup_unverified:{self.pid}:{self.guardian_pid}") from exc

    def detach(self) -> None:
        """成功交接后停止守护；后续生命周期由操作者按明确身份处理。"""
        if self.process.poll() is not None:
            raise RuntimeError(f"fixture_process_exited_before_handoff:{self.pid}")
        self.request("ping", b"ok\n")
        self.request("quit", b"bye\n")
        self.stream.close()
        self.peer.close()
        self.detached = True


def spawn(args: list[str], *, cwd: Path, env: dict, stdout=None, stderr=None) -> FixtureProcess:
    parent, child = socket.socketpair()
    parent.settimeout(10)
    token = secrets.token_hex(32)
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", c8.SPAWN_SOURCE, str(child.fileno()), c8.GUARDIAN_SOURCE, *args],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            pass_fds=(child.fileno(),),
            start_new_session=True,
        )
        child.close()
        parent.sendall((token + "\n").encode())
        stream = parent.makefile("rb")
        hello = stream.readline().decode().strip().split(" ")
        stream.close()
        if len(hello) != 2 or not secrets.compare_digest(hello[1], token):
            raise RuntimeError("fixture_guardian_auth_failed")
        guardian_pid = int(hello[0])
        if os.getpgid(guardian_pid) != process.pid or os.getsid(guardian_pid) != process.pid:
            raise RuntimeError("fixture_guardian_group_mismatch")
        return FixtureProcess(process, parent, token, guardian_pid)
    except BaseException as exc:
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        parent.close()
        child.close()
        raise RuntimeError(f"fixture_start_cleanup_unverified:{process.pid if process else 'unknown'}") from exc


def command(args: list[str], *, cwd: Path, env: dict, capture: bool = False, limit_s: float = 300) -> bytes:
    process = spawn(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        output, _ = process.process.communicate(timeout=limit_s)
        if process.process.returncode != 0:
            raise RuntimeError(f"fixture_command_failed:{Path(args[0]).name}:{process.process.returncode}")
        return output or b""
    finally:
        process.stop(grace_s=0.5)
