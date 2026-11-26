"""仅由 stdio 连接器启动；监督已登记的进程组并在 worker 消失时收回。"""

import json
import os
import resource
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def _cap(kind: int, maximum: int) -> None:
    _, hard = resource.getrlimit(kind)
    limit = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
    resource.setrlimit(kind, (limit, limit))


def _reap_adopted_children() -> None:
    """Linux 子收割器也负责插件另起会话的后台子进程。"""
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            waited, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if waited:
            continue
        for child in children.read_text().split():
            try:
                os.kill(int(child), signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.01)


def main() -> int:
    alive, ready = map(int, sys.argv[1:3])
    _cap(resource.RLIMIT_NOFILE, 256)
    _cap(resource.RLIMIT_CORE, 0)
    _cap(resource.RLIMIT_CPU, 120)
    _cap(resource.RLIMIT_FSIZE, 64 * 1024 * 1024)
    if sys.platform.startswith("linux"):
        import ctypes

        # Linux PR_SET_NO_NEW_PRIVS：随后 exec 的插件及其后代也无法通过 setuid 文件提权。
        libc = ctypes.CDLL(None)
        # PR_SET_CHILD_SUBREAPER 收留插件另起进程组或会话的后代。
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(36, 1, 0, 0, 0) != 0:
            return 1
        _cap(resource.RLIMIT_AS, 1024 * 1024 * 1024)
    identity = {}
    if os.geteuid() == 0:
        import pwd

        account = pwd.getpwnam("nobody")
        identity = {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": ()}
    environment = json.loads(sys.stdin.buffer.readline())
    proc = subprocess.Popen(sys.argv[3:], start_new_session=True, umask=0o077, env=environment, **identity)
    try:
        os.write(ready, b"1")
        os.close(ready)
        # 只有插件应持有协议管道；worker 关闭 stdin 后插件也能看到 EOF。
        os.close(0)
        os.close(1)
        while proc.poll() is None:
            if select.select([alive], [], [], 0.1)[0]:
                break
    finally:
        try:
            # worker 已关闭协议管道。先让插件正常收尾，再处理不响应退出的进程树。
            try:
                proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(0.1)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        if sys.platform.startswith("linux"):
            _reap_adopted_children()
    return 0


if __name__ == "__main__":
    sys.exit(main())
