"""以真实 MCP SDK 与本地 TCP 验证单次外部调用的物理发送门禁。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.models import AgentChange, AIConfig, McpPlugin, Project, Task, User
from app.services.agent.mcp_tools import McpSendState
from app.services.mcp import client as mcp_client
from tests.helpers import wait_task
from tests.mcp_fakes import FakeMcpConnector, serve
from tests.test_agent_mcp_skills import call, change_action, send_task, setup
from tests.test_system_settings import save_settings


async def test_send_gate_rejects_duplicate_post_and_pending_cancellation():
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def lease():
        entered.set()
        await release.wait()
        yield

    def request():
        return httpx.Request("POST", "http://example.test/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "send", "arguments": {"to": "编辑部"}},
        })

    gate, state = mcp_client.McpRequestGate(), McpSendState()
    future = asyncio.get_running_loop().create_future()
    gate.arm("send", {"to": "编辑部"}, state, lease, future=future)
    pending = asyncio.create_task(gate.before_request(request()))
    await entered.wait()
    future.cancel()
    release.set()
    with pytest.raises(mcp_client.McpError):
        await pending
    assert not state.started
    await gate.aclose()

    gate, state = mcp_client.McpRequestGate(), McpSendState()
    future = asyncio.get_running_loop().create_future()
    gate.arm("send", {"to": "编辑部"}, state, lease, future=future)
    first = request()
    await gate.before_request(first)
    await gate.after_headers(httpx.Response(200, request=first))
    assert state.started
    with pytest.raises(mcp_client.McpError):
        await gate.before_request(request())
    await gate.aclose()
    future.cancel()


async def test_unregistered_connector_cannot_be_used_to_bypass_send_gate(app, client, fake_llm):
    fake = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake
    _, base, conversation_url, _ = await setup(client, fake)
    invoked = False

    @asynccontextmanager
    async def untrusted(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        yield object()

    app.state.task_engine.ctx.mcp_connector = untrusted
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(client, conversation_url)
    assert (await wait_task(client, task, within=20, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert response.status_code == 409, response.text
    assert not invoked and fake.calls == []


async def test_local_mcp_server_supports_real_sdk_protocol():
    remote = FastAPI()

    @remote.post("/mcp")
    async def rpc(request: Request):
        message = await request.json()
        if message.get("method") == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "test", "version": "1"}}
        elif message.get("method") == "tools/call":
            result = {"content": [{"type": "text", "text": "完成"}], "isError": False}
        elif message.get("method") == "tools/list":
            result = {"tools": [{"name": "send", "inputSchema": {"type": "object", "properties": {}}}]}
        else:
            return Response(status_code=202)
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})

    async with serve(remote) as url:
        async with mcp_client.connect(mcp_client.McpTarget(f"{url}/mcp", "streamable_http"),
                                      allow_private=True) as connection:
            result = await asyncio.wait_for(connection.call_tool("send", {"to": "编辑部"}), 5)
            assert result.text == "完成"


@pytest.mark.parametrize("revoked_first", [False, True])
async def test_sse_sdk_uses_the_same_real_send_gate(app, client, fake_llm, revoked_first):
    initialized, release_init = asyncio.Event(), asyncio.Event()
    messages: asyncio.Queue = asyncio.Queue()
    calls = []
    remote = FastAPI()

    @remote.get("/sse")
    async def stream():
        async def events():
            yield "event: endpoint\ndata: /messages\n\n"
            while True:
                item = await messages.get()
                yield f"event: message\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    @remote.post("/messages")
    async def rpc(request: Request):
        message = await request.json()
        method = message.get("method")
        if method == "initialize":
            initialized.set()
            if revoked_first:
                await release_init.wait()
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "test", "version": "1"}}
        elif method == "tools/call":
            calls.append(message)
            result = {"content": [{"type": "text", "text": "完成"}], "isError": False}
        elif method == "tools/list":
            result = {"tools": [{"name": "send", "inputSchema": {"type": "object", "properties": {}}}]}
        else:
            return Response(status_code=202)
        await messages.put({"jsonrpc": "2.0", "id": message["id"], "result": result})
        return Response(status_code=202)

    fake = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake
    _, base, conversation_url, plugin = await setup(client, fake)
    owner = (await client.get("/api/v1/auth/me")).json()
    app.state.settings.allow_private_ai_endpoints = True
    async with serve(remote) as remote_url:
        async with app.state.sessionmaker() as db:
            saved = await db.get(McpPlugin, plugin["id"])
            saved.url, saved.transport = f"{remote_url}/sse", "sse"
            await db.commit()
        app.state.task_engine.ctx.mcp_connector = mcp_client.connect
        fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
        task = await send_task(client, conversation_url)
        assert (await wait_task(client, task, within=20, statuses=("paused", "failed")))["status"] == "paused"
        change = (await client.get(conversation_url)).json()["changes"][0]
        pending = asyncio.create_task(client.post(
            f"{base}/changes/{change['id']}/confirm", json=change_action(change),
        ))
        try:
            await asyncio.wait_for(initialized.wait(), 10)
            if revoked_first:
                async with app.state.sessionmaker() as db:
                    (await db.get(User, owner["id"])).token_version += 1
                    await db.commit()
            release_init.set()
            response = await asyncio.wait_for(pending, 15)
            if revoked_first:
                assert response.status_code in (401, 409), response.text
                assert calls == []
            else:
                assert response.status_code == 200 and response.json()["status"] == "executed", response.text
                assert len(calls) == 1
        finally:
            release_init.set()
            if not pending.done():
                pending.cancel()


@pytest.mark.parametrize("case", ["revoked_first", "disabled_first", "stale_disabled", "stale_disabled_before_confirm",
                                  "task_changed", "change_changed", "result_tampered", "model_changed",
                                  "send_first", "disabled_while_headers", "disabled_after_headers",
                                  "success", "body_blocked", "task_requeued", "task_reexecuted", "cancel_requeued",
                                  "timeout", "cancel", "disconnect"])
async def test_sdk_tool_send_is_gated_at_real_http_boundary(app, client, system_admin, fake_llm, monkeypatch, case):
    initialized, release_init, received, release_headers, body_started, release_body = (
        asyncio.Event() for _ in range(6)
    )
    calls = []
    methods = []
    remote = FastAPI()

    @remote.post("/mcp")
    async def rpc(request: Request):
        message = await request.json()
        method = message.get("method")
        methods.append(method)
        if method == "initialize":
            initialized.set()
            if case in ("revoked_first", "disabled_first", "stale_disabled", "task_changed", "change_changed",
                        "result_tampered", "model_changed"):
                await release_init.wait()
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }}, headers={"mcp-session-id": "test-session"})
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "tools": [{"name": "send", "inputSchema": {"type": "object", "properties": {}}}],
            }})
        if method == "tools/call":
            calls.append(message)
            received.set()
            if case == "disconnect":
                raise RuntimeError("连接中止")
            if case in ("send_first", "disabled_while_headers", "disabled_after_headers", "body_blocked",
                        "task_requeued", "task_reexecuted", "cancel_requeued"):
                if case in ("send_first", "disabled_while_headers"):
                    await release_headers.wait()

                async def body():
                    body_started.set()
                    await release_body.wait()
                    yield json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "content": [{"type": "text", "text": "远端已执行"}], "isError": False,
                    }}, ensure_ascii=False).encode()
                return StreamingResponse(body(), media_type="application/json")
            await release_headers.wait()
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "content": [{"type": "text", "text": "远端已执行"}], "isError": False,
            }})
        return Response(status_code=202)

    @remote.get("/mcp")
    async def get_stream():
        return Response(status_code=405)

    fake = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake
    project, base, conversation_url, plugin = await setup(client, fake)
    owner = (await client.get("/api/v1/auth/me")).json()
    app.state.settings.allow_private_ai_endpoints = True
    async with serve(remote) as remote_url:
        async with app.state.sessionmaker() as db:
            (await db.get(McpPlugin, plugin["id"])).url = f"{remote_url}/mcp"
            await db.commit()
        app.state.task_engine.ctx.mcp_connector = mcp_client.connect
        if case == "timeout":
            monkeypatch.setattr(mcp_client, "CALL_TIMEOUT", 0.5)
        fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
        task = await send_task(client, conversation_url)
        assert (await wait_task(client, task, within=20, statuses=("paused", "failed")))["status"] == "paused"
        change = (await client.get(conversation_url)).json()["changes"][0]
        if case == "stale_disabled_before_confirm":
            app.state.settings.mcp_enabled = False
        pending = asyncio.create_task(client.post(
            f"{base}/changes/{change['id']}/confirm", json=change_action(change),
        ))
        writer = None
        try:
            if case == "stale_disabled_before_confirm":
                reached = asyncio.create_task(initialized.wait())
                done, _ = await asyncio.wait((reached, pending), timeout=10, return_when=asyncio.FIRST_COMPLETED)
                if pending in done and not initialized.is_set():
                    response = await pending
                    pytest.fail(f"持久设置已开启，确认在发送前误拒：{response.status_code} {response.text}")
                reached.cancel()
            await asyncio.wait_for(initialized.wait(), 10)
            if case in ("stale_disabled", "stale_disabled_before_confirm"):
                # 另一实例已恢复持久开关，但本进程的旧内存值还未刷新；实际发送应以持久事实判定。
                app.state.settings.mcp_enabled = False
                release_init.set()
                await asyncio.wait_for(received.wait(), 10)
                release_headers.set()
                response = await asyncio.wait_for(pending, 10)
                assert response.status_code == 200 and response.json()["status"] == "executed", response.text
                assert len(calls) == 1
            elif case in ("revoked_first", "disabled_first", "task_changed", "change_changed", "result_tampered",
                          "model_changed"):
                if case == "disabled_first":
                    await save_settings(system_admin, mcp_enabled=False)
                else:
                    async with app.state.sessionmaker() as db:
                        if case == "revoked_first":
                            (await db.get(User, owner["id"])).token_version += 1
                        elif case == "task_changed":
                            current = await db.get(Task, task["id"])
                            current.input = {**current.input, "unexpected": "已被替换"}
                        elif case == "change_changed":
                            (await db.get(AgentChange, change["id"])).arguments = {"to": "另一目标"}
                        elif case == "result_tampered":
                            current = await db.get(AgentChange, change["id"])
                            current.result = {**current.result, "_external_outcome_unknown": None}
                        else:
                            current = await db.get(Task, task["id"])
                            config = await db.get(AIConfig, current.input["_project_model"]["config_id"])
                            fields = {name: getattr(config, name) for name in (
                                "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
                                "is_default", "thinking_level", "context_window_k", "max_output_k", "created_at",
                                "updated_at",
                            )}
                            config_id = config.id
                            await db.delete(config)
                            await db.flush()
                            db.add(AIConfig(id=config_id, instance_key=str(uuid4()), **fields))
                        await db.commit()
                release_init.set()
                if case == "result_tampered":
                    release_headers.set()  # 红灯实现会错误发送并拿到明确响应，不能以超时掩盖。
                response = await asyncio.wait_for(pending, 10)
                assert response.status_code in (401, 403, 409), response.text
                if case == "disabled_first":
                    assert response.status_code == 409 and response.json()["code"] == "mcp_disabled"
                assert calls == []
                if case in ("task_changed", "change_changed", "result_tampered"):
                    async with app.state.sessionmaker() as db:
                        saved = await db.get(AgentChange, change["id"])
                        assert saved.status == "executing" and saved.result["_external_claim_id"]
                        if case == "result_tampered":
                            assert "_external_outcome_unknown" in saved.result
            else:
                await asyncio.wait_for(received.wait(), 10)
                if case == "send_first":
                    async def revoke():
                        async with app.state.sessionmaker() as db:
                            (await db.get(User, owner["id"])).token_version += 1
                            await db.commit()
                    writer = asyncio.create_task(revoke())
                    await asyncio.sleep(0.1)
                    assert not writer.done()  # 真实请求尚未收到响应头，认证物理门禁仍持有。
                    release_headers.set()
                    await asyncio.wait_for(writer, 10)
                    release_body.set()
                    response = await asyncio.wait_for(pending, 10)
                    assert response.status_code in (401, 409), response.text
                elif case == "disabled_while_headers":
                    writer = asyncio.create_task(save_settings(system_admin, mcp_enabled=False))
                    await asyncio.sleep(0.1)
                    assert not writer.done()  # 设置独占门禁等待本次真实请求的响应头。
                    release_headers.set()
                    await asyncio.wait_for(writer, 10)
                    release_body.set()
                    response = await asyncio.wait_for(pending, 10)
                    assert response.status_code == 200 and response.json()["status"] == "executed", response.text
                elif case == "disabled_after_headers":
                    await asyncio.wait_for(body_started.wait(), 10)
                    await asyncio.wait_for(save_settings(system_admin, mcp_enabled=False), 5)
                    assert not pending.done()  # 正文等待不再持设置门禁。
                    release_body.set()
                    response = await asyncio.wait_for(pending, 10)
                    assert response.status_code == 200 and response.json()["status"] == "executed", response.text
                elif case == "success":
                    release_headers.set()
                    try:
                        response = await asyncio.wait_for(pending, 10)
                    except TimeoutError:
                        pytest.fail(f"等待工具结果超时，方法序列：{methods}")
                    assert response.status_code == 200 and response.json()["status"] == "executed", response.text
                elif case == "body_blocked":
                    await asyncio.wait_for(body_started.wait(), 10)

                    async def write_after_headers():
                        async with app.state.sessionmaker() as db:
                            (await db.get(Project, project["id"])).theme = "响应正文未到时的并发写入"
                            await db.commit()

                    # 响应头已到而正文仍阻塞，写事务必须能提交，不能持锁等完整响应。
                    await asyncio.wait_for(write_after_headers(), 5)
                    assert not pending.done()
                    release_body.set()
                    response = await asyncio.wait_for(pending, 10)
                    assert response.status_code == 200 and response.json()["status"] == "executed", response.text
                elif case in ("task_requeued", "task_reexecuted", "cancel_requeued"):
                    # 真实请求已到远端且响应头已释放门禁。复用 Task UUID 的另一执行轮次
                    # 不能接受旧请求的结果，即使所有其他快照字段完全相同。
                    await asyncio.wait_for(body_started.wait(), 10)
                    async with app.state.sessionmaker() as db:
                        current = await db.get(Task, task["id"])
                        saved = await db.get(AgentChange, change["id"])
                        original_claim = dict(saved.result)
                        if case in ("task_requeued", "cancel_requeued"):
                            current.queued_at += timedelta(seconds=1)
                        else:
                            current.execution_key = str(uuid4())
                        await db.commit()
                    if case == "cancel_requeued":
                        pending.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                    else:
                        release_body.set()
                        response = await asyncio.wait_for(pending, 10)
                        assert response.status_code == 409 and response.json()["code"] == "external_outcome_unknown"
                elif case == "cancel":
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    response = await asyncio.wait_for(pending, 10)
                    assert response.status_code == 409 and response.json()["code"] == "external_outcome_unknown"
                assert len(calls) == 1
                async with app.state.sessionmaker() as db:
                    saved = await db.get(AgentChange, change["id"])
                    if case in ("timeout", "cancel", "disconnect"):
                        assert saved.status == "failed" and saved.result["_external_outcome_unknown"] is True
                    elif case == "send_first":
                        assert saved.status == "failed"
                    elif case in ("task_requeued", "task_reexecuted", "cancel_requeued"):
                        assert saved.status == "executing" and saved.result == original_claim
                    else:
                        assert saved.status == "executed"
                    if case in ("timeout", "cancel", "disconnect"):
                        # 即使远端仍未结束，关闭传输后本地锁也必须释放。
                        (await db.get(Project, project["id"])).theme = "本地锁已释放"
                        await db.commit()
                if case in ("timeout", "cancel", "disconnect"):
                    repeated = await client.post(
                        f"{base}/changes/{change['id']}/confirm", json=change_action(change),
                    )
                    assert repeated.status_code == 409 and repeated.json()["code"] == "external_outcome_unknown"
                    assert len(calls) == 1
        finally:
            release_init.set()
            release_headers.set()
            release_body.set()
            if not pending.done():
                pending.cancel()
            if writer is not None and not writer.done():
                await asyncio.wait_for(writer, 10)
