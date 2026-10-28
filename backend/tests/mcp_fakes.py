"""MCP 测试替身：FakeMcpConnector（按地址脚本化工具与结果，记录连接 / 关闭 / 调用）与本机 uvicorn 服务。"""

import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn

from app.services.mcp.client import McpError, McpResult, McpTarget, McpToolInfo


def tool(name: str, properties: dict | None = None, required: list[str] | None = None, **kw) -> McpToolInfo:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return McpToolInfo(name=name, description=kw.pop("description", f"{name} 工具"), input_schema=schema, **kw)


Handler = Callable[[str, dict], Any]


@dataclass
class FakeServer:
    tools: list[McpToolInfo]
    handler: Handler | None = None
    # 下一次 connect 时抛出（模拟连接失败）
    connect_error: Exception | None = None


@dataclass
class FakeMcpConnector:
    servers: dict[str, FakeServer] = field(default_factory=dict)
    targets: list[McpTarget] = field(default_factory=list)
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    closes: int = 0
    open: int = 0

    def add(self, url: str, tools: list[McpToolInfo], handler: Handler | None = None) -> FakeServer:
        server = FakeServer(tools=tools, handler=handler)
        self.servers[url] = server
        return server

    @property
    def connects(self) -> int:
        return len(self.targets)

    @asynccontextmanager
    async def __call__(self, target: McpTarget, *, allow_private: bool) -> AsyncIterator[Any]:
        self.targets.append(target)
        server = self.servers.get(target.url)
        if server is None:
            raise McpError("无法连接 MCP 服务，请检查地址是否正确、服务是否已启动")
        if server.connect_error is not None:
            raise server.connect_error
        fake = self
        self.open += 1

        class _Conn:
            async def list_tools(self) -> list[McpToolInfo]:
                return list(server.tools)

            async def call_tool(self, name: str, arguments: dict) -> McpResult:
                fake.calls.append((target.url, name, dict(arguments)))
                if server.handler is None:
                    return McpResult(text=f"{name} 结果：{arguments}")
                value = server.handler(name, arguments)
                if asyncio.iscoroutine(value):
                    value = await value
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, McpResult) else McpResult(text=str(value))

        try:
            yield _Conn()
        finally:
            self.open -= 1
            self.closes += 1


@asynccontextmanager
async def serve(asgi_app) -> AsyncIterator[str]:
    """在 127.0.0.1 的随机端口上运行 ASGI 应用（真实 HTTP），yield 基础地址。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(asgi_app, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
