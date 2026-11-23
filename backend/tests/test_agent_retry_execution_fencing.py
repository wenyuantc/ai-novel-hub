"""Agent 重试不能改绑正在执行或无法证明身份的外部操作。"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AgentChange, Task
from app.services.agent import mcp_tools
from app.services.ai_dispatch import read_visibility_stamp
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.registry import get_handler
from tests.helpers import task_params, wait_task
from tests.mcp_fakes import FakeMcpConnector
from tests.test_agent_mcp_skills import call, change_action, send_task, setup


@pytest.fixture
def retry_mcp(app):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    return connector


async def _cancelled_external(app, client, fake_llm, retry_mcp, *, handler=None):
    project, base, url, _ = await setup(client, retry_mcp, handler=handler)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task = await send_task(client, url)
    assert (await wait_task(client, task, statuses=("paused",)))["status"] == "paused"
    detail = await client.get(url)
    assert detail.status_code == 200, detail.text
    change = detail.json()["changes"][0]
    assert change["change_key"] and change["view_version"] and change["turn_task_key"] == task["task_key"]
    return project, base, url, task, change


async def _mark_executing(app, client, task_ref, change_ref, *, minutes=3, evidence="valid"):
    actor = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
    assert actor is not None
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_ref["id"])
        change = await db.get(AgentChange, change_ref["id"])
        assert task.statistics_key == task_ref["task_key"] and task.user_id == actor[0]
        assert task.input["_agent_actor_token_version"] == task.input["_task_actor_token_version"] == actor[1]
        assert change.change_key == change_ref["change_key"] and change.turn_task_key == task_ref["task_key"]
        started = datetime.now(UTC) - timedelta(minutes=minutes)
        claim_id = str(uuid4())
        payload = {"_external_claim_id": claim_id, "_external_task_key": task_ref["task_key"]}
        if evidence == "missing":
            payload.pop("_external_task_key")
        elif evidence == "wrong":
            payload["_external_task_key"] = str(uuid4())
        change.status, change.executed_at, change.result = "executing", started, payload
        await db.flush()
        if evidence == "valid":
            identity = mcp_tools.McpSendIdentity(
                sessionmaker=app.state.sessionmaker, actor=AuthenticatedActor(*actor),
                project_id=change.project_id, conversation_id=change.conversation_id,
                task_id=task.id, task_key=task_ref["task_key"], queued_at=task.queued_at, execution_key=None,
                change_id=change.id, change_key=change_ref["change_key"], claim_id=claim_id,
                tool_call_id=change.tool_call_id, started_at=started,
                stamp=await read_visibility_stamp(db, change.project_id),
                task_snapshot=mcp_tools.task_snapshot(task), change_snapshot=mcp_tools.change_snapshot(change),
            )
            payload = {**payload, "_external_snapshot_mac": mcp_tools.identity_snapshot_mac(
                db.info.get("agent_view_hmac_key"), identity,
            )}
            change.result = payload
        await db.commit()
    return payload


@pytest.mark.parametrize("evidence", ["missing", "wrong"])
async def test_retry_rejects_legacy_or_mismatched_external_claim(app, client, fake_llm, retry_mcp, evidence):
    _, _, url, task, change = await _cancelled_external(app, client, fake_llm, retry_mcp)
    task_id, change_id = task["id"], change["id"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task))).status_code == 200
    payload = await _mark_executing(app, client, task, change, evidence=evidence)
    response = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task))
    assert response.status_code == 409, response.text
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, change_id)
        assert (change.status, change.turn_task_id, change.result) == ("executing", task_id, payload)
        assert await db.scalar(select(Task.id).where(Task.retry_of_id == task_id)) is None
    assert retry_mcp.calls == []


async def test_stale_proven_external_cannot_rebind_or_resend(app, client, fake_llm, retry_mcp):
    _, _, url, task, change = await _cancelled_external(app, client, fake_llm, retry_mcp)
    task_id, change_id = task["id"], change["id"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task))).status_code == 200
    payload = await _mark_executing(app, client, task, change)
    calls_before = len(fake_llm.tool_calls)
    response = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task))
    assert response.status_code == 409 and response.json()["code"] == "external_outcome_unknown", response.text
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, change_id)
        assert (change.status, change.turn_task_id, change.turn_task_key, change.result) == (
            "executing", task_id, task["task_key"], payload,
        )
        assert await db.scalar(select(Task.id).where(Task.retry_of_id == task_id)) is None
    assert retry_mcp.calls == [] and len(fake_llm.tool_calls) == calls_before


async def test_retry_does_not_rebind_live_external_call(app, client, fake_llm, retry_mcp):
    entered, release = asyncio.Event(), asyncio.Event()

    async def call_external(_name, _arguments):
        entered.set()
        await release.wait()
        return "远端已执行"

    _, base, url, task, change = await _cancelled_external(
        app, client, fake_llm, retry_mcp, handler=call_external,
    )
    task_id, change_id = task["id"], change["id"]
    confirm = asyncio.create_task(client.post(f"{base}/changes/{change_id}/confirm", json=change_action(change)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task))).status_code == 200
        response = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task))
        assert response.status_code == 409, response.text
        async with app.state.sessionmaker() as db:
            change = await db.get(AgentChange, change_id)
            assert change.status == "executing" and change.turn_task_id == task_id
            assert await db.scalar(select(Task.id).where(Task.retry_of_id == task_id)) is None
        release.set()
        result = await asyncio.wait_for(confirm, 5)
        assert result.status_code == 200, result.text
        assert len(retry_mcp.calls) == 1
        async with app.state.sessionmaker() as db:
            change = await db.get(AgentChange, change_id)
            assert change.status == "executed" and change.turn_task_id == task_id
    finally:
        release.set()
        if not confirm.done():
            confirm.cancel()
            with suppress(asyncio.CancelledError):
                await confirm


async def test_retry_after_proven_external_completion_does_not_resend(app, client, fake_llm, retry_mcp):
    entered, release = asyncio.Event(), asyncio.Event()

    async def call_external(_name, _arguments):
        entered.set()
        await release.wait()
        return "远端已执行"

    _, base, url, task, change = await _cancelled_external(
        app, client, fake_llm, retry_mcp, handler=call_external,
    )
    confirmation = asyncio.create_task(client.post(
        f"{base}/changes/{change['id']}/confirm", json=change_action(change),
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancelled = await client.post(f"/api/v1/tasks/{task['id']}/cancel", params=task_params(task))
        assert cancelled.status_code == 200, cancelled.text
        release.set()
        confirmed = await asyncio.wait_for(confirmation, 5)
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
        assert confirmed.json()["result"] == {"text": "远端已执行"}
        fake_llm.push_tools([("text", {"text": "基于已完成结果继续"})])
        response = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
        assert response.status_code == 202, response.text
        retried = response.json()
        assert (await wait_task(client, retried))["status"] == "succeeded"
        async with app.state.sessionmaker() as db:
            stored = await db.get(AgentChange, change["id"])
            assert stored.status == "executed" and stored.result == {"text": "远端已执行"}
            assert stored.turn_task_id == retried["id"] and stored.turn_task_key == retried["task_key"]
        assert len(retry_mcp.calls) == 1
    finally:
        release.set()
        if not confirmation.done():
            confirmation.cancel()
            with suppress(asyncio.CancelledError):
                await confirmation


async def test_delayed_retry_rechecks_source_before_transferring_changes(app, client, fake_llm, retry_mcp):
    _, _, url, task, change = await _cancelled_external(app, client, fake_llm, retry_mcp)
    task_id, change_id = task["id"], change["id"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task))).status_code == 200
    engine = app.state.task_engine
    await engine.stop()
    response = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task))
    assert response.status_code == 202, response.text
    new_ref = response.json()
    new_id, new_key = new_ref["id"], new_ref["task_key"]
    await _mark_executing(app, client, task, change, minutes=0)
    async with app.state.sessionmaker() as db:
        new_task = await db.get(Task, new_id)
        queued_at = new_task.queued_at
    engine._running_rounds[new_id], engine._stopping = queued_at, False
    await engine.lease.register()
    await engine._claim_and_run(new_id, new_key)
    async with app.state.sessionmaker() as db:
        old_change = await db.get(AgentChange, change_id)
        new_task = await db.get(Task, new_id)
        assert old_change.status == "executing" and old_change.turn_task_id == task_id
        assert new_task.status == "failed"
    assert retry_mcp.calls == []


async def test_recovery_of_mixed_claims_rolls_back_when_conflict_is_caught(app, client, fake_llm, retry_mcp):
    _, _, url, task, change = await _cancelled_external(app, client, fake_llm, retry_mcp)
    task_id, change_id = task["id"], change["id"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task))).status_code == 200
    valid = await _mark_executing(app, client, task, change)
    async with app.state.sessionmaker() as db:
        first = await db.get(AgentChange, change_id)
        second = AgentChange(
            conversation_id=first.conversation_id, project_id=first.project_id, turn_task_id=task_id,
            tool_call_id="second-call", tool_name=first.tool_name, arguments={}, kind="external", preview={},
            status="executing", executed_at=first.executed_at, result={"_external_claim_id": str(uuid4())},
        )
        db.add(second)
        await db.commit()
        second_id = second.id
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        with pytest.raises(ConflictError):
            await get_handler("agent.turn").validate_retry(
                db, app.state.task_engine.ctx, user_id=task.user_id, task=task,
            )
        # 即使直接调用方捕获冲突后提交其他工作，不能保留半批恢复。
        await db.commit()
    async with app.state.sessionmaker() as db:
        first, second = await db.get(AgentChange, change_id), await db.get(AgentChange, second_id)
        assert (first.status, first.turn_task_id, first.result) == ("executing", task_id, valid)
        assert second.status == "executing" and second.turn_task_id == task_id
    assert retry_mcp.calls == []


async def test_retry_rejects_source_identity_replacement_after_queueing(app, client, fake_llm, retry_mcp):
    _, _, url, task, change = await _cancelled_external(app, client, fake_llm, retry_mcp)
    task_id, change_id = task["id"], change["id"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task))).status_code == 200
    engine = app.state.task_engine
    await engine.stop()
    response = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task))
    assert response.status_code == 202, response.text
    new_ref = response.json()
    new_id, new_key = new_ref["id"], new_ref["task_key"]
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=str(uuid4())))
        await db.commit()
        new_task = await db.get(Task, new_id)
        queued_at = new_task.queued_at
    engine._running_rounds[new_id], engine._stopping = queued_at, False
    await engine.lease.register()
    await engine._claim_and_run(new_id, new_key)
    async with app.state.sessionmaker() as db:
        change, new_task = await db.get(AgentChange, change_id), await db.get(Task, new_id)
        assert change.status == "proposed" and change.turn_task_id == task_id
        assert new_task.status == "failed"
    assert retry_mcp.calls == []


async def test_retry_can_resume_after_completed_rebinding_and_old_task_removal(app, client, fake_llm, retry_mcp):
    _, base, url, task, change = await _cancelled_external(app, client, fake_llm, retry_mcp)
    task_id, change_id = task["id"], change["id"]
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task))).status_code == 200
    fake_llm.push_tools([("text", {"text": "继续完成"})])
    retry = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task))
    assert retry.status_code == 202, retry.text
    new_task = retry.json()
    new_id = new_task["id"]
    assert (await wait_task(client, new_task, statuses=("paused", "failed")))["status"] == "paused"
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change_id)).turn_task_id == new_id
    assert (await client.delete(f"/api/v1/tasks/{task_id}", params=task_params(task))).status_code == 204
    detail = await client.get(url)
    assert detail.status_code == 200, detail.text
    updated = next(item for item in detail.json()["changes"] if item["id"] == change_id)
    assert updated["change_key"] == change["change_key"] and updated["turn_task_key"] == new_task["task_key"]
    confirmed = await client.post(f"{base}/changes/{change_id}/confirm", json=change_action(updated))
    assert confirmed.status_code == 200, confirmed.text
    assert (await wait_task(client, new_task))["status"] == "succeeded"
    assert len(retry_mcp.calls) == 1
