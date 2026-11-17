"""独立 Agent 会话必须继承 HTTP 认证时的令牌版本。"""

import asyncio
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models import AgentChange, AgentConversation, Task, User
from app.services import authorization_service
from app.services.agent import access as agent_access
from app.services.agent import transactions
from tests.helpers import create_ai_config, create_project, register, task_params, wait_task
from tests.mcp_fakes import FakeMcpConnector
from tests.test_agent_mcp_skills import AUTO_MCP_REASON, URL, call, change_action, send_task, setup


async def _reject_after_token_revoked(app, client, monkeypatch):
    user = await register(client, "agent-token-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    response = await client.post(f"{base}/conversations", json={})
    assert response.status_code == 201, response.text
    conversation_id = response.json()["id"]
    async with app.state.sessionmaker() as db:
        task = Task(
            user_id=user["id"], project_id=project["id"], kind="agent.turn", status="paused",
            input={"request": {"conversation_id": conversation_id}},
        )
        db.add(task)
        await db.flush()
        changes = [AgentChange(
            conversation_id=conversation_id, project_id=project["id"], turn_task_id=task.id,
            turn_task_key=task.statistics_key,
            tool_call_id=f"change-{index}", tool_name="create_character", kind="small", arguments={}, preview={},
        ) for index in range(2)]
        db.add_all(changes)
        await db.commit()
        change_id = changes[0].id
    visible = (await client.get(f"{base}/conversations/{conversation_id}")).json()["changes"]
    change = next(item for item in visible if item["id"] == change_id)

    entered, release = asyncio.Event(), asyncio.Event()
    # E4i-d 起 Agent HTTP 写入口统一经 lock_agent_entry(lock=True)（认证/User 屏障之前）进入作品锁；
    # 锁外初检（lock=False）不取写屏障，不在此拦截。
    original = agent_access.lock_agent_entry

    async def gated(db, actor_id, project_id, **kwargs):
        if actor_id == user["id"] and project_id == project["id"] and kwargs.get("lock", True):
            entered.set()
            await release.wait()
        return await original(db, actor_id, project_id, **kwargs)

    monkeypatch.setattr(agent_access, "lock_agent_entry", gated)
    request = asyncio.create_task(client.post(f"{base}/changes/{change_id}/reject", json=change_action(change)))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        # 与请求认证、Agent 会话均分离；此提交完成后业务才能取得其 User 屏障。
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
            await db.commit()
        release.set()
        result = await asyncio.wait_for(request, 10)
        assert result.status_code == 401, result.text
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(AgentChange.status).where(AgentChange.id == change_id)) == "proposed"
            assert await db.scalar(select(AgentConversation.id).where(AgentConversation.id == conversation_id))
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request


async def test_agent_reject_rechecks_original_http_token_after_revocation(app, client, monkeypatch):
    await _reject_after_token_revoked(app, client, monkeypatch)




