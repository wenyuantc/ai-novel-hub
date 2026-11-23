"""MCP 插件管理与外连的跨实例开关。"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.ai import endpoint_guard
from app.services.mcp import client as mcp_client
from app.services.mcp import service
from tests.helpers import register
from tests.mcp_fakes import FakeMcpConnector, serve, tool
from tests.schema_helpers import isolated_worker_database
from tests.test_mcp_plugins import BASE, URL, _create
from tests.test_system_settings import save_settings


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield {**overrides, "allow_private_ai_endpoints": True}


async def test_peer_disable_blocks_all_plugin_routes_before_connection(client, second_app, system_admin):
    await register(client)
    fake = FakeMcpConnector()
    fake.add(URL, [tool("lookup")])
    second_app.state.task_engine.ctx.mcp_connector = fake
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
                                 cookies=client.cookies) as peer:
        plugin = (await _create(peer)).json()
        item = f"{BASE}/{plugin['id']}"
        assert (await peer.post(f"{item}/refresh")).status_code == 200
        existing_connections = fake.connects
        await save_settings(system_admin, mcp_enabled=False)
        for method, path, data in (
            ("GET", BASE, None), ("GET", item, None), ("POST", BASE, {"name": "next", "url": URL}),
            ("POST", f"{BASE}/import", {"mcpServers": {"next": {"url": URL}}}),
            ("PATCH", item, {"name": "changed"}), ("PATCH", f"{item}/tools", {"tool": "lookup", "enabled": False}),
            ("POST", f"{item}/refresh", None), ("POST", f"{item}/call", {"tool": "lookup", "arguments": {}}),
            ("DELETE", item, None),
        ):
            res = await peer.request(method, path, json=data)
            assert res.status_code == 403, (method, path, res.text)
            assert res.json()["detail"] == "管理员已关闭 MCP"
        assert fake.connects == existing_connections and not fake.calls
        await save_settings(system_admin, mcp_enabled=True)
        assert (await peer.get(item)).json()["name"] == plugin["name"]


@pytest.mark.parametrize("phase", [
    "handshake", "next_page", "call_preparation", "call_headers", "call_body", "invalid_output", "second_page_tool",
])
async def test_peer_close_at_real_sdk_request_boundary(client, second_app, system_admin, phase):
    reached, release = asyncio.Event(), asyncio.Event()
    seen = []
    remote = FastAPI()

    @remote.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def rpc(request: Request):
        if request.method != "POST":
            return Response(status_code=405)
        message = await request.json()
        method = message.get("method")
        seen.append(method)
        if method == "initialize":
            if phase == "handshake":
                reached.set()
                await release.wait()
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "test", "version": "1"}}
        elif method == "tools/list":
            descriptor = {"name": "lookup", "inputSchema": {"type": "object"}}
            if phase == "invalid_output":
                descriptor["outputSchema"] = {"type": "object", "required": ["count"],
                                              "properties": {"count": {"type": "integer"}}}
            result = {"tools": [descriptor], "nextCursor": "next"}
            if phase == "second_page_tool":
                if message.get("params", {}).get("cursor") == "next":
                    result = {"tools": [descriptor]}
                else:
                    result["tools"] = [{"name": "first", "inputSchema": {"type": "object"}}]
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "完成"}], "isError": False}
            if phase == "invalid_output":
                result["structuredContent"] = {"count": "不合法"}
            if phase == "call_headers":
                reached.set()
                await release.wait()
        else:
            return Response(status_code=202)
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if ((phase in ("next_page", "call_preparation") and method == "tools/list")
                or (phase in ("call_body", "invalid_output", "second_page_tool") and method == "tools/call")):
            async def body():
                reached.set()
                await release.wait()
                yield json.dumps(reply).encode()
            return StreamingResponse(body(), media_type="application/json")
        return JSONResponse(reply)

    await register(client)
    async with serve(remote) as url, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://testserver", cookies=client.cookies,
    ) as peer:
        target = f"{url}/mcp"
        plugin = (await _create(peer, url=target)).json()
        item = f"{BASE}/{plugin['id']}"
        fake = FakeMcpConnector()
        fake.add(target, [tool("lookup")])
        second_app.state.task_engine.ctx.mcp_connector = mcp_client.connect if phase == "second_page_tool" else fake
        cached = await peer.post(f"{item}/refresh")
        assert cached.status_code == 200, cached.text
        if phase == "second_page_tool":
            assert [t["name"] for t in cached.json()["tools"]] == ["first", "lookup"]
            assert seen.count("tools/list") == 2
            seen.clear()
        second_app.state.task_engine.ctx.mcp_connector = mcp_client.connect
        refresh = phase in ("next_page", "handshake")
        operation = asyncio.create_task(peer.post(
            f"{item}/refresh" if refresh else f"{item}/call",
            json=None if refresh else {"tool": "lookup", "arguments": {}},
        ))
        writer = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            writer = asyncio.create_task(save_settings(system_admin, mcp_enabled=False))
            if phase == "call_headers":
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(writer), 0.15)
                release.set()
            await asyncio.wait_for(writer, 5)
            release.set()
            response = await asyncio.wait_for(operation, 10)
            if refresh or phase == "call_preparation":
                assert response.status_code == 403, response.text
                assert seen.count("tools/list") == (0 if phase == "handshake" else 1)
                assert "tools/call" not in seen
            else:
                if phase == "invalid_output":
                    assert response.status_code == 400 and "Invalid structured content" in response.json()["detail"]
                else:
                    assert response.status_code == 200 and response.json()["text"] == "完成", response.text
                assert [method for method in seen if method.startswith("tools/")] == ["tools/list", "tools/call"]
        finally:
            release.set()
            for task in (operation, writer):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)


async def test_disabled_peer_opens_no_tcp_connection(client, second_app, system_admin):
    connections = []

    async def record(reader, writer):
        connections.append(writer)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(record, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    target = f"http://127.0.0.1:{port}/mcp"
    await register(client)
    async with server, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://testserver", cookies=client.cookies,
    ) as peer:
        fake = FakeMcpConnector()
        fake.add(target, [tool("lookup")])
        second_app.state.task_engine.ctx.mcp_connector = fake
        item = f"{BASE}/{(await _create(peer, url=target)).json()['id']}"
        assert (await peer.post(f"{item}/refresh")).status_code == 200
        second_app.state.task_engine.ctx.mcp_connector = mcp_client.connect
        await save_settings(system_admin, mcp_enabled=False)
        assert (await peer.post(f"{item}/refresh")).status_code == 403
        assert (await peer.post(f"{item}/call", json={"tool": "lookup"})).status_code == 403
        assert not connections


async def test_import_close_between_items_returns_disabled_and_keeps_first(
    client, second_app, system_admin, monkeypatch,
):
    await register(client)
    entered, release = asyncio.Event(), asyncio.Event()
    create = service.create_plugin

    async def pause_after_commit(*args, **kwargs):
        value = await create(*args, **kwargs)
        entered.set()
        await release.wait()
        return value

    monkeypatch.setattr(service, "create_plugin", pause_after_commit)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
                                 cookies=client.cookies) as peer:
        pending = asyncio.create_task(peer.post(f"{BASE}/import", json={
            "mcpServers": {"first": {"url": URL}, "second": {"url": URL}},
        }))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(save_settings(system_admin, mcp_enabled=False), 5)
        finally:
            release.set()
        result = await pending
        assert result.status_code == 403, result.text
        await save_settings(system_admin, mcp_enabled=True)
        assert [p["name"] for p in (await peer.get(BASE)).json()] == ["first"]


@pytest.fixture
def slow_dns(second_app, monkeypatch):
    reached, release = asyncio.Event(), asyncio.Event()

    async def resolve(host, port):
        if host == "slow.example":
            reached.set()
            await release.wait()
        return ["93.184.216.34"]

    monkeypatch.setattr(endpoint_guard, "_resolve", resolve)
    second_app.state.settings.allow_private_ai_endpoints = False
    return reached, release


@pytest.mark.parametrize("operation_name", ["create", "update"])
async def test_plugin_url_lookup_does_not_block_setting_close(
    client, second_app, system_admin, slow_dns, operation_name,
):
    reached, release = slow_dns
    await register(client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
                                 cookies=client.cookies) as peer:
        original_url = "https://original.example/mcp"
        new_url = "https://slow.example/mcp"
        if operation_name == "update":
            response = await _create(peer, url=original_url)
            assert response.status_code == 201, response.text
            item = f"{BASE}/{response.json()['id']}"
            change = peer.patch(item, json={"url": new_url})
        else:
            change = _create(peer, url=new_url)
        pending = asyncio.create_task(change)
        try:
            await asyncio.wait_for(reached.wait(), 10)
            await asyncio.wait_for(save_settings(system_admin, mcp_enabled=False), 4)
            assert not pending.done()
            release.set()
            blocked = await asyncio.wait_for(pending, 5)
            assert blocked.status_code == 403, blocked.text
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
        await save_settings(system_admin, mcp_enabled=True)
        urls = [item["url"] for item in (await peer.get(BASE)).json()]
        assert urls == ([original_url] if operation_name == "update" else [])


@pytest.mark.parametrize("change", ["edit_target", "recreate"])
async def test_slow_url_update_keeps_concurrent_plugin_target(client, second_app, slow_dns, change):
    reached, release = slow_dns
    await register(client)
    original = (await _create(client, headers=None)).json()
    item = f"{BASE}/{original['id']}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://testserver", cookies=client.cookies,
    ) as peer:
        pending = asyncio.create_task(peer.patch(item, json={"url": "https://slow.example/mcp"}))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            if change == "edit_target":
                response = await client.patch(item, json={"url": "https://other.example/mcp"})
                assert response.status_code == 200, response.text
            else:
                assert (await client.delete(item)).status_code == 204
                response = await _create(client, headers=None)
                assert response.status_code == 201, response.text
            current = response.json()
        finally:
            release.set()
            late = await asyncio.wait_for(pending, 5)
        assert late.status_code == (409 if change == "edit_target" else 404), late.text
        assert (await client.get(f"{BASE}/{current['id']}")).json()["url"] == current["url"]


async def test_refresh_cannot_publish_on_recreated_plugin(client, second_app, monkeypatch):
    await register(client)
    original = (await _create(client, headers=None)).json()
    item = f"{BASE}/{original['id']}"
    fake = FakeMcpConnector()
    fake.add(URL, [tool("old_tool")])
    second_app.state.task_engine.ctx.mcp_connector = fake
    read_tools = service._with_connection
    reached, release = asyncio.Event(), asyncio.Event()

    async def pause_before_publish(*args, **kwargs):
        value = await read_tools(*args, **kwargs)
        reached.set()
        await release.wait()
        return value

    monkeypatch.setattr(service, "_with_connection", pause_before_publish)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://testserver", cookies=client.cookies,
    ) as peer:
        pending = asyncio.create_task(peer.post(f"{item}/refresh"))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            assert (await client.delete(item)).status_code == 204
            replacement = await _create(client, headers=None)
            assert replacement.status_code == 201, replacement.text
        finally:
            release.set()
            late = await asyncio.wait_for(pending, 5)
        assert late.status_code == 404, late.text
        row = (await client.get(f"{BASE}/{replacement.json()['id']}")).json()
        assert row["tools"] == [] and row["tools_refreshed_at"] is None and row["last_tested_at"] is None
