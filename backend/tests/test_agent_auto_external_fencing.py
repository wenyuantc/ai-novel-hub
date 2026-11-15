"""自动外部工具需要持久占用，已发送且结果未知时不能被模型再次执行。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import inspect, select, update

from app.core.errors import ConflictError
from app.models import AgentChange, AgentConversation, AgentMessage, AIConfig, McpPlugin, Project, Task, User
from app.services import ai_config_service
from app.services.agent import mcp_tools, runner, service, transactions
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.mcp import client as mcp_client
from app.services.mcp.client import McpError, McpResult, _register_test_connector
from app.services.task_engine.execution_identity import from_queued_task, matches_running, same_execution
from tests.helpers import Gate, create_project, register, task_params, wait_task
from tests.mcp_fakes import FakeMcpConnector, serve
from tests.test_agent_mcp_skills import call, send_task, setup


@pytest.fixture(autouse=True)
def simulated_transport(monkeypatch):
    """仅为 claim 协议测试替换门禁，不把本地替身视为真实发送证明。"""
    original = runner._require_auto_external_transport
    monkeypatch.setattr(runner, "_require_auto_external_transport", lambda: None)
    return original


async def test_unproved_auto_transport_fails_closed(app, client, fake_llm, monkeypatch, simulated_transport):
    monkeypatch.setattr(runner, "_require_auto_external_transport", simulated_transport)
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}})
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    task = await send_task(client, url)
    result = await wait_task(client, task, statuses=("succeeded", "failed"))
    assert result["status"] == "failed" and connector.calls == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AgentChange.id).where(AgentChange.turn_task_id == task["id"])) is None


@pytest.mark.parametrize("case", ["preference_revoked", "token_revoked", "admin_intent", "claim_tampered",
                                  "proof_bool", "model_replaced", "send_first", "admin_send_first", "success",
                                  "nonreadonly", "timeout", "disconnect", "cancel"])
async def test_real_mcp_sdk_checks_auto_confirm_at_post(app, client, fake_llm, monkeypatch, case):
    initialized, release, received, release_result = (asyncio.Event() for _ in range(4))
    revoked = asyncio.Event()
    calls = []
    remote = FastAPI()

    @remote.post("/mcp")
    async def rpc(request: Request):
        message = await request.json()
        method = message.get("method")
        if method == "initialize":
            initialized.set()
            await release.wait()
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }}, headers={"mcp-session-id": "auto-test"})
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "tools": [{"name": "lookup", "inputSchema": {
                    "type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"],
                }}, {"name": "send", "inputSchema": {
                    "type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"],
                }}],
            }})
        if method == "tools/call":
            calls.append(message)
            received.set()
            if case in ("timeout", "send_first", "admin_send_first", "cancel"):
                await release_result.wait()
            if case == "disconnect":
                raise RuntimeError("模拟远端断开")
            return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": {
                "content": [{"type": "text", "text": "已执行"}], "isError": False,
            }})
        return Response(status_code=202)

    @remote.get("/mcp")
    async def get_stream():
        return Response(status_code=405)

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    tool_name = "send" if case == "nonreadonly" else "lookup"
    _, _, url, plugin = await setup(client, connector, prefs={tool_name: {"auto_confirm": True}})
    app.state.settings.allow_private_ai_endpoints = True
    if case == "proof_bool":
        owner = (await client.get("/api/v1/auth/me")).json()
        async with app.state.sessionmaker() as db:
            (await db.get(User, owner["id"])).token_version = 1
            await db.commit()
        login = await client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
        assert login.status_code == 200
    async with serve(remote) as remote_url:
        async with app.state.sessionmaker() as db:
            (await db.get(McpPlugin, plugin["id"])).url = f"{remote_url}/mcp"
            await db.commit()
        app.state.task_engine.ctx.mcp_connector = mcp_client.connect
        if case == "timeout":
            monkeypatch.setattr(mcp_client, "CALL_TIMEOUT", 0.5)
        if case in ("send_first", "admin_send_first"):
            original_lock = runner._lock_execution

            async def hold_result_until_revoked(db, job):
                if received.is_set():
                    await asyncio.wait_for(revoked.wait(), 15)
                await original_lock(db, job)

            monkeypatch.setattr(runner, "_lock_execution", hold_result_until_revoked)
        arguments = {"to": "编辑部"} if case == "nonreadonly" else {"word": "请求"}
        fake_llm.push_tools([call(f"mcp__dict__{tool_name}", arguments)])
        if case in ("success", "nonreadonly"):
            fake_llm.push_tools([("text", {"text": "已完成"})])
        elif case in ("timeout", "disconnect", "cancel"):
            fake_llm.push_tools([call("mcp__dict__lookup", {"word": "不可重发"}, "new-call")])
        task = await send_task(client, url)
        writer = None
        try:
            try:
                await asyncio.wait_for(initialized.wait(), 10)
            except TimeoutError:
                snapshot = await client.get(f"/api/v1/tasks/{task['id']}", params=task_params(task))
                pytest.fail(f"MCP 初始化未到达本机服务：{snapshot.text}")
            if case == "preference_revoked":
                update_pref = await client.patch(f"/api/v1/mcp-plugins/{plugin['id']}/tools", json={
                    "tool": "lookup", "auto_confirm": False,
                })
                assert update_pref.status_code == 200, update_pref.text
            if case == "token_revoked":
                async with app.state.sessionmaker() as db:
                    actor = await db.get(User, (await db.get(Task, task["id"])).user_id)
                    actor.token_version += 1
                    await db.commit()
            if case == "admin_intent":
                async with app.state.sessionmaker() as db:
                    row = await db.get(Task, task["id"])
                    row.admin_cancel_operation_id = str(uuid4())
                    row.admin_cancel_requested_at = datetime.now(UTC)
                    await db.commit()
            if case == "claim_tampered":
                async with app.state.sessionmaker() as db:
                    row = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
                    row.arguments = {"word": "伪造"}
                    await db.commit()
            if case == "proof_bool":
                async with app.state.sessionmaker() as db:
                    row = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
                    assert row.result["_external_execution"]["actor_token_version"] == 1
                    # 显式 UPDATE：ORM 会认为 True == 1 而不发出写入，篡改必须真实落库。
                    await db.execute(update(AgentChange).where(AgentChange.id == row.id).values(result={
                        **row.result, "_external_execution": {
                            **row.result["_external_execution"], "actor_token_version": True,
                        },
                    }))
                    await db.commit()
            if case == "model_replaced":
                async with app.state.sessionmaker() as db:
                    row = await db.get(Task, task["id"])
                    config = await db.get(AIConfig, row.input["ai_config_id"])
                    fields = {column.key: getattr(config, column.key) for column in AIConfig.__table__.columns}
                    fields["instance_key"] = str(uuid4())
                    await db.delete(config)
                    await db.flush()
                    db.add(AIConfig(**fields))
                    await db.commit()
            release.set()
            if case in ("send_first", "admin_send_first"):
                await asyncio.wait_for(received.wait(), 10)

                async def revoke():
                    async with app.state.sessionmaker() as db:
                        row = await db.get(Task, task["id"])
                        if case == "send_first":
                            actor = await db.get(User, row.user_id)
                            actor.token_version += 1
                        else:
                            row.admin_cancel_operation_id = str(uuid4())
                            row.admin_cancel_requested_at = datetime.now(UTC)
                        await db.commit()
                        revoked.set()

                writer = asyncio.create_task(revoke())
                await asyncio.sleep(0.1)
                assert not writer.done()
                release_result.set()
                await asyncio.wait_for(writer, 10)
            if case == "cancel":
                await asyncio.wait_for(received.wait(), 10)
                cancellation = asyncio.create_task(client.post(
                    f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task),
                ))
                await asyncio.sleep(0.1)
                release_result.set()
                assert (await asyncio.wait_for(cancellation, 15)).status_code == 200
            if case in ("token_revoked", "admin_intent", "send_first", "admin_send_first"):
                async with asyncio.timeout(15):
                    while True:
                        async with app.state.sessionmaker() as db:
                            row = await db.get(Task, task["id"])
                            if row.status in ("succeeded", "failed", "cancelled"):
                                result = {"status": row.status}
                                break
                        await asyncio.sleep(0.02)
            else:
                result = await wait_task(client, task, within=15, statuses=("succeeded", "failed", "cancelled"))
            async with app.state.sessionmaker() as db:
                change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
                if case in ("claim_tampered", "proof_bool"):
                    assert result["status"] == "failed" and calls == []
                    assert change.status == "executing"
                    if case == "claim_tampered":
                        assert change.arguments == {"word": "伪造"}
                    else:
                        assert change.result["_external_execution"]["actor_token_version"] is True
                elif case in ("preference_revoked", "token_revoked", "admin_intent", "model_replaced"):
                    assert result["status"] in ("failed", "cancelled") and calls == []
                    assert change.status == "failed" and change.result == {"error": service.EXTERNAL_NOT_SENT}
                elif case in ("success", "nonreadonly"):
                    assert result["status"] == "succeeded" and len(calls) == 1 and change is None
                    assert await db.scalar(select(AgentMessage.id).where(
                        AgentMessage.turn_task_id == task["id"], AgentMessage.role == "tool",
                    )) is not None
                else:
                    assert result["status"] in ("failed", "cancelled") and len(calls) == 1
                    assert change.status == "failed" and change.result["_external_outcome_unknown"] is True
                    assert len(fake_llm.tool_calls) == 1
        finally:
            release.set()
            release_result.set()
            if writer is not None and not writer.done():
                await asyncio.wait_for(writer, 10)


async def test_auto_external_timeout_keeps_unknown_and_blocks_retry(app, client, fake_llm):
    received, release = asyncio.Event(), asyncio.Event()
    remote_calls = []
    remote = FastAPI()

    @remote.post("/perform")
    async def perform(payload: dict):
        remote_calls.append(payload)
        received.set()
        await release.wait()
        return {"completed": True}

    async with serve(remote) as remote_url:
        async def invoke(_name, arguments):
            try:
                async with httpx.AsyncClient(timeout=0.15) as session:
                    await session.post(f"{remote_url}/perform", json=arguments)
            except httpx.TimeoutException:
                raise McpError("远端调用超时") from None
            return "完成"

        connector = FakeMcpConnector()
        app.state.task_engine.ctx.mcp_connector = connector
        _, _, conversation, _ = await setup(
            client, connector, prefs={"lookup": {"auto_confirm": True}}, handler=invoke,
        )
        fake_llm.push_tools([call("mcp__dict__lookup", {"word": "首次"}, "first")])
        fake_llm.push_tools([call("mcp__dict__lookup", {"word": "重复"}, "again")])
        fake_llm.push_tools([("text", {"text": "结束"})])
        try:
            task = await send_task(client, conversation)
            try:
                await asyncio.wait_for(received.wait(), 10)
            except TimeoutError:
                failed = await client.get(f"/api/v1/tasks/{task['id']}", params=task_params(task))
                pytest.fail(f"外部调用未到达本机服务：{failed.text}")
            ended = await wait_task(client, task, within=15, statuses=("failed", "succeeded"))
            assert ended["status"] == "failed" and len(remote_calls) == 1
            async with app.state.sessionmaker() as db:
                change = await db.scalar(select(AgentChange).where(
                    AgentChange.turn_task_id == task["id"], AgentChange.tool_call_id == "first",
                ))
                assert change.status == "failed" and change.result["_external_outcome_unknown"] is True
                assert change.result["_external_task_key"] == task["task_key"]
                assert change.result["_external_claim_id"]
            retry = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
            assert retry.status_code == 409 and len(remote_calls) == 1
            new_turn = await client.post(conversation + "/messages", json={"content": "再试一次"})
            assert new_turn.status_code == 409 and len(remote_calls) == 1
            public = await client.get(conversation)
            for key in ("_external_execution", "_external_snapshot_mac", "_external_claim_id",
                        "_agent_actor_token_version", "_task_actor_token_version"):
                assert key not in public.text
            assert task["task_key"] not in str(public.json()["changes"][0]["result"])
        finally:
            release.set()


async def test_auto_external_cancel_after_send_keeps_unknown(app, client, fake_llm):
    entered, release = asyncio.Event(), asyncio.Event()
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector

    async def call_remote(_name, _args):
        entered.set()
        await release.wait()
        return "已执行"

    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}}, handler=call_remote)
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    fake_llm.push_tools([("text", {"text": "不能继续"})])
    task = await send_task(client, url)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        pending = asyncio.create_task(client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task)))
        await asyncio.sleep(0.1)
        release.set()
        assert (await asyncio.wait_for(pending, 15)).status_code == 200
        assert (await wait_task(client, task, statuses=("cancelled", "failed")))["status"] == "cancelled"
        async with app.state.sessionmaker() as db:
            change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
            assert change.status == "failed" and change.result["_external_outcome_unknown"] is True
            assert change.result["_external_execution"]["execution_key"]
        assert (await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))).status_code == 409
        assert len(connector.calls) == 1 and len(fake_llm.tool_calls) == 1
    finally:
        release.set()


async def test_cancel_before_send_is_known_and_can_retry(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}})
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def slow_connect(target, *, allow_private, request_gate):
        entered.set()
        await release.wait()
        async with connector(target, allow_private=allow_private, request_gate=request_gate) as conn:
            yield conn

    _register_test_connector(slow_connect)
    app.state.task_engine.ctx.mcp_connector = slow_connect
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    fake_llm.push_tools([("text", {"text": "本次未发送"})])
    task = await send_task(client, url)
    try:
        await asyncio.wait_for(entered.wait(), 10)
        pending = asyncio.create_task(client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task)))
        await asyncio.sleep(0.1)
        release.set()
        assert (await asyncio.wait_for(pending, 15)).status_code == 200
        async with app.state.sessionmaker() as db:
            change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
            assert change.status == "failed" and change.result == {"error": service.EXTERNAL_NOT_SENT}
        assert connector.calls == []
        response = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
        assert response.status_code == 202, response.text
        assert (await wait_task(client, response.json(), statuses=("succeeded", "failed")))["status"] == "succeeded"
        assert connector.calls == []
    finally:
        release.set()


async def test_result_commit_ack_loss_uses_persisted_result_without_resending(app, client, fake_llm, monkeypatch):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}})
    original_commit = transactions.AgentAuditCoordinator.commit
    injected = False

    async def lose_ack(self, db, factory):
        nonlocal injected
        writing_tool = any(isinstance(row, AgentMessage) and row.role == "tool" for row in db.new)
        await original_commit(self, db, factory)
        if writing_tool and not injected:
            injected = True
            raise RuntimeError("模拟提交后响应丢失")

    monkeypatch.setattr(transactions.AgentAuditCoordinator, "commit", lose_ack)
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    fake_llm.push_tools([("text", {"text": "已收到结果"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("succeeded", "failed")))["status"] == "succeeded"
    assert injected and len(connector.calls) == 1
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AgentChange.id).where(AgentChange.turn_task_id == task["id"])) is None
        assert await db.scalar(select(AgentMessage.id).where(
            AgentMessage.turn_task_id == task["id"], AgentMessage.role == "tool",
        )) is not None


async def test_result_commit_rejection_keeps_unknown_and_blocks_retry(app, client, fake_llm, monkeypatch):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}})
    original_commit = transactions.AgentAuditCoordinator.commit
    injected = False

    async def reject_commit(self, db, factory):
        nonlocal injected
        if not injected and any(isinstance(row, AgentMessage) and row.role == "tool" for row in db.new):
            injected = True
            raise RuntimeError("模拟提交未完成")
        await original_commit(self, db, factory)

    monkeypatch.setattr(transactions.AgentAuditCoordinator, "commit", reject_commit)
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    fake_llm.push_tools([("text", {"text": "不能继续"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("succeeded", "failed")))["status"] == "failed"
    assert injected and len(connector.calls) == 1 and len(fake_llm.tool_calls) == 1
    async with app.state.sessionmaker() as db:
        change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
        assert change.status == "failed" and change.result["_external_outcome_unknown"] is True
    assert (await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))).status_code == 409


async def test_remote_call_has_persisted_identity_without_holding_write_lock(app, client, fake_llm):
    entered, release = asyncio.Event(), asyncio.Event()
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector

    async def remote(_name, _args):
        entered.set()
        await release.wait()
        return "完成"

    project, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}}, handler=remote)
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    fake_llm.push_tools([("text", {"text": "完成"})])
    task = await send_task(client, url)
    try:
        await asyncio.wait_for(entered.wait(), 10)

        async def competing_writer():
            async with app.state.sessionmaker() as db:
                claim = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
                running = await db.get(Task, task["id"])
                assert claim.status == "executing" and claim.result["_external_task_key"] == task["task_key"]
                assert claim.result["_external_execution"]["execution_key"] == running.execution_key
                assert claim.result["_external_claim_id"]
                await db.execute(update(Project).where(Project.id == project["id"]).values(
                    updated_at=Project.updated_at,
                ))
                await db.commit()

        await asyncio.wait_for(competing_writer(), 2)
        release.set()
        assert (await wait_task(client, task, statuses=("succeeded", "failed")))["status"] == "succeeded"
    finally:
        release.set()


async def test_explicit_remote_error_is_a_known_result(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}},
                               handler=lambda _name, _args: McpResult("明确拒绝", is_error=True))
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    fake_llm.push_tools([("text", {"text": "请求被拒绝"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("succeeded", "failed")))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AgentChange.id).where(AgentChange.turn_task_id == task["id"])) is None
        result = await db.scalar(select(AgentMessage.content).where(
            AgentMessage.turn_task_id == task["id"], AgentMessage.role == "tool",
        ))
        assert "明确拒绝" in result
    assert len(connector.calls) == 1


@pytest.mark.parametrize("revocation", ["disabled", "token"])
async def test_permission_loss_after_send_preserves_unknown(app, client, fake_llm, revocation):
    entered, release = asyncio.Event(), asyncio.Event()
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector

    async def remote(_name, _args):
        entered.set()
        await release.wait()
        return "已执行"

    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}}, handler=remote)
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    fake_llm.push_tools([("text", {"text": "不能继续"})])
    task = await send_task(client, url)
    user_id, old_token_version = None, None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with app.state.sessionmaker() as db:
            user_id = (await db.get(Task, task["id"])).user_id
            old_token_version = (await db.get(User, user_id)).token_version
            values = {"is_active": False} if revocation == "disabled" else {"token_version": old_token_version + 1}
            await db.execute(update(User).where(User.id == user_id).values(**values))
            await db.commit()
        release.set()
        async with asyncio.timeout(10):
            while True:
                async with app.state.sessionmaker() as db:
                    row = await db.get(Task, task["id"])
                    if row.status == "failed":
                        change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
                        assert change.result["_external_outcome_unknown"] is True
                        break
                await asyncio.sleep(0.02)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user_id).values(
                is_active=True, token_version=old_token_version,
            ))
            await db.commit()
        assert (await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))).status_code == 409
        assert len(connector.calls) == 1 and len(fake_llm.tool_calls) == 1
    finally:
        release.set()
        if user_id is not None:
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == user_id).values(
                    is_active=True, token_version=old_token_version,
                ))
                await db.commit()


async def test_permission_loss_before_send_is_recorded_as_not_sent(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}})

    @asynccontextmanager
    async def rejected_connect(_target, *, allow_private, request_gate):
        raise VisibilityPolicyError("授权已变化")
        yield  # pragma: no cover

    _register_test_connector(rejected_connect)
    app.state.task_engine.ctx.mcp_connector = rejected_connect
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("succeeded", "failed")))["status"] == "failed"
    async with app.state.sessionmaker() as db:
        change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
        assert change.status == "failed" and change.result == {"error": service.EXTERNAL_NOT_SENT}
    assert connector.calls == []


async def test_queued_retry_with_new_uuid_rechecks_unknown_before_dispatch(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑"})])
    original = await send_task(client, url)
    assert (await wait_task(client, original, statuses=("paused", "failed")))["status"] == "paused"
    cancelled = await client.post(f"/api/v1/tasks/{original['id']}/cancel", params=task_params(original))
    assert cancelled.status_code == 200
    engine = app.state.task_engine
    await engine.stop()
    response = await client.post(f"/api/v1/tasks/{original['id']}/retry", params=task_params(original))
    assert response.status_code == 202, response.text
    retried = response.json()
    assert retried["task_key"] != original["task_key"]
    async with app.state.sessionmaker() as db:
        old_change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == original["id"]))
        old_change.status = "failed"
        old_change.error = service.EXTERNAL_INTERRUPTED
        old_change.result = service._unknown_external_result(str(uuid4()), original["task_key"])
        await db.commit()
        change_id = old_change.id
        queued = await db.get(Task, retried["id"])
        engine._running_rounds[retried["id"]], engine._stopping = queued.queued_at, False
    await engine._claim_and_run(retried["id"], retried["task_key"])
    async with app.state.sessionmaker() as db:
        row = await db.get(AgentChange, change_id)
        task = await db.get(Task, retried["id"])
        assert row.turn_task_id == original["id"] and row.turn_task_key == original["task_key"]
        assert row.result["_external_outcome_unknown"] is True and task.status == "failed"
    assert connector.calls == []


async def test_queued_retry_cannot_rebind_source_with_changed_model_instance(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑"})])
    original = await send_task(client, url)
    assert (await wait_task(client, original, statuses=("paused", "failed")))["status"] == "paused"
    cancelled = await client.post(f"/api/v1/tasks/{original['id']}/cancel", params=task_params(original))
    assert cancelled.status_code == 200
    engine = app.state.task_engine
    await engine.stop()
    response = await client.post(f"/api/v1/tasks/{original['id']}/retry", params=task_params(original))
    assert response.status_code == 202, response.text
    retried = response.json()
    async with app.state.sessionmaker() as db:
        source = await db.get(Task, original["id"])
        original_input = dict(source.input)
        source.input = {**original_input, "_project_model": {
            **original_input["_project_model"], "config_instance_key": str(uuid4()),
        }}
        await db.commit()
        target = await db.get(Task, retried["id"])
        engine._running_rounds[retried["id"]], engine._stopping = target.queued_at, False
    await engine._claim_and_run(retried["id"], retried["task_key"])
    async with app.state.sessionmaker() as db:
        message = await db.scalar(select(AgentMessage).where(
            AgentMessage.conversation_id == int(url.rsplit("/", 1)[1]), AgentMessage.role == "user",
        ))
        change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == original["id"]))
        assert message.turn_task_id == original["id"] and message.turn_task_key == original["task_key"]
        assert change is not None and change.turn_task_key == original["task_key"]
        assert (await db.get(Task, retried["id"])).status in ("failed", "running")
    assert connector.calls == []


@pytest.mark.parametrize("proof", [None, True, -1, 999999])
async def test_queued_agent_rejects_missing_or_replaced_original_actor(app, client, fake_llm, proof):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}})
    engine = app.state.task_engine
    await engine.stop()
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    task = await send_task(client, url)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        data = dict(row.input)
        if proof is None:
            data.pop("_agent_actor_token_version")
        else:
            data["_agent_actor_token_version"] = proof
        row.input = data
        await db.commit()
        engine._running_rounds[task["id"]], engine._stopping = row.queued_at, False
    await engine._claim_and_run(task["id"], task["task_key"])
    async with app.state.sessionmaker() as db:
        saved = await db.get(Task, task["id"])
        assert saved.status in ("queued", "failed") and saved.execution_key is None
    assert connector.calls == [] and fake_llm.tool_calls == []


async def test_queued_agent_does_not_adopt_a_newer_token(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector, prefs={"lookup": {"auto_confirm": True}})
    engine = app.state.task_engine
    await engine.stop()
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "请求"})])
    task = await send_task(client, url)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        actor = await db.get(User, row.user_id)
        assert row.input["_agent_actor_token_version"] == actor.token_version
        actor.token_version += 1
        await db.commit()
        engine._running_rounds[task["id"]], engine._stopping = row.queued_at, False
    await engine._claim_and_run(task["id"], task["task_key"])
    async with app.state.sessionmaker() as db:
        saved = await db.get(Task, task["id"])
        assert saved.status in ("queued", "failed") and saved.execution_key is None
    assert connector.calls == [] and fake_llm.tool_calls == []


async def test_queued_agent_rejects_model_id_reuse_even_with_same_fields(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector)
    engine = app.state.task_engine
    await engine.stop()
    fake_llm.push_tools([("text", {"text": "不能使用新实例"})])
    task = await send_task(client, url)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        binding = row.input["_project_model"]
        assert binding["version"] == 2 and binding["config_instance_key"]
        config = await db.get(AIConfig, row.input["ai_config_id"])
        assert binding["config_instance_key"] == config.instance_key
        fields = {column.key: getattr(config, column.key) for column in AIConfig.__table__.columns}
        fields["instance_key"] = str(uuid4())
        await db.delete(config)
        await db.flush()
        db.add(AIConfig(**fields))
        await db.commit()
        engine._running_rounds[task["id"]], engine._stopping = row.queued_at, False
    await engine._claim_and_run(task["id"], task["task_key"])
    async with app.state.sessionmaker() as db:
        saved = await db.get(Task, task["id"])
        assert saved.status in ("queued", "failed")
    assert connector.calls == [] and fake_llm.tool_calls == []


async def test_agent_provider_construction_does_not_hold_task_write_transaction(app, client, fake_llm, monkeypatch):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    project, _, url, _ = await setup(client, connector)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ai_config_service.build_provider

    async def delayed_build(config, *args, **kwargs):
        if isinstance(config, AIConfig) and inspect(config).transient:
            entered.set()
            await release.wait()
        return await original(config, *args, **kwargs)

    monkeypatch.setattr(ai_config_service, "build_provider", delayed_build)
    fake_llm.push_tools([("text", {"text": "完成"})])
    task = await send_task(client, url)
    try:
        await asyncio.wait_for(entered.wait(), 10)

        async def competing_writer():
            async with app.state.sessionmaker() as db:
                await db.execute(update(Project).where(Project.id == project["id"]).values(
                    updated_at=Project.updated_at,
                ))
                await db.commit()

        await asyncio.wait_for(competing_writer(), 2)
        release.set()
        assert (await wait_task(client, task, within=15, statuses=("succeeded", "failed")))["status"] == "succeeded"
    finally:
        release.set()


@pytest.mark.parametrize("changed", ["model", "model_recreate", "actor", "message"])
async def test_agent_event_does_not_publish_after_resource_change(app, client, fake_llm, changed):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, url, _ = await setup(client, connector)
    gate = Gate()
    fake_llm.push_tools([("text", {"text": "已授权"}), gate, ("text", {"text": "不得发布"})])
    task = await send_task(client, url)
    try:
        await gate.wait_reached(within=10)
        hub = app.state.task_engine.hubs[task["id"]]
        running = app.state.task_engine._running[task["id"]]
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task["id"])
            if changed == "model":
                config = await db.get(AIConfig, row.input["ai_config_id"])
                config.model = "替换后的模型"
            elif changed == "model_recreate":
                config = await db.get(AIConfig, row.input["ai_config_id"])
                fields = {column.key: getattr(config, column.key) for column in AIConfig.__table__.columns}
                fields["instance_key"] = str(uuid4())
                await db.delete(config)
                await db.flush()
                db.add(AIConfig(**fields))
            elif changed == "actor":
                actor = await db.get(User, row.user_id)
                actor.token_version += 1
            else:
                original = await db.get(AgentMessage, row.input["message_id"])
                original.content = "替换后的用户消息"
            await db.commit()
        gate.release()
        await asyncio.wait({running}, timeout=15)
        assert running.done()
        async with app.state.sessionmaker() as db:
            saved = await db.get(Task, task["id"])
            assert "不得发布" not in (saved.output_text or "")
        assert "不得发布" not in hub.text
    finally:
        gate.release()


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_checkpoint_identity_advances_only_after_proven_commit(app, client, lost_ack):
    owner = await register(client)
    project = await create_project(client)
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="queued",
                    queued_at=now, input={"_agent_actor_token_version": 0, "_task_actor_token_version": 0})
        db.add_all([conversation, task])
        await db.flush()
        execution_key = str(uuid4())
        execution = from_queued_task(task, execution_key)
        task.status, task.execution_key = "running", execution_key
        await db.commit()
        job = runner.AgentJob(task.id, owner["id"], project["id"], conversation.id, 1, None, None, None,
                              task_key=task.statistics_key, queued_at=now, execution_key=execution_key,
                              actor_token_version=0, execution=execution,
                              execution_state=runner._AgentExecutionState(execution))

    async with app.state.sessionmaker() as db:
        async def commit_with_lost_ack():
            await db.commit()
            raise RuntimeError("模拟断点提交确认丢失")

        if lost_ack:
            with pytest.raises(RuntimeError):
                await runner._checkpoint(db, job, {"step": "committed"}, commit=commit_with_lost_ack)
            assert job.task_execution_identity == execution
            with pytest.raises(ConflictError):
                await runner._lock_execution(db, job)
        else:
            await runner._checkpoint(db, job, {"step": "committed"})
            assert same_execution(execution, job.task_execution_identity)
            assert job.task_execution_identity.checkpoint == {"step": "committed"}
    async with app.state.sessionmaker() as db:
        stored = await db.get(Task, job.task_id)
        assert stored.checkpoint == {"step": "committed"}
        assert matches_running(job.task_execution_identity, stored) is (not lost_ack)


@pytest.mark.parametrize("result", [
    {"error": "未知", "_external_outcome_unknown": False},
    {"error": "未知", "_external_outcome_unknown": None},
    {"error": "未知", "_external_execution": None},
    {"error": "未知", "_external_snapshot_mac": None},
    {"error": "未知", "_external_claim_id": "坏凭据"},
    None,
])
async def test_damaged_or_stale_external_evidence_blocks_new_turn(app, client, fake_llm, result):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    project, _, url, _ = await setup(client, connector)
    conversation_id = int(url.rsplit("/", 1)[1])
    async with app.state.sessionmaker() as db:
        db.add(AgentChange(conversation_id=conversation_id, project_id=project["id"],
                           tool_call_id="old", tool_name="external", kind="external", status="failed", result=result,
                           error=service.EXTERNAL_INTERRUPTED))
        await db.commit()
    response = await client.post(url + "/messages", json={"content": "尝试新 call id"})
    assert response.status_code == 409 and response.json()["code"] == "external_outcome_unknown"
    assert connector.calls == [] and fake_llm.tool_calls == []


@pytest.mark.parametrize("replacement", ["task_key", "execution", "queued", "change", "claim", "actor", "proof",
                                          "proof_bool", "input", "checkpoint", "payload", "mac", "key_rotation"])
async def test_old_cleanup_never_modifies_replaced_identity(app, client, replacement):
    owner = await register(client)
    project = await create_project(client)
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        conv = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="queued",
                    queued_at=now,
                    input={"_agent_actor_token_version": 0, "_task_actor_token_version": 0})
        db.add_all([conv, task])
        await db.flush()
        execution_key = str(uuid4())
        execution = from_queued_task(task, execution_key)
        task.status, task.execution_key = "running", execution_key
        job = runner.AgentJob(task.id, owner["id"], project["id"], conv.id, 1, None, None, None,
                              task_key=task.statistics_key, queued_at=now, execution_key=task.execution_key,
                              actor_token_version=0, execution=execution,
                              execution_state=runner._AgentExecutionState(execution))
        queued = now.isoformat()
        claim_id, change_key = str(uuid4()), str(uuid4())
        result = {"_external_claim_id": claim_id, "_external_task_key": job.task_key,
                  "_external_execution": {"queued_at": queued, "execution_key": job.execution_key,
                                          "actor_token_version": 0}}
        row = AgentChange(change_key=change_key, conversation_id=conv.id, project_id=project["id"],
                          turn_task_id=task.id, turn_task_key=job.task_key, tool_call_id="call", tool_name="external",
                          kind="external", status="executing", executed_at=now, result=result)
        db.add(row)
        await db.flush()
        task_snapshot = runner._execution_task_snapshot(execution)
        change_snapshot = mcp_tools.change_snapshot(row)
        mac = mcp_tools.external_snapshot_mac(
            db.info.get("agent_view_hmac_key"), actor_id=job.user_id, token_version=0, project_id=job.project_id,
            conversation_id=job.conversation_id, task_id=job.task_id, task_key=job.task_key, queued_at=now,
            execution_key=execution_key, change_id=row.id, change_key=change_key, claim_id=claim_id,
            tool_call_id="call", started_at=now, task_evidence=task_snapshot, change_evidence=change_snapshot,
        )
        result = {**result, "_external_snapshot_mac": mac}
        row.result = result
        claim = runner._AutoExternalClaim(job, row.id, change_key, claim_id, "call", now, queued,
                                          execution, change_snapshot, task_snapshot, mac)
        change_id = row.id
        if replacement == "task_key":
            task.statistics_key = str(uuid4())
        elif replacement == "execution":
            task.execution_key = str(uuid4())
        elif replacement == "queued":
            task.queued_at = now + timedelta(seconds=1)
        elif replacement == "change":
            row.change_key = str(uuid4())
        elif replacement == "claim":
            result = {**result, "_external_claim_id": str(uuid4())}
            row.result = result
        elif replacement == "actor":
            task.input = {"_agent_actor_token_version": 1, "_task_actor_token_version": 1}
        elif replacement == "proof":
            result = {**result, "_external_execution": {**result["_external_execution"], "actor_token_version": 1}}
            row.result = result
        elif replacement == "proof_bool":
            result = {**result, "_external_execution": {**result["_external_execution"], "actor_token_version": False}}
            row.result = result
        elif replacement == "input":
            task.input = {**task.input, "replaced": True}
        elif replacement == "checkpoint":
            task.checkpoint = {"replaced": True}
        elif replacement == "payload":
            row.arguments = {"replaced": True}
        elif replacement == "mac":
            result = {**result, "_external_snapshot_mac": "a" * 64}
            row.result = result
        await db.commit()
    info = app.state.sessionmaker.kw["info"]
    old_key = info["agent_view_hmac_key"]
    if replacement == "key_rotation":
        info["agent_view_hmac_key"] = b"k" * 32
    try:
        await runner._settle_external_claim(app.state.task_engine.ctx, claim, unknown=True)
    finally:
        info["agent_view_hmac_key"] = old_key
    async with app.state.sessionmaker() as db:
        saved = await db.get(AgentChange, change_id)
        assert saved.status == "executing" and saved.result == result
        if replacement == "payload":
            assert saved.arguments == {"replaced": True}
        if replacement == "proof_bool":
            assert saved.result["_external_execution"]["actor_token_version"] is False