@pytest.mark.parametrize("action", ["create", "update", "delete", "send"])
async def test_agent_conversation_writes_recheck_original_token(app, client, monkeypatch, action):
    user = await register(client, "agent-token-write-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    created = await client.post(f"{base}/conversations", json={"title": "原会话"})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    entered, release = asyncio.Event(), asyncio.Event()
    original_session = transactions.session

    @asynccontextmanager
    async def gated_session(factory):
        # 已经过 HTTP 认证，但尚未在独立 Agent 会话内取得任何写锁。
        entered.set()
        await release.wait()
        async with original_session(factory) as session:
            yield session

    monkeypatch.setattr(transactions, "session", gated_session)
    conversation_url = f"{base}/conversations/{conversation_id}"
    method, path, body = {
        "create": ("POST", f"{base}/conversations", {"title": "不可创建"}),
        "update": ("PATCH", conversation_url, {"title": "不可修改"}),
        "delete": ("DELETE", conversation_url, None),
        "send": ("POST", f"{conversation_url}/messages", {"content": "不可发送"}),
    }[action]
    request = asyncio.create_task(client.request(method, path, json=body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
            await db.commit()
        release.set()
        result = await asyncio.wait_for(request, 10)
        assert result.status_code == 401, result.text
        async with app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(AgentConversation).where(
                AgentConversation.project_id == project["id"], AgentConversation.user_id == user["id"],
            )))
            assert len(rows) == 1 and rows[0].id == conversation_id and rows[0].title == "原会话"
            assert await db.scalar(select(Task.id).where(Task.user_id == user["id"])) is None
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request


async def _conversation_write_after_revocation(app, client, monkeypatch, action):
    user = await register(client, "agent-token-gate-owner")
    # 发送消息先做模型预检，须有可用配置才能真正抵达写门禁。
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    created = await client.post(f"{base}/conversations", json={"title": "原会话"})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original_require = authorization_service.require_authenticated

    async def gated_require(db, actor, *, write=False, targets=()):
        if write and actor == user["id"]:
            entered.set()
            await release.wait()
        return await original_require(db, actor, write=write, targets=targets)

    monkeypatch.setattr(authorization_service, "require_authenticated", gated_require)
    conversation_url = f"{base}/conversations/{conversation_id}"
    method, path, body = {
        "create": ("POST", f"{base}/conversations", {"title": "不可创建"}),
        "update": ("PATCH", conversation_url, {"title": "不可修改"}),
        "delete": ("DELETE", conversation_url, None),
        "send": ("POST", f"{conversation_url}/messages", {"content": "不可发送"}),
    }[action]
    request = asyncio.create_task(client.request(method, path, json=body))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
            await db.commit()
        release.set()
        result = await asyncio.wait_for(request, 10)
        assert result.status_code == 401, result.text
        async with app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(AgentConversation).where(
                AgentConversation.project_id == project["id"], AgentConversation.user_id == user["id"],
            )))
            assert len(rows) == 1 and rows[0].id == conversation_id and rows[0].title == "原会话"
            assert await db.scalar(select(Task.id).where(Task.user_id == user["id"])) is None
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request


@pytest.mark.parametrize("action", ["create", "update", "delete", "send"])
async def test_agent_conversation_writes_revalidate_at_write_gate(app, client, monkeypatch, action):
    await _conversation_write_after_revocation(app, client, monkeypatch, action)




async def test_external_result_after_token_revocation_does_not_apply(app, client, fake_llm, monkeypatch):
    from app.services.agent import service

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    project, base, conversation_url, _ = await setup(client, connector)
    actor = (await client.get("/api/v1/auth/me")).json()
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_ref = await send_task(client, conversation_url)
    task_id = task_ref["id"]
    assert (await wait_task(client, task_ref, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def gated_call(name, arguments):
        calls.append((name, arguments))
        entered.set()
        await release.wait()
        return "PRIVATE-REMOTE-RESULT"

    connector.servers[URL].handler = gated_call
    request = asyncio.create_task(client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change)))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == actor["id"]).values(token_version=User.token_version + 1))
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 10)
        assert response.status_code == 401, response.text
        assert "PRIVATE-REMOTE-RESULT" not in response.text
        async with app.state.sessionmaker() as db:
            stored = await db.get(AgentChange, change["id"])
            assert stored.project_id == project["id"] and stored.status == "failed"
            assert stored.error == service.EXTERNAL_RETURNED_NOT_APPLIED
            assert stored.result == {"error": service.EXTERNAL_RETURNED_NOT_APPLIED}
            assert "PRIVATE-REMOTE-RESULT" not in str(stored.result)
            assert (await db.get(Task, task_id)).status == "paused"
        assert len(calls) == 1
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request


