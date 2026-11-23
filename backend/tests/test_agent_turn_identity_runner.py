"""后台执行体携带不可复用的任务身份，整数 ID 重用不能接管旧轮次。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.models import AgentChange, AgentMessage, GenerationLog, Task
from app.services.agent import runner
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.handlers import agent as agent_handler
from tests.helpers import create_ai_config, create_project, register, wait_task
from tests.mcp_fakes import FakeMcpConnector
from tests.test_agent_mcp_skills import call, setup


async def _old_job(app, client):
    owner = await register(client, "turn-identity-owner")
    project = await create_project(client)
    await create_ai_config(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    created = await client.post(f"{base}/conversations", json={})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    await app.state.task_engine.stop()
    accepted = await client.post(f"{base}/conversations/{conversation_id}/messages", json={"content": "原轮次"})
    assert accepted.status_code == 202, accepted.text
    task_ref = accepted.json()["task"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_ref["id"])
        assert task.statistics_key == task_ref["task_key"] and task.user_id == owner["id"]
        actor_version = task.input["_agent_actor_token_version"]
        assert type(actor_version) is int and actor_version >= 0
        # 测试断言从空消息表开始；身份仍取真实认证请求受理时的 Task 快照。
        message = await db.get(AgentMessage, task.input["message_id"])
        assert message.turn_task_id == task.id and message.turn_task_key == task.statistics_key
        await db.delete(message)
        # 与真实领取一致：在 queued 快照上生成不可变执行身份，再以同一 execution_key 进入 running。
        task.checkpoint = {"original": True}
        execution = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", execution.execution_key
        job = SimpleNamespace(
            task_id=task.id, task_key=task.statistics_key, queued_at=task.queued_at,
            execution_key=task.execution_key, user_id=owner["id"], project_id=project["id"],
            conversation_id=conversation_id, actor_token_version=actor_version,
            execution=execution, execution_state=runner._AgentExecutionState(execution),
        )
        await db.commit()
    return job


@pytest.mark.parametrize("write", ["tool_result", "proposal"])
async def test_old_execution_cannot_attach_to_reused_task_id(app, client, monkeypatch, write):
    job = await _old_job(app, client)
    waiting, continue_old_job = asyncio.Event(), asyncio.Event()
    call = {"id": "same-call", "name": "external", "arguments": "{}"}
    monkeypatch.setattr(runner.mcp_tools, "validate", lambda *_: None)
    monkeypatch.setattr(runner.mcp_tools, "preview", lambda *_: {"summary": "待确认"})

    async def old_execution():
        waiting.set()
        await continue_old_job.wait()
        async with app.state.sessionmaker() as db:
            if write == "tool_result":
                await runner._result(db, job, call, {"ok": True})
            else:
                external = SimpleNamespace(label="插件", auto_confirm=False, entry={})
                async for _ in runner._external_call(job, db, call, {}, external, None, []):
                    pass

    old = asyncio.create_task(old_execution())
    await waiting.wait()
    async with app.state.sessionmaker() as db:
        previous = await db.get(Task, job.task_id)
        await db.delete(previous)
        await db.flush()
        replacement = Task(
            id=job.task_id, user_id=job.user_id, project_id=job.project_id, kind="agent.turn", status="running",
            execution_key=str(uuid4()), checkpoint={"replacement": True},
        )
        db.add(replacement)
        await db.commit()
        new_key = replacement.statistics_key
    assert new_key != job.task_key
    continue_old_job.set()
    with pytest.raises(ConflictError):
        await old

    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, job.task_id)).checkpoint == {"replacement": True}
        assert list(await db.scalars(select(AgentMessage))) == []
        assert list(await db.scalars(select(AgentChange))) == []


@pytest.mark.parametrize("change", ["execution_key", "queued_at", "status", "admin_cancel"])
async def test_old_execution_cannot_write_after_same_task_is_requeued(app, client, change):
    job = await _old_job(app, client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, job.task_id)
        if change == "execution_key":
            task.execution_key = str(uuid4())
        elif change == "queued_at":
            task.queued_at += timedelta(seconds=1)
        elif change == "status":
            task.status = "paused"
        else:
            task.admin_cancel_operation_id = str(uuid4())
        task.checkpoint = {"new_execution": True}
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await runner._checkpoint(db, job, {"stale": True})
        await db.rollback()
        with pytest.raises(ConflictError):
            await runner._result(db, job, {"id": "call", "name": "read"}, {"ok": True})
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, job.task_id)
        assert task.statistics_key == job.task_key and task.checkpoint == {"new_execution": True}
        assert list(await db.scalars(select(AgentMessage))) == []


async def test_current_execution_persists_original_key_and_deduplicates(app, client, monkeypatch):
    job = await _old_job(app, client)
    call = {"id": "read-call", "name": "read"}
    monkeypatch.setattr(runner.mcp_tools, "validate", lambda *_: None)
    monkeypatch.setattr(runner.mcp_tools, "preview", lambda *_: {"summary": "待确认"})
    async with app.state.sessionmaker() as db:
        await runner._result(db, job, call, {"ok": True})
        await runner._result(db, job, call, {"ok": False})
        await runner._checkpoint(db, job, {"current": True})
        external = SimpleNamespace(label="插件", auto_confirm=False, entry={})
        proposal_call = {"id": "external-call", "name": "external"}
        async for _ in runner._external_call(job, db, proposal_call, {}, external, None, []):
            pass
    async with app.state.sessionmaker() as db:
        messages = list(await db.scalars(select(AgentMessage)))
        changes = list(await db.scalars(select(AgentChange)))
        assert len(messages) == len(changes) == 1
        assert (messages[0].turn_task_id, messages[0].turn_task_key) == (job.task_id, job.task_key)
        assert (changes[0].turn_task_id, changes[0].turn_task_key) == (job.task_id, job.task_key)
        assert (await db.get(Task, job.task_id)).checkpoint == {"current": True}


async def test_task_replacement_waits_for_locked_result_commit(app, client, monkeypatch):
    job = await _old_job(app, client)
    locked, release = asyncio.Event(), asyncio.Event()
    real_lock = runner._lock_execution

    async def pause_after_lock(db, identity):
        await real_lock(db, identity)
        locked.set()
        await release.wait()

    monkeypatch.setattr(runner, "_lock_execution", pause_after_lock)

    async def old_execution():
        async with app.state.sessionmaker() as db:
            await runner._result(db, job, {"id": "call", "name": "read"}, {"ok": True})

    async def replace_task():
        async with app.state.sessionmaker() as db:
            await db.delete(await db.get(Task, job.task_id))
            await db.flush()
            replacement = Task(
                id=job.task_id, user_id=job.user_id, project_id=job.project_id, kind="agent.turn",
                status="running", execution_key=str(uuid4()),
            )
            db.add(replacement)
            await db.commit()
            return replacement.statistics_key

    old = asyncio.create_task(old_execution())
    await asyncio.wait_for(locked.wait(), timeout=5)
    replacement = asyncio.create_task(replace_task())
    try:
        await asyncio.sleep(0.1)
        assert not replacement.done()
    finally:
        release.set()
    await asyncio.wait_for(old, timeout=5)
    new_key = await asyncio.wait_for(replacement, timeout=5)
    assert new_key != job.task_key
    async with app.state.sessionmaker() as db:
        message = await db.scalar(select(AgentMessage))
        # 删除旧任务后 FK 清空；旧消息仍保留创建时的 UUID，无法归属给复用 ID 的新任务。
        assert message.turn_task_id is None and message.turn_task_key == job.task_key


async def test_new_queued_turn_binds_user_message_in_creation_transaction(app, client):
    job = await _old_job(app, client)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(job.user_id, job.actor_token_version)
        message = AgentMessage(conversation_id=job.conversation_id, role="user", content="新的轮次")
        db.add(message)
        await db.flush()
        task = Task(
            user_id=job.user_id, project_id=job.project_id, kind="agent.turn", status="queued",
            input={"request": {"conversation_id": job.conversation_id}, "message_id": message.id,
                   "_agent_actor_token_version": job.actor_token_version,
                   "_task_actor_token_version": job.actor_token_version},
        )
        db.add(task)
        await db.flush()
        await agent_handler.bind_queued_turn(db, task)
        await db.commit()
        task_id, task_key, message_id = task.id, task.statistics_key, message.id
    async with app.state.sessionmaker() as db:
        message = await db.get(AgentMessage, message_id)
        assert (message.turn_task_id, message.turn_task_key) == (task_id, task_key)


async def test_queued_turn_never_rebinds_already_owned_message(app, client):
    job = await _old_job(app, client)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(job.user_id, job.actor_token_version)
        message = AgentMessage(conversation_id=job.conversation_id, role="user", content="已有轮次",
                               turn_task_id=job.task_id, turn_task_key=job.task_key)
        db.add(message)
        await db.flush()
        task = Task(
            user_id=job.user_id, project_id=job.project_id, kind="agent.turn", status="queued",
            input={"request": {"conversation_id": job.conversation_id}, "message_id": message.id,
                   "_agent_actor_token_version": job.actor_token_version,
                   "_task_actor_token_version": job.actor_token_version},
        )
        db.add(task)
        await db.flush()
        with pytest.raises(ConflictError):
            await agent_handler.bind_queued_turn(db, task)
        await db.rollback()


async def test_new_turn_binds_user_and_assistant_from_captured_task(app, client, fake_llm):
    await register(client, "turn-run-owner")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    created = await client.post(f"{base}/conversations", json={})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    fake_llm.push_tools([("text", {"text": "完成"})])
    response = await client.post(f"{base}/conversations/{conversation_id}/messages", json={"content": "继续"})
    assert response.status_code == 202, response.text
    captured = response.json()["task"]
    assert captured["task_key"]
    final = await wait_task(client, captured, within=10)
    assert final["status"] == "succeeded", final
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AgentMessage).order_by(AgentMessage.id)))
        assert [row.role for row in rows] == ["user", "assistant"]
        assert {(row.turn_task_id, row.turn_task_key) for row in rows} == {(captured["id"], captured["task_key"])}


async def _external_turn(app, client, fake_llm):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    _, base, url, _ = await setup(client, connector)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    response = await client.post(f"{url}/messages", json={"content": "请发送"})
    assert response.status_code == 202, response.text
    task = response.json()["task"]
    assert (await wait_task(client, task, within=10, statuses=("paused", "failed")))["status"] == "paused"
    detail = await client.get(url)
    assert detail.status_code == 200, detail.text
    change = detail.json()["changes"][0]
    return connector, base, url, task, change


async def test_proposal_confirm_resume_keeps_task_identity(app, client, fake_llm):
    connector, base, _, task, change = await _external_turn(app, client, fake_llm)
    assert (change["turn_task_id"], change["turn_task_key"]) == (task["id"], task["task_key"])
    fake_llm.push_tools([("text", {"text": "已发送"})])
    response = await client.post(
        f"{base}/changes/{change['id']}/confirm",
        json={"change_key": change["change_key"], "view_version": change["view_version"]},
    )
    assert response.status_code == 200, response.text
    assert (await wait_task(client, task, within=10))["status"] == "succeeded"
    assert len(connector.calls) == 1
    async with app.state.sessionmaker() as db:
        messages = list(await db.scalars(select(AgentMessage)))
        proposal = await db.get(AgentChange, change["id"])
        assert {(row.turn_task_id, row.turn_task_key) for row in messages} == {(task["id"], task["task_key"])}
        assert (proposal.turn_task_id, proposal.turn_task_key) == (task["id"], task["task_key"])


async def test_retry_rebinds_proven_message_and_proposal_to_new_task(app, client, fake_llm):
    connector, _, _, old, change = await _external_turn(app, client, fake_llm)
    params = {"task_key": old["task_key"]}
    cancelled = await client.post(f"/api/v1/tasks/{old['id']}/cancel", params=params)
    assert cancelled.status_code == 200, cancelled.text
    response = await client.post(f"/api/v1/tasks/{old['id']}/retry", params=params)
    assert response.status_code == 202, response.text
    new = response.json()
    assert new["id"] != old["id"] and new["task_key"] != old["task_key"]
    final = await wait_task(client, new, within=10, statuses=("paused", "failed"))
    assert final["status"] == "paused", final
    async with app.state.sessionmaker() as db:
        messages = list(await db.scalars(select(AgentMessage)))
        proposal = await db.get(AgentChange, change["id"])
        assert {(row.turn_task_id, row.turn_task_key) for row in messages} == {(new["id"], new["task_key"])}
        assert (proposal.turn_task_id, proposal.turn_task_key) == (new["id"], new["task_key"])
    assert connector.calls == []


@pytest.mark.parametrize("legacy", ["message", "proposal"])
async def test_retry_rejects_unproven_source_without_backfilling(app, client, fake_llm, legacy):
    connector, _, _, old, change = await _external_turn(app, client, fake_llm)
    params = {"task_key": old["task_key"]}
    assert (await client.post(f"/api/v1/tasks/{old['id']}/cancel", params=params)).status_code == 200
    async with app.state.sessionmaker() as db:
        if legacy == "message":
            await db.execute(update(AgentMessage).where(
                AgentMessage.turn_task_id == old["id"], AgentMessage.role == "user",
            ).values(turn_task_key=None))
        else:
            await db.execute(update(AgentChange).where(AgentChange.id == change["id"]).values(turn_task_key=None))
        await db.commit()
    response = await client.post(f"/api/v1/tasks/{old['id']}/retry", params=params)
    assert response.status_code == 409, response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Task.id).where(Task.retry_of_id == old["id"])) is None
        proposal = await db.get(AgentChange, change["id"])
        assert proposal.turn_task_id == old["id"]
        assert proposal.turn_task_key == (None if legacy == "proposal" else old["task_key"])
        message = await db.scalar(select(AgentMessage).where(AgentMessage.role == "user"))
        assert message.turn_task_key == (None if legacy == "message" else old["task_key"])
    assert connector.calls == []


async def test_queued_retry_rejects_deleted_and_reused_source_id(app, client, fake_llm):
    connector, _, _, old, change = await _external_turn(app, client, fake_llm)
    params = {"task_key": old["task_key"]}
    assert (await client.post(f"/api/v1/tasks/{old['id']}/cancel", params=params)).status_code == 200
    engine = app.state.task_engine
    await engine.stop()
    response = await client.post(f"/api/v1/tasks/{old['id']}/retry", params=params)
    assert response.status_code == 202, response.text
    new = response.json()
    async with app.state.sessionmaker() as db:
        source = await db.get(Task, old["id"])
        owner_id, project_id = source.user_id, source.project_id
        await db.delete(source)
        await db.flush()
        replacement = Task(id=old["id"], user_id=owner_id, project_id=project_id,
                           kind="agent.turn", status="cancelled", checkpoint={"replacement": True})
        db.add(replacement)
        await db.commit()
        assert replacement.statistics_key != old["task_key"]
        queued = await db.get(Task, new["id"])
        engine._running_rounds[new["id"]], engine._stopping = queued.queued_at, False
    await engine.lease.register()
    await engine._claim_and_run(new["id"], new["task_key"])
    async with app.state.sessionmaker() as db:
        proposal = await db.get(AgentChange, change["id"])
        assert proposal.turn_task_id is None and proposal.turn_task_key == old["task_key"]
        assert (await db.get(Task, new["id"])).status == "failed"
        assert (await db.get(Task, old["id"])).checkpoint == {"replacement": True}
    assert connector.calls == []


async def test_reused_task_id_stops_model_send_before_fake_provider(app, client, fake_llm, monkeypatch):
    await register(client, "turn-send-owner")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    created = await client.post(f"{base}/conversations", json={})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    ready, resume = asyncio.Event(), asyncio.Event()
    real_guard = runner.context.guard_agent_provider

    async def pause_before_first_send(*args, **kwargs):
        guarded = await real_guard(*args, **kwargs)

        class PausedProvider:
            async def chat_tools_stream(self, *args, **kwargs):
                ready.set()
                await resume.wait()
                async for event in guarded.chat_tools_stream(*args, **kwargs):
                    yield event

        return PausedProvider()

    monkeypatch.setattr(runner.context, "guard_agent_provider", pause_before_first_send)
    fake_llm.push_tools([("text", {"text": "不能发出"})])
    response = await client.post(f"{base}/conversations/{conversation_id}/messages", json={"content": "原任务"})
    assert response.status_code == 202, response.text
    task = response.json()["task"]
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        old_execution = app.state.task_engine._running[task["id"]]
        async with app.state.sessionmaker() as db:
            original = await db.get(Task, task["id"])
            user_id, project_id = original.user_id, original.project_id
            await db.delete(original)
            await db.flush()
            replacement = Task(id=task["id"], user_id=user_id, project_id=project_id,
                               kind="agent.turn", status="running", execution_key=str(uuid4()),
                               checkpoint={"replacement": True})
            db.add(replacement)
            await db.commit()
            replacement_key = replacement.statistics_key
        assert replacement_key != task["task_key"]
    finally:
        resume.set()
    await asyncio.wait_for(old_execution, timeout=10)
    assert fake_llm.tool_calls == []
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task["id"])).checkpoint == {"replacement": True}
        message = await db.scalar(select(AgentMessage))
        assert message.turn_task_id is None and message.turn_task_key == task["task_key"]
        assert list(await db.scalars(select(GenerationLog))) == []
