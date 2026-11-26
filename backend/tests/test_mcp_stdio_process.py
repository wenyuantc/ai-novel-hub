import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.mcp import client
from app.services.mcp.stdio import StdioProgram

pytestmark = pytest.mark.skipif(os.name != "posix", reason="本机托管需要 POSIX 进程组")


@asynccontextmanager
async def permit():
    yield


def target():
    program = StdioProgram(
        command=sys.executable,
        env={"DEBUG": "1", "LANG": "C"},
        args=(str(Path(__file__).parent / "fixtures/stdio_plugin.py"),),
    )
    return client.McpTarget("", "stdio", stdio=program)


async def test_real_stdio_writes_only_after_lease_and_reaps_process_group(monkeypatch):
    monkeypatch.setenv("WORKER_MARKER", "private-worker-environment")
    gate = client.McpRequestGate()
    gate.start_lease = permit
    state = SimpleNamespace(started=False)
    async with client.connect(target(), allow_private=False, request_gate=gate) as conn:
        assert [t.name for t in await conn.list_tools()] == ["record"]
        gate.arm("record", {}, state, permit, future=asyncio.get_running_loop().create_future())
        detail = json.loads((await conn.call_tool("record", {})).text)
        assert state.started
        assert detail["marker"] is None
        assert detail["uid"] != 0
    await assert_reaped(detail)


async def assert_reaped(detail):
    for key in ("pid", "child"):
        for _ in range(50):
            try:
                os.kill(detail[key], 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.03)
        else:
            # Linux PID 1 may reap an orphaned zombie later; it already ceased running.
            stat = Path(f"/proc/{detail[key]}/stat")
            assert await asyncio.to_thread(stat.is_file) and (await asyncio.to_thread(stat.read_text)).split(") ")[
                1
            ].startswith("Z ")


async def test_stdio_denied_write_does_not_mark_started():
    @asynccontextmanager
    async def deny():
        raise client.McpError("已撤销")
        yield

    gate = client.McpRequestGate()
    gate.start_lease = permit
    state = SimpleNamespace(started=False)
    async with client.connect(target(), allow_private=False, request_gate=gate) as conn:
        gate.arm("record", {}, state, deny, future=asyncio.get_running_loop().create_future())
        with pytest.raises(client.McpError, match="已撤销"):
            await conn.call_tool("record", {})
        assert not state.started


async def test_missing_stdio_executable_is_reported_cleanly():
    gate = client.McpRequestGate()
    gate.start_lease = permit
    invalid = client.McpTarget("", "stdio", stdio=StdioProgram("/missing/stdio-fixture"))
    with pytest.raises(client.McpError, match="启动失败"):
        async with client.connect(invalid, allow_private=False, request_gate=gate):
            pytest.fail("unexpected connection")


async def test_killed_worker_releases_real_plugin_and_descendant():
    # 子 worker 没有机会运行 finally，只有监督进程能观察存活管道 EOF。
    script = """
import asyncio, json, sys
from contextlib import asynccontextmanager
from app.services.mcp.stdio import StdioProgram, _process

class Gate:
    @asynccontextmanager
    async def stdio_start(self):
        yield

async def main():
    async with _process(StdioProgram(sys.executable, (sys.argv[1],)), Gate()) as process:
        process.stdin.write(b'{"id":1,"method":"tools/call","params":{}}\\n')
        await process.stdin.drain()
        detail = json.loads(await process.stdout.readline())["result"]["content"][0]["text"]
        print(detail, flush=True)
        await asyncio.Event().wait()

asyncio.run(main())
"""
    plugin = str(Path(__file__).parent / "fixtures/stdio_plugin.py")
    worker = await asyncio.create_subprocess_exec(sys.executable, "-c", script, plugin, stdout=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(45):
            detail = json.loads(await worker.stdout.readline())
    finally:
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
    await assert_reaped(detail)


async def test_stdio_start_limit_and_initialization_timeout(monkeypatch):
    from app.services.mcp import stdio

    gate = client.McpRequestGate()
    gate.start_lease = permit
    monkeypatch.setattr(stdio, "_PROCESS_LIMIT", 1)
    async with client.connect(target(), allow_private=False, request_gate=gate):
        with pytest.raises(client.McpError, match="已满"):
            async with client.connect(target(), allow_private=False, request_gate=gate):
                pytest.fail("unexpected parallel session")
    monkeypatch.setattr(client, "CONNECT_TIMEOUT", 0.4)
    sleeper = StdioProgram(sys.executable, ("-c", "import time; time.sleep(30)"))
    with pytest.raises(client.McpError, match="超时|初始化失败"):
        async with client.connect(client.McpTarget("", "stdio", stdio=sleeper), allow_private=False, request_gate=gate):
            pytest.fail("unexpected handshake")
    assert stdio._active == 0


async def test_cancel_waiting_tool_reaps_children_and_redacts_deployment_value():
    original = target().stdio
    program = StdioProgram(original.command, original.args, env={"WORKER_MARKER": "xy"}, secret_keys=("WORKER_MARKER",))
    gate = client.McpRequestGate()
    gate.start_lease = permit
    async with client.connect(
        client.McpTarget("", "stdio", stdio=program), allow_private=False, request_gate=gate
    ) as conn:
        loop = asyncio.get_running_loop()
        gate.arm("record", {}, SimpleNamespace(started=False), permit, future=loop.create_future())
        detail = json.loads((await conn.call_tool("record", {})).text)
        assert detail["marker"] == "***"
        gate.disarm()
        state = SimpleNamespace(started=False)
        sent = asyncio.Event()

        @asynccontextmanager
        async def signal_sent():
            yield
            sent.set()

        gate.arm("record", {"wait": True}, state, signal_sent, future=loop.create_future())
        waiting = asyncio.create_task(conn.call_tool("record", {"wait": True}))
        async with asyncio.timeout(5):
            await sent.wait()
        assert state.started
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    await assert_reaped(detail)