async def test_external_policy_failure_stops_turn_without_returning_result(app, client, fake_llm, monkeypatch):
    from app.services.agent import mcp_tools, service

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    project, base, conversation_url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_ref = await send_task(client, conversation_url)
    task_id = task_ref["id"]
    assert (await wait_task(client, task_ref, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    calls = 0

    async def fail_policy(*_args, **kwargs):
        nonlocal calls
        assert kwargs.get("preserve_outcome") is True
        assert isinstance(kwargs.get("send_state"), mcp_tools.McpSendState)
        calls += 1
        raise transactions.AgentAuthorizationChangedError()

    monkeypatch.setattr(mcp_tools, "call_data", fail_policy)
    response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert response.status_code == 409 and response.json()["code"] == "permission_denied", response.text
    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, change["id"])
        assert stored.project_id == project["id"] and stored.status == "failed"
        assert stored.error == service.EXTERNAL_NOT_SENT
        assert stored.result == {"error": service.EXTERNAL_NOT_SENT}
        assert (await db.get(Task, task_id)).status == "paused"
    assert calls == 1


@pytest.mark.parametrize("phase", ["connect", "call"])
async def test_external_worker_preserves_policy_failure(app, client, fake_llm, phase):
    from app.services.agent import service

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(
        client, connector, handler=lambda _name, _args: transactions.AgentAuthorizationChangedError(),
    )
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_ref = await send_task(client, conversation_url)
    task_id = task_ref["id"]
    assert (await wait_task(client, task_ref, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    if phase == "connect":
        connector.servers[URL].connect_error = transactions.AgentAuthorizationChangedError()
    response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == ("permission_denied" if phase == "connect" else "external_outcome_unknown")
    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, change["id"])
        assert stored.status == "failed"
        if phase == "connect":
            assert stored.error == service.EXTERNAL_NOT_SENT
            assert stored.result == {"error": service.EXTERNAL_NOT_SENT}
        else:
            assert stored.error == service.EXTERNAL_INTERRUPTED
            assert stored.result["_external_outcome_unknown"] is True
            assert stored.result["_external_task_key"] == task_ref["task_key"]
            assert stored.result[transactions.EXTERNAL_CLAIM_KEY]
        assert (await db.get(Task, task_id)).status == "paused"
    assert len(connector.calls) == (0 if phase == "connect" else 1)
    assert connector.open == 0


@pytest.mark.xfail(strict=True, reason=AUTO_MCP_REASON)
async def test_automatic_external_policy_failure_stops_background_turn(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, _, conversation_url, _ = await setup(
        client, connector, prefs={"lookup": {"auto_confirm": True}},
        handler=lambda _name, _args: transactions.AgentAuthorizationChangedError(),
    )
    fake_llm.push_tools([call("mcp__dict__lookup", {"word": "剑"})])
    fake_llm.push_tools([("text", {"text": "不应继续"})])
    task_ref = await send_task(client, conversation_url)
    result = await wait_task(client, task_ref, statuses=("succeeded", "failed"))
    assert result["status"] == "failed"
    assert len(connector.calls) == 1 and len(fake_llm.tool_calls) == 1
    assert connector.open == 0


@pytest.mark.parametrize("replacement", ["claim", "task"])
async def test_external_result_rejects_replaced_execution_claim(app, client, fake_llm, monkeypatch, replacement):
    from app.services.agent import context, runner

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    project, base, conversation_url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_ref = await send_task(client, conversation_url)
    task_id = task_ref["id"]
    assert (await wait_task(client, task_ref, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_call(_name, _arguments):
        entered.set()
        await release.wait()
        return "旧执行结果"

    connector.servers[URL].handler = gated_call
    request = asyncio.create_task(client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change)))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        view = await client.get(conversation_url)
        assert view.status_code == 200, view.text
        assert "_external_claim_id" not in view.text and view.json()["changes"][0]["result"] is None
        assert "_external_task_key" not in view.text
        async with app.state.sessionmaker() as db:
            executing = await db.get(AgentChange, change["id"])
            assert executing.result and "_external_claim_id" in executing.result
            original_claim = executing.result["_external_claim_id"]
            original_result = dict(executing.result)
            original_task_key = executing.result["_external_task_key"]
            assert original_task_key == (await db.get(Task, task_id)).statistics_key
            assert "_external_claim_id" not in str(context.change_result_for_model(executing))
            assert "_external_claim_id" not in str(runner._out(executing))
            assert "_external_task_key" not in str(context.change_result_for_model(executing))
            assert "_external_task_key" not in str(runner._out(executing))
        # 即使同一时钟值和任务仍在使用，也不能让旧执行覆盖后来一轮的 UUID claim。
        new_claim = str(uuid4())
        async with app.state.sessionmaker() as db:
            if replacement == "claim":
                await db.execute(update(AgentChange).where(AgentChange.id == change["id"]).values(
                    result={"_external_claim_id": new_claim, "_external_task_key": original_task_key},
                ))
            else:
                await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=str(uuid4())))
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 10)
        assert response.status_code == 409, response.text
        async with app.state.sessionmaker() as db:
            stored = await db.get(AgentChange, change["id"])
            assert stored.project_id == project["id"] and stored.status == "executing"
            # 被替换的 claim 原样保留；任务被替换时原 claim（含快照 MAC）也不能被旧执行体改写。
            assert stored.result == ({"_external_claim_id": new_claim, "_external_task_key": original_task_key}
                                     if replacement == "claim" else original_result)
            assert stored.result["_external_claim_id"] == (new_claim if replacement == "claim" else original_claim)
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request


