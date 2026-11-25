"""真实验证演练控制器的进程组归属、超时与取消清理。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import c8_drill as c8

CHILD = """import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(90)
"""

PARENT = """import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])
while not Path(sys.argv[2]).exists():
    time.sleep(0.01)
Path(sys.argv[3]).write_text('leader exiting')
"""


async def wait_file(path: Path) -> None:
    async with asyncio.timeout(5):
        while True:
            if await asyncio.to_thread(path.exists):
                break
            await asyncio.sleep(0.02)


async def no_live_process(pid: int) -> None:
    async with asyncio.timeout(5):
        while True:
            probe = await asyncio.create_subprocess_exec(
                "ps",
                "-p",
                str(pid),
                "-o",
                "stat=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=c8.safe_env(),
            )
            out, _ = await asyncio.wait_for(probe.communicate(), 2)
            if probe.returncode != 0 or out.decode().strip().startswith("Z"):
                return
            await asyncio.sleep(0.05)


async def pipe_case(root: Path, mode: str) -> None:
    child_file = root / f"{mode}-child"
    exit_file = root / f"{mode}-leader"
    started = time.monotonic()
    task = asyncio.create_task(
        c8.command(
            [sys.executable, "-c", PARENT, CHILD, str(child_file), str(exit_file)],
            limit_s=0.7 if mode == "timeout" else 60,
        )
    )
    try:
        await wait_file(exit_file)
        child_pid = int(child_file.read_text())
        if mode == "cancel":
            task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except (TimeoutError, asyncio.CancelledError) as exc:
            assert (mode == "timeout" and isinstance(exc, TimeoutError)) or (
                mode == "cancel" and isinstance(exc, asyncio.CancelledError)
            )
        else:
            raise AssertionError("command unexpectedly succeeded")
        elapsed = time.monotonic() - started
        assert elapsed < 5
        await no_live_process(child_pid)
        c8.mark(
            "继承管道的后代清理",
            mode=mode,
            leader_exited=True,
            child_ignored_term=True,
            child_stopped=True,
            elapsed_s_test=round(elapsed, 3),
        )
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


async def authentication_case() -> None:
    process = await c8.spawn_owned_process(
        [sys.executable, "-c", "import time; time.sleep(90)"],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    original = process.token
    try:
        process.token = "wrong-identity"
        try:
            await c8.stop_application_group(process, grace_s=0.1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("incorrect guardian identity accepted")
        assert process.returncode is None
        assert process.pid in await c8.group_members(process.pid)
        c8.mark("守护身份错误验证", rejected=True, target_still_alive=True, no_group_signal=True)
    finally:
        process.token = original
        await c8.cleanup_owned(process, grace_s=0.1)
        await c8.cleanup_owned(process, grace_s=0.1)
    assert process.cleaned
    c8.mark("重复清理验证", idempotent=True)


async def dead_guardian_case() -> None:
    process = await c8.spawn_owned_process(
        [sys.executable, "-c", "import time; time.sleep(90)"],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        # 守护是本函数刚获得且仍由私有通道证明的真实 PID。
        await c8.guardian_request(process, "ping", b"ok\n")
        os.kill(process.guardian_pid, signal.SIGKILL)
        await no_live_process(process.guardian_pid)
        try:
            await c8.stop_application_group(process, grace_s=0.1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("dead guardian accepted")
        assert process.returncode is None
        c8.mark("守护先退出验证", rejected=True, target_still_alive=True, no_group_signal=True)
    finally:
        # 只有本函数持有的组长句柄被单独终止；本例目标不创建后代。
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5)
        process.writer.close()
        await process.writer.wait_closed()
        await no_live_process(process.pid)


async def cancelled_creation_case() -> None:
    started = asyncio.Event()
    original = asyncio.create_subprocess_exec
    created = []

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        if kwargs.get("start_new_session"):
            created.append(process)
            started.set()
            await asyncio.sleep(0.2)
        return process

    asyncio.create_subprocess_exec = delayed
    try:
        task = asyncio.create_task(
            c8.spawn_owned_process(
                [sys.executable, "-c", "import time; time.sleep(90)"],
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("creation cancellation not propagated")
        assert len(created) == 1 and created[0].returncode is not None
        assert not await c8.group_members(created[0].pid)
        c8.mark("创建期间取消验证", created_process_exited=True, owned_group_empty=True)
    finally:
        asyncio.create_subprocess_exec = original


async def environment_case() -> None:
    keys = ("C8_PRIVATE_TEST_VALUE", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
    before = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.update(
            C8_PRIVATE_TEST_VALUE="fictional-sensitive-value",
            DOCKER_HOST="unix:///fictional-socket",
            DOCKER_CONTEXT="fictional-context",
            DOCKER_CONFIG="/fictional/config",
        )
        code = "import os; assert 'C8_PRIVATE_TEST_VALUE' not in os.environ; assert 'DOCKER_HOST' not in os.environ"
        await c8.command([sys.executable, "-c", code])
        docker = c8.safe_env(docker=True)
        assert all(docker[key] == os.environ[key] for key in keys[1:])
        assert "C8_PRIVATE_TEST_VALUE" not in docker
        c8.mark("环境白名单验证", command_private_value_excluded=True, docker_connection_forwarded=True)
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def main(root: Path) -> None:
    await environment_case()
    await pipe_case(root, "timeout")
    await pipe_case(root, "cancel")
    await authentication_case()
    await dead_guardian_case()
    await cancelled_creation_case()
    c8.mark("进程清理故障检查完成", cases=7)


if __name__ == "__main__":
    c8.mark(
        "进程清理脚本版本",
        sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        controller_sha256=hashlib.sha256(Path(c8.__file__).read_bytes()).hexdigest(),
    )
    with TemporaryDirectory(prefix="ainovel-c8-process-") as temporary:
        asyncio.run(main(Path(temporary).resolve()))
