"""本地连接超时不代表远端未执行，必须保留结果未知的持久证据。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.models import AgentChange
from app.services.agent import mcp_tools
from app.services.agent.mcp_tools import McpSendState, _Worker
from app.services.ai_dispatch import VisibilityPolicyError, VisibilityStamp
from app.services.authorization_service import AuthenticatedActor
from app.services.mcp import client as mcp_client
from app.services.mcp.client import McpError, McpResult, McpTarget
from tests.helpers import wait_task
from tests.mcp_fakes import FakeMcpConnector, serve, tool
from tests.test_agent_mcp_skills import call, change_action, send_task, setup
from tests.test_agent_runtime import setup_agent


@pytest.mark.parametrize("result,status,expected", [
    ({"error": "已明确失败"}, "failed", 204),
    ({"error": "未知", "_external_outcome_unknown": True}, "failed", 409),
    ({"error": "未知", "_external_execution": None}, "failed", 409),
    ({"error": "未知", "_external_execution": "损坏"}, "failed", 409),
    ({"error": "未知", "_external_snapshot_mac": None}, "failed", 409),
    ({"error": "未知", "_external_claim_id": None}, "failed", 409),
    ({}, "executing", 409),
])
async def test_conversation_deletion_preserves_unresolved_external_evidence(app, client, result, status, expected):
    project, _, conversation_url = await setup_agent(client)
    conversation_id = int(conversation_url.rsplit("/", 1)[1])
    async with app.state.sessionmaker() as db:
        change = AgentChange(conversation_id=conversation_id, project_id=project["id"], tool_call_id="unresolved",
                             tool_name="external", kind="external", status=status, result=result,
                             error=result.get("error", ""))
        db.add(change)
        await db.commit()
        change_id = change.id
    response = await client.delete(conversation_url)
    assert response.status_code == expected, response.text
    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, change_id)
        if expected == 204:
            assert stored is None
        else:
            assert stored is not None and stored.result == result and stored.status == status


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_local_http_interruption_remains_unknown_while_remote_continues(app, client, fake_llm, outcome):
    received, release, remote_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    remote_app = FastAPI()

    @remote_app.post("/perform")
    async def perform():
        received.set()
        await release.wait()
        remote_finished.set()
        return {"sent": True}

    async with serve(remote_app) as remote_url:
        async def send_remote(_tool, arguments):
            try:
                async with httpx.AsyncClient(timeout=0.2 if outcome == "timeout" else 30) as remote:
                    await remote.post(f"{remote_url}/perform", json=arguments)
            except httpx.TimeoutException:
                raise McpError("连接超时") from None
            return "已发送"

        connector = FakeMcpConnector()
        app.state.task_engine.ctx.mcp_connector = connector
        _, base, conversation_url, _ = await setup(client, connector, handler=send_remote)
        fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
        task = await send_task(client, conversation_url)
        assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
        change = (await client.get(conversation_url)).json()["changes"][0]
        try:
            pending = asyncio.create_task(client.post(
                f"{base}/changes/{change['id']}/confirm", json=change_action(change),
            ))
            await asyncio.wait_for(received.wait(), 10)
            if outcome == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                response = await client.get(conversation_url)
                assert response.status_code == 200, response.text
                public = response.json()["changes"][0]
            else:
                response = await asyncio.wait_for(pending, 15)
                assert response.status_code == 409 and response.json()["code"] == "external_outcome_unknown"
                response = await client.get(conversation_url)
                assert response.status_code == 200, response.text
                public = response.json()["changes"][0]
            assert "未知" in public["error"]
            assert all(key not in response.text for key in (
                "_external_outcome_unknown", "_external_claim_id", "_external_snapshot_mac",
            ))
            async with app.state.sessionmaker() as db:
                saved = await db.get(AgentChange, change["id"])
                assert saved.status == "failed" and saved.result["_external_outcome_unknown"] is True
                assert saved.result["_external_claim_id"] and saved.result["_external_task_key"] == task["task_key"]
                assert saved.result["_external_snapshot_mac"]
            repeated = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
            assert repeated.status_code == 409 and repeated.json()["code"] == "external_outcome_unknown", repeated.text
            assert len(connector.calls) == 1
            release.set()
            await asyncio.wait_for(remote_finished.wait(), 10)
        finally:
            release.set()


async def test_local_precheck_rejection_does_not_create_unknown_remote_claim(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(client, conversation_url)
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    old = (await client.get(conversation_url)).json()["changes"][0]
    assert (await client.patch(conversation_url, json={"mcp_plugin_ids": []})).status_code == 200
    first = await client.post(f"{base}/changes/{old['id']}/confirm", json=change_action(old))
    assert first.status_code == 409, first.text
    fresh = (await client.get(conversation_url)).json()["changes"][0]
    response = await client.post(f"{base}/changes/{old['id']}/confirm", json=change_action(fresh))
    assert response.status_code == 200 and response.json()["status"] == "failed", response.text
    async with app.state.sessionmaker() as db:
        saved = await db.get(AgentChange, old["id"])
        assert "_external_outcome_unknown" not in saved.result
        assert "_external_claim_id" not in saved.result and "_external_task_key" not in saved.result
    assert connector.calls == []


async def test_tcp_disconnect_during_connection_before_tool_send_is_known_failure(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(client, conversation_url)
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    connected = asyncio.Event()

    async def disconnect(reader, writer):
        await reader.read(1)
        connected.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(disconnect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    @asynccontextmanager
    async def handshake_failure(_target, *, allow_private, request_gate=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"x")
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
        raise McpError("连接握手失败")
        yield  # pragma: no cover

    mcp_client._register_test_connector(handshake_failure)
    app.state.task_engine.ctx.mcp_connector = handshake_failure
    try:
        response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
        assert response.status_code == 200 and response.json()["status"] == "failed", response.text
        assert "握手失败" in response.json()["error"] and connected.is_set()
        async with app.state.sessionmaker() as db:
            saved = await db.get(AgentChange, change["id"])
            assert "_external_outcome_unknown" not in saved.result and "_external_claim_id" not in saved.result
        assert connector.calls == []
    finally:
        server.close()
        await server.wait_closed()


async def test_explicit_remote_error_response_is_not_a_transport_unknown(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(
        client, connector, handler=lambda _tool, _args: McpResult("服务拒绝", is_error=True),
    )
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(client, conversation_url)
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert response.status_code == 200 and response.json()["error"] == "服务拒绝", response.text
    async with app.state.sessionmaker() as db:
        saved = await db.get(AgentChange, change["id"])
        assert saved.status == "failed" and "_external_outcome_unknown" not in saved.result
        assert "_external_claim_id" not in saved.result and "_external_task_key" not in saved.result


async def test_worker_exit_after_send_is_unknown_even_without_a_result(app, client, fake_llm):
    async def dropped(_tool, _arguments):
        raise asyncio.CancelledError

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(client, connector, handler=dropped)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(client, conversation_url)
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert response.status_code == 409 and response.json()["code"] == "external_outcome_unknown", response.text
    async with app.state.sessionmaker() as db:
        saved = await db.get(AgentChange, change["id"])
        assert saved.result["_external_outcome_unknown"] is True and saved.result["_external_claim_id"]
    assert len(connector.calls) == 1


async def test_reused_connection_tracks_each_call_send_boundary_separately(app, monkeypatch):
    connector = FakeMcpConnector()
    connector.add("https://state.example/mcp", [tool("read")])
    settings = app.state.settings
    # 单独测试队列状态；真实数据库租约另由实际 HTTP 测试验证。
    lease_calls = 0

    @asynccontextmanager
    async def checked_lease(*_args):
        nonlocal lease_calls
        lease_calls += 1
        if lease_calls == 2:
            raise VisibilityPolicyError("MCP 功能已关闭", extra={"code": "mcp_disabled"})
        yield

    monkeypatch.setattr(mcp_tools, "_send_lease", checked_lease)
    identity = mcp_tools.McpSendIdentity(
        sessionmaker=app.state.sessionmaker, actor=AuthenticatedActor(1, 0), project_id=1, conversation_id=1,
        task_id=1, task_key=str(uuid4()), queued_at=datetime.now(UTC), execution_key=None,
        change_id=1, change_key=str(uuid4()), claim_id=str(uuid4()), tool_call_id="call",
        started_at=datetime.now(UTC), stamp=VisibilityStamp(1, 0, 0),
    )
    worker = _Worker(connector, McpTarget("https://state.example/mcp", "sse", {}), False, settings, app.state.cipher,
                     identity=identity)
    first, second = McpSendState(), McpSendState()
    try:
        await worker.call("read", {}, first, identity=identity, resolved=object())
        assert first.started
        with pytest.raises(VisibilityPolicyError) as failure:
            await worker.call("read", {}, second, identity=identity, resolved=object())
        assert failure.value.extra["code"] == "mcp_disabled" and not second.started
        assert lease_calls == 2 and len(connector.calls) == 1
    finally:
        await worker.close()