async def test_task_deletion_waits_for_inflight_external_claim(app, client, fake_llm, monkeypatch):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_ref = await send_task(client, conversation_url)
    task_id = task_ref["id"]
    assert (await wait_task(client, task_ref, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_call(_name, _arguments):
        entered.set()
        await release.wait()
        return "远端已经处理"

    connector.servers[URL].handler = gated_call
    request = asyncio.create_task(client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change)))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))).status_code == 200
        response = await client.delete(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))
        assert response.status_code == 409, response.text
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await request


async def test_stale_recovery_does_not_interrupt_a_live_remote_call(app, client, fake_llm, monkeypatch):
    from datetime import timedelta

    from app.services.agent import service

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_ref = await send_task(client, conversation_url)
    task_id = task_ref["id"]

    async def wait_paused():
        while True:
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, task_id)
                if task.status in ("paused", "failed"):
                    assert task.status == "paused"
                    return task.statistics_key
            await asyncio.sleep(0.05)

    task_key = task_ref["task_key"]
    assert await asyncio.wait_for(wait_paused(), 15) == task_key
    change = (await client.get(conversation_url)).json()["changes"][0]
    assert change["turn_task_key"] == task_key
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated_call(_name, _arguments):
        entered.set()
        await release.wait()
        return "远端已完成"

    connector.servers[URL].handler = gated_call
    monkeypatch.setattr(service, "EXTERNAL_STALE_AFTER", timedelta(seconds=0))
    identity = {"change_key": change.get("change_key"), "view_version": change.get("view_version")}
    request = asyncio.create_task(client.post(f"{base}/changes/{change['id']}/confirm", json=identity))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with app.state.sessionmaker() as db:
            assert await service.recover_external(
                db, task_id=task_id, task_key=task_key, engine=app.state.task_engine,
            ) == 0
            await db.commit()
            row = await db.get(AgentChange, change["id"])
            assert row.status == "executing" and row.result["_external_claim_id"]
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await request


async def test_external_missing_task_uuid_fails_before_network(app, client, fake_llm, monkeypatch):
    from app.services.agent import mcp_tools

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, conversation_url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_ref = await send_task(client, conversation_url)
    task_id = task_ref["id"]
    assert (await wait_task(client, task_ref, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=None))
        await db.commit()

    async def must_not_send(*_args, **_kwargs):
        pytest.fail("缺失任务 UUID 不能发送外部请求")

    monkeypatch.setattr(mcp_tools, "call_data", must_not_send)
    response = await client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert response.status_code == 409, response.text
    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, change["id"])
        assert stored.status == "proposed"
