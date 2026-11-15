"""外部调用恢复必须针对原始不可复用占用，不得误伤后来的一次执行。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentChange, AgentConversation, Task
from app.services.agent import mcp_tools, service, transactions
from tests.helpers import create_project, register


async def _sign(db, task, change):
    await db.flush()
    values = change.result
    mac = mcp_tools.external_snapshot_mac(
        db.info["agent_view_hmac_key"], actor_id=task.user_id,
        token_version=task.input[service._AGENT_ACTOR_TOKEN_VERSION], project_id=task.project_id,
        conversation_id=change.conversation_id, task_id=task.id, task_key=task.statistics_key,
        queued_at=task.queued_at, execution_key=task.execution_key if "_external_execution" in values else None,
        change_id=change.id, change_key=change.change_key, claim_id=values[transactions.EXTERNAL_CLAIM_KEY],
        tool_call_id=change.tool_call_id, started_at=change.executed_at,
        task_evidence=mcp_tools.task_snapshot(task), change_evidence=mcp_tools.change_snapshot(change),
    )
    change.result = {**values, service._EXTERNAL_SNAPSHOT_MAC: mac}
    return mac


def _actor_input(version=0):
    return {service._AGENT_ACTOR_TOKEN_VERSION: version, service._TASK_ACTOR_TOKEN_VERSION: version}


@pytest.mark.parametrize("replacement", ["claim", "task", "change", "extra"])
async def test_recover_external_does_not_interrupt_replaced_execution(app, client, monkeypatch, replacement):
    owner = await register(client, f"recovery-{replacement}")
    project = await create_project(client)
    old_claim, new_claim = str(uuid4()), str(uuid4())
    started = datetime.now(UTC) - timedelta(minutes=3)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="paused",
                    input=_actor_input())
        db.add_all([conversation, task])
        await db.flush()
        task_key = task.statistics_key
        change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task_key,
                             tool_call_id="same-call", tool_name="external", kind="external", status="executing",
                             executed_at=started, result={transactions.EXTERNAL_CLAIM_KEY: old_claim,
                                                          service._EXTERNAL_TASK_KEY: task_key})
        db.add(change)
        old_mac = await _sign(db, task, change)
        await db.commit()
        task_id, change_id, conversation_id = task.id, change.id, conversation.id

    original_execute = AsyncSession.execute
    interleaved = False
    async with app.state.sessionmaker() as db:
        async def swap_between_query_and_update(session, statement, *args, **kwargs):
            nonlocal interleaved
            result = await original_execute(session, statement, *args, **kwargs)
            if session is db and not interleaved and str(statement).lstrip().startswith("SELECT") \
                    and "FROM agent_changes" in str(statement):
                interleaved = True
                await db.rollback()  # 释放 SQLite 读锁，模拟 PostgreSQL 的并发提交。
                async with app.state.sessionmaker() as writer:
                    if replacement == "task":
                        row = await writer.get(Task, task_id)
                        row.statistics_key = str(uuid4())
                    elif replacement == "claim":
                        row = await writer.get(AgentChange, change_id)
                        row.result = {transactions.EXTERNAL_CLAIM_KEY: new_claim, service._EXTERNAL_TASK_KEY: task_key}
                    elif replacement == "extra":
                        row = await writer.get(AgentChange, change_id)
                        row.result = {**row.result, "_external_outcome_unknown": None}
                    else:
                        row = await writer.get(AgentChange, change_id)
                        created_at = row.created_at
                        await writer.delete(row)
                        await writer.flush()
                        writer.add(AgentChange(
                            id=change_id, conversation_id=conversation_id, project_id=project["id"],
                            turn_task_id=task_id, turn_task_key=task_key,
                            tool_call_id="same-call", tool_name="external", kind="external",
                            status="executing", executed_at=started, created_at=created_at,
                            result={transactions.EXTERNAL_CLAIM_KEY: new_claim, service._EXTERNAL_TASK_KEY: task_key},
                        ))
                    await writer.commit()
            return result

        monkeypatch.setattr(AsyncSession, "execute", swap_between_query_and_update)
        recovered = await service.recover_external(db, task_id=task_id, engine=app.state.task_engine)
        await db.commit()
        assert interleaved and recovered == 0

    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, change_id)
        assert stored.status == "executing"
        expected = {transactions.EXTERNAL_CLAIM_KEY: old_claim if replacement in ("task", "extra") else new_claim,
                    service._EXTERNAL_TASK_KEY: task_key}
        if replacement in ("task", "extra"):
            expected[service._EXTERNAL_SNAPSHOT_MAC] = old_mac
        if replacement == "extra":
            expected["_external_outcome_unknown"] = None
        assert stored.result == expected


async def test_recovery_skips_legacy_unproven_claims_but_recovers_proven_claim(app, client):
    owner = await register(client, "recovery-legacy")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="paused",
                    input=_actor_input())
        db.add_all([conversation, task])
        await db.flush()
        started = datetime.now(UTC) - timedelta(minutes=3)
        legacy = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task.statistics_key,
                             tool_call_id="old", tool_name="external", kind="external", status="executing",
                             executed_at=started, result={})
        proven = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task.statistics_key,
                             tool_call_id="new", tool_name="external", kind="external", status="executing",
                             executed_at=started, result={transactions.EXTERNAL_CLAIM_KEY: str(uuid4()),
                                                          service._EXTERNAL_TASK_KEY: task.statistics_key})
        db.add_all([legacy, proven])
        await _sign(db, task, proven)
        await db.commit()
        assert await service.recover_external(db, stale_only=False) == 1
        await db.commit()
        await db.refresh(legacy)
        await db.refresh(proven)
        assert legacy.status == "executing"
        assert proven.status == "failed" and proven.result["error"] == service.EXTERNAL_INTERRUPTED
        assert proven.result["_external_outcome_unknown"] is True
        assert proven.result["_external_claim_id"] and proven.result["_external_task_key"] == task.statistics_key


async def test_recovery_skips_claim_rebound_to_different_task_before_scan(app, client):
    owner = await register(client, "recovery-rebound")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        first = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="cancelled",
                     input=_actor_input())
        second = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="paused",
                      input=_actor_input())
        db.add_all([conversation, first, second])
        await db.flush()
        claim = str(uuid4())
        change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=second.id,
                             turn_task_key=first.statistics_key,
                             tool_call_id="rebound", tool_name="external", kind="external", status="executing",
                             executed_at=datetime.now(UTC) - timedelta(minutes=3),
                             result={transactions.EXTERNAL_CLAIM_KEY: claim,
                                     service._EXTERNAL_TASK_KEY: first.statistics_key})
        db.add(change)
        await db.commit()
        assert await service.recover_external(db, task_id=second.id, engine=app.state.task_engine) == 0
        await db.commit()
        await db.refresh(change)
        assert change.status == "executing" and change.result[transactions.EXTERNAL_CLAIM_KEY] == claim


async def test_recovery_sees_the_current_cleanup_scope_and_recovers_after_exit(app, client):
    owner = await register(client, "recovery-current-scope")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="paused",
                    input=_actor_input())
        db.add_all([conversation, task])
        await db.flush()
        change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task.statistics_key,
                             tool_call_id="self", tool_name="external", kind="external", status="executing",
                             executed_at=datetime.now(UTC) - timedelta(minutes=3),
                             result={transactions.EXTERNAL_CLAIM_KEY: str(uuid4()),
                                     service._EXTERNAL_TASK_KEY: task.statistics_key})
        db.add(change)
        await _sign(db, task, change)
        await db.commit()
        engine = app.state.task_engine
        with engine.cleanup_scope(task):
            assert not engine.project_work(project["id"])  # 此 API 排除了当前协程。
            assert await service.recover_external(db, task_id=task.id, task_key=task.statistics_key, engine=engine) == 0
        assert await service.recover_external(db, task_id=task.id, task_key=task.statistics_key, engine=engine) == 1
        await db.commit()


@pytest.mark.parametrize("variant", ["valid", "replaced_execution", "replaced_queue", "malformed", "null",
                                    "missing_actor", "replaced_actor", "boolean_actor", "result_extra"])
async def test_automatic_external_recovery_requires_and_preserves_original_execution(app, client, variant):
    owner = await register(client, f"recovery-auto-{variant}")
    project = await create_project(client)
    queued = datetime.now(UTC) - timedelta(minutes=5)
    execution_key, claim_id = str(uuid4()), str(uuid4())
    marker = {"queued_at": queued.isoformat(), "execution_key": execution_key, "actor_token_version": 3}
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="running",
                    queued_at=queued, execution_key=execution_key, input={service._AGENT_ACTOR_TOKEN_VERSION: 3,
                                                                         service._TASK_ACTOR_TOKEN_VERSION: 3})
        if variant == "replaced_execution":
            task.execution_key = str(uuid4())
        if variant == "replaced_queue":
            task.queued_at = queued + timedelta(seconds=1)
        if variant == "malformed":
            marker = {**marker, "unexpected": "unproven"}
        if variant == "null":
            marker = None
        if variant == "missing_actor":
            marker.pop("actor_token_version")
        if variant == "replaced_actor":
            task.input = {service._AGENT_ACTOR_TOKEN_VERSION: 4, service._TASK_ACTOR_TOKEN_VERSION: 3}
        if variant == "boolean_actor":
            task.input = {service._AGENT_ACTOR_TOKEN_VERSION: 1, service._TASK_ACTOR_TOKEN_VERSION: 1}
            marker["actor_token_version"] = True
        db.add_all([conversation, task])
        await db.flush()
        original_result = {transactions.EXTERNAL_CLAIM_KEY: claim_id, service._EXTERNAL_TASK_KEY: task.statistics_key,
                           "_external_execution": marker}
        if variant == "result_extra":
            original_result["_external_outcome_unknown"] = None
        change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task.statistics_key, tool_call_id="auto", tool_name="external",
                             kind="external", status="executing", executed_at=queued, result=original_result)
        db.add(change)
        await _sign(db, task, change)
        original_result = dict(change.result)
        await db.commit()
        count = await service.recover_external(db, task_id=task.id, task_key=task.statistics_key, stale_only=False)
        await db.commit()
        await db.refresh(change)
        if variant == "valid":
            assert count == 1 and change.status == "failed"
            assert change.result["_external_execution"] == marker
            assert change.result["_external_outcome_unknown"] is True
        else:
            assert count == 0 and change.status == "executing"
            assert change.result == original_result


@pytest.mark.parametrize("replacement", ["task_execution", "task_queue", "task_actor", "marker", "marker_extra",
                                        "manual_null", "result_extra"])
async def test_automatic_external_recovery_cas_checks_execution_after_scan(app, client, monkeypatch, replacement):
    owner = await register(client, f"auto-race-{replacement}")
    project = await create_project(client)
    queued, execution_key, claim_id = datetime.now(UTC) - timedelta(minutes=5), str(uuid4()), str(uuid4())
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="running",
                    queued_at=queued, execution_key=execution_key, input={service._AGENT_ACTOR_TOKEN_VERSION: 3,
                                                                         service._TASK_ACTOR_TOKEN_VERSION: 3})
        if replacement == "manual_null":
            task.execution_key = None
        db.add_all([conversation, task])
        await db.flush()
        result = {transactions.EXTERNAL_CLAIM_KEY: claim_id, service._EXTERNAL_TASK_KEY: task.statistics_key}
        if replacement != "manual_null":
            result["_external_execution"] = {
                "queued_at": queued.isoformat(), "execution_key": execution_key, "actor_token_version": 3,
            }
        change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task.statistics_key, tool_call_id="race", tool_name="external",
                             kind="external", status="executing", executed_at=queued, result=result)
        db.add(change)
        await _sign(db, task, change)
        await db.commit()
        task_id, task_key, change_id = task.id, task.statistics_key, change.id

    original_execute = AsyncSession.execute
    interleaved = False
    async with app.state.sessionmaker() as db:
        async def replace_execution(session, statement, *args, **kwargs):
            nonlocal interleaved
            result = await original_execute(session, statement, *args, **kwargs)
            if session is db and not interleaved and str(statement).lstrip().startswith("SELECT") \
                    and "FROM agent_changes" in str(statement):
                interleaved = True
                await db.rollback()
                async with app.state.sessionmaker() as writer:
                    if replacement == "task_execution":
                        (await writer.get(Task, task_id)).execution_key = str(uuid4())
                    elif replacement == "task_queue":
                        (await writer.get(Task, task_id)).queued_at = queued + timedelta(seconds=1)
                    elif replacement == "task_actor":
                        (await writer.get(Task, task_id)).input = {service._AGENT_ACTOR_TOKEN_VERSION: True,
                                                                   service._TASK_ACTOR_TOKEN_VERSION: 3}
                    elif replacement == "result_extra":
                        row = await writer.get(AgentChange, change_id)
                        row.result = {**row.result, "_external_outcome_unknown": None}
                    else:
                        row = await writer.get(AgentChange, change_id)
                        marker = (None if replacement == "manual_null" else {
                            "queued_at": queued.isoformat(),
                            "execution_key": str(uuid4()) if replacement == "marker" else execution_key,
                            "actor_token_version": 3,
                        })
                        if replacement == "marker_extra":
                            marker["unproven"] = True
                        row.result = {**row.result, "_external_execution": marker}
                    await writer.commit()
            return result

        monkeypatch.setattr(AsyncSession, "execute", replace_execution)
        assert await service.recover_external(db, task_id=task_id, task_key=task_key, stale_only=False) == 0
        await db.commit()
        assert interleaved
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, change_id)
        assert change.status == "executing"
        assert change.result[transactions.EXTERNAL_CLAIM_KEY] == claim_id


@pytest.mark.parametrize("variant", ["valid", "legacy", "key_rotated", "task_tampered", "change_tampered",
                                    "mac_tampered", "result_extra"])
async def test_recovery_requires_original_snapshot_mac(app, client, variant):
    owner = await register(client, f"mac-{variant}")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="paused",
                    input=_actor_input(), checkpoint={"unchanged": True})
        db.add_all([conversation, task])
        await db.flush()
        change = AgentChange(conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task.statistics_key, tool_call_id="mac", tool_name="external",
                             kind="external", status="executing", arguments={"to": "原目标"},
                             preview={"title": "原预览"}, executed_at=datetime.now(UTC) - timedelta(minutes=3),
                             result={transactions.EXTERNAL_CLAIM_KEY: str(uuid4()),
                                     service._EXTERNAL_TASK_KEY: task.statistics_key})
        db.add(change)
        if variant != "legacy":
            await _sign(db, task, change)
        await db.commit()
        if variant == "key_rotated":
            db.info["agent_view_hmac_key"] = b"r" * 32
        elif variant == "task_tampered":
            task.input = {**task.input, "unexpected": "替换"}
            await db.commit()
        elif variant == "change_tampered":
            change.arguments = {"to": "新目标"}
            await db.commit()
        elif variant == "mac_tampered":
            change.result = {**change.result, service._EXTERNAL_SNAPSHOT_MAC: "f" * 64}
            await db.commit()
        elif variant == "result_extra":
            change.result = {**change.result, "_external_outcome_unknown": None}
            await db.commit()
        before = dict(change.result)
        count = await service.recover_external(db, task_id=task.id, task_key=task.statistics_key, stale_only=False)
        await db.commit()
        await db.refresh(change)
        if variant == "valid":
            assert count == 1 and change.status == "failed"
            assert change.result[service._EXTERNAL_SNAPSHOT_MAC] == before[service._EXTERNAL_SNAPSHOT_MAC]
            assert change.result[service._EXTERNAL_OUTCOME_UNKNOWN] is True
        else:
            assert count == 0 and change.status == "executing" and change.result == before
