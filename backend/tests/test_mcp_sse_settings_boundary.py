"""旧版 MCP SSE 传输中的长 GET 和消息 POST 都使用管理端发送门禁。"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import Response, StreamingResponse

from app.services.mcp.client import connect
from tests.helpers import register
from tests.mcp_fakes import FakeMcpConnector, serve, tool
from tests.schema_helpers import isolated_worker_database
from tests.test_mcp_plugins import BASE, _create
from tests.test_system_settings import save_settings


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield {**overrides, "allow_private_ai_endpoints": True}


@pytest.mark.parametrize("phase", ["get_headers", "call_result", "cancel_call"])
async def test_sse_management_close_while_response_pending(client, second_app, system_admin, phase):
    reached, release = asyncio.Event(), asyncio.Event()
    messages = asyncio.Queue()
    methods = []
    remote = FastAPI()

    @remote.get("/events")
    async def events():
        if phase == "get_headers":
            reached.set()
            await release.wait()

        async def stream():
            yield "event: endpoint\ndata: /messages\n\n"
            while True:
                message = await messages.get()
                if phase == "call_result" and message["id"] == call_id:
                    reached.set()
                    await release.wait()
                yield f"event: message\ndata: {json.dumps(message)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    call_id = None

    @remote.post("/messages")
    async def rpc(request: Request):
        nonlocal call_id
        message = await request.json()
        method = message["method"]
        methods.append(method)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "SSE", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            call_id = message["id"]
            if phase == "cancel_call":
                reached.set()
                await release.wait()
            result = {"content": [{"type": "text", "text": "成功"}], "isError": False}
        else:
            return Response(status_code=202)
        await messages.put({"jsonrpc": "2.0", "id": message["id"], "result": result})
        return Response(status_code=202)

    await register(client)
    async with serve(remote) as url, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://testserver", cookies=client.cookies,
    ) as peer:
        target = f"{url}/events"
        plugin = (await _create(peer, url=target, transport="sse")).json()
        base = f"{BASE}/{plugin['id']}"
        fake = FakeMcpConnector()
        fake.add(target, [tool("lookup")])
        second_app.state.task_engine.ctx.mcp_connector = fake
        assert (await peer.post(f"{base}/refresh")).status_code == 200
        second_app.state.task_engine.ctx.mcp_connector = connect
        operation = asyncio.create_task(peer.post(f"{base}/call", json={"tool": "lookup", "arguments": {}}))
        try:
            await asyncio.wait_for(reached.wait(), 10)
            if phase == "cancel_call":
                # 服务端还没返回消息 POST 的响应头，取消者必须立即归还发送门。
                operation.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(operation, 5)
            await asyncio.wait_for(save_settings(system_admin, mcp_enabled=False), 5)
            if phase == "cancel_call":
                assert methods.count("tools/call") == 1
                return
            assert not operation.done()
            release.set()
            response = await asyncio.wait_for(operation, 10)
            if phase == "get_headers":
                assert response.status_code == 403, response.text
                assert methods == []
            else:
                assert response.status_code == 200, response.text
                assert response.json()["text"] == "成功"
                assert methods.count("tools/call") == 1
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
