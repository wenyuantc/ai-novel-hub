"""部署清单的 stdio 传输。SDK 直接向子进程管道写入，发送资格覆盖到 drain。"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import anyio
from anyio.abc import ObjectReceiveStream, ObjectSendStream
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage


@dataclass(frozen=True)
class StdioProgram:
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)
    version: str = ""
    catalog_id: str = ""
    secret_keys: tuple[str, ...] = field(default=(), repr=False)


# 每个 worker 的同时在途会话（含正在启动及回收的子进程）。
_PROCESS_LIMIT = 8
_active = 0
_MAX_FRAME = 4 * 1024 * 1024


def _redact_payload(value, env_values, *, exposed=False):
    """对工具提供的字符串脱敏，短环境值仅匹配整个字符串，协议 ID 和类型保持原样。"""
    if isinstance(value, dict):
        fields = {
            "name",
            "title",
            "description",
            "text",
            "uri",
            "message",
            "data",
            "structuredContent",
            "inputSchema",
            "outputSchema",
        }
        return {
            (_redact_payload(key, env_values, exposed=True) if exposed else key): _redact_payload(
                item, env_values, exposed=exposed or key in fields
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_payload(item, env_values, exposed=exposed) for item in value]
    if exposed and isinstance(value, str):
        for secret, strict in env_values:
            if secret == value:
                return "***"
            if strict or len(secret) >= 6:
                value = value.replace(secret, "***")
    return value


class _Reader(ObjectReceiveStream):
    def __init__(self, pipe, program):
        self.pipe = pipe
        strict_values = {program.env[key] for key in program.secret_keys if key in program.env}
        values = sorted(set(program.env.values()) - {""}, key=len, reverse=True)
        self.env_values = [(value, value in strict_values) for value in values]

    async def receive(self):
        from app.services.mcp.client import McpError

        try:
            line = await self.pipe.readline()
            if not line:
                raise anyio.EndOfStream
            data = json.loads(line)
            if self.env_values and isinstance(data, dict):
                for field_name in ("result", "error", "params"):
                    if field_name in data:
                        data[field_name] = _redact_payload(data[field_name], self.env_values)
            return SessionMessage(message=types.JSONRPCMessage.model_validate(data))
        except ValueError:
            raise McpError("本机插件协议消息无效或过长") from None

    async def aclose(self):
        pass


class _Writer(ObjectSendStream):
    def __init__(self, pipe, gate):
        self.pipe = pipe
        self.gate = gate
        self.lock = asyncio.Lock()

    async def send(self, message):
        data = message.message.model_dump(by_alias=True, mode="json", exclude_none=True)
        payload = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        async with self.lock, self.gate.stdio_write(data):
            self.pipe.write(payload)
            await self.pipe.drain()

    async def aclose(self):
        self.pipe.close()


@asynccontextmanager
async def _process(program, gate):
    from app.services.mcp.client import CONNECT_TIMEOUT, McpError

    global _active
    if os.name != "posix" or not os.path.isabs(program.command):
        raise McpError("本机插件需要 POSIX 主机和已登记的绝对程序路径")
    if _active >= _PROCESS_LIMIT:
        raise McpError("本机插件会话已满，请稍后再试")
    _active += 1
    fds = set()
    proc = None
    try:
        alive_read, alive_write = os.pipe()
        fds.update((alive_read, alive_write))
        ready_read, ready_write = os.pipe()
        fds.update((ready_read, ready_write))
        os.set_blocking(ready_read, False)
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def started():
            if not ready.done():
                ready.set_result(os.read(ready_read, 1) == b"1")

        async with asyncio.timeout(CONNECT_TIMEOUT), gate.stdio_start():
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("stdio_supervisor.py")),
                    str(alive_read),
                    str(ready_write),
                    program.command,
                    *program.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    pass_fds=(alive_read, ready_write),
                    env={},
                    cwd=program.cwd or "/",
                    start_new_session=True,
                    limit=_MAX_FRAME,
                )
            except OSError:
                raise McpError("本机插件无法启动，请检查部署程序和工作目录") from None
            os.close(ready_write)
            fds.remove(ready_write)
            # 仅将插件环境交给将降权的子进程。动态加载器等变量不能作用于 root 监督进程。
            try:
                proc.stdin.write(json.dumps(program.env, ensure_ascii=True).encode() + b"\n")
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise McpError("本机插件监督进程启动失败") from None
            loop.add_reader(ready_read, started)
            try:
                if not await ready:
                    raise McpError("本机插件启动失败，请检查部署程序和执行权限")
            finally:
                loop.remove_reader(ready_read)
        yield proc
    finally:
        for fd in fds:
            os.close(fd)
        try:
            if proc is not None:
                if proc.stdin:
                    proc.stdin.close()
                # 客户端任务取消也必须回收，监督进程会在 alive_write 关闭后结束整个组。
                with anyio.CancelScope(shield=True):
                    try:
                        async with asyncio.timeout(4):
                            if proc.stdout:
                                while await proc.stdout.read(65536):
                                    pass
                            await proc.wait()
                    except TimeoutError:
                        # 别处继承协议管道的异常插件不能让 worker 停服一直等待 EOF。
                        if proc.returncode is None:
                            proc.kill()
                        with anyio.move_on_after(1):
                            await proc.wait()
        finally:
            _active -= 1


@asynccontextmanager
async def connect_stdio(program, gate):
    from app.core.errors import AppError
    from app.services.mcp.client import CALL_TIMEOUT, CONNECT_TIMEOUT, McpError, _leaf, _SdkConnection
    from app.services.mcp.management import ManagementRequestGate

    initialized = False
    try:
        async with _process(program, gate) as proc:
            reader = _Reader(proc.stdout, program)
            writer = _Writer(proc.stdin, gate)
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT)) as session:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await session.initialize()
                initialized = True
                yield _SdkConnection(session, program.env, management=isinstance(gate, ManagementRequestGate))
    except BaseExceptionGroup as group:
        error = _leaf(group)
        if initialized or isinstance(error, (AppError, McpError, asyncio.CancelledError)):
            raise error from None
        raise McpError("本机插件初始化失败，请检查部署版本和协议") from None
    except TimeoutError:
        raise McpError("本机插件响应超时") from None
