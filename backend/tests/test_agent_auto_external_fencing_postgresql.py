"""自动外部占用的旧执行体清理与新身份修改共享 PostgreSQL 写屏障。"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.models import AgentChange, AgentConversation, Task
from app.services import project_lifecycle
from app.services.agent import mcp_tools, runner
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_user_deletion_postgresql",)


async def _seed(app, client):
    owner = await register(client, "auto-external-pg")
    project = await create_project(client)
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="queued",
                    queued_at=now,
                    input={"_agent_actor_token_version": 0, "_task_actor_token_version": 0})
        db.add_all([conversation, task])
        await db.flush()
        execution_key = str(uuid4())
        execution = from_queued_task(task, execution_key)
        task.status, task.execution_key = "running", execution_key
        job = runner.AgentJob(task.id, owner["id"], project["id"], conversation.id, 1, None, None, None,
                              task_key=task.statistics_key, queued_at=now, execution_key=task.execution_key,
                              actor_token_version=0, execution=execution,
                              execution_state=runner._AgentExecutionState(execution))
        claim_id, change_key = str(uuid4()), str(uuid4())
        result = {"_external_claim_id": claim_id, "_external_task_key": job.task_key,
                  "_external_execution": {"queued_at": now.isoformat(), "execution_key": job.execution_key,
                                          "actor_token_version": 0}}
        change = AgentChange(change_key=change_key, conversation_id=conversation.id, project_id=project["id"],
                             turn_task_id=task.id, turn_task_key=job.task_key, tool_call_id="call", tool_name="tool",
                             kind="external", status="executing", executed_at=now, result=result)
        db.add(change)
        await db.flush()
        task_snapshot = runner._execution_task_snapshot(execution)
        change_snapshot = mcp_tools.change_snapshot(change)
        mac = mcp_tools.external_snapshot_mac(
            db.info.get("agent_view_hmac_key"), actor_id=job.user_id, token_version=0, project_id=job.project_id,
            conversation_id=job.conversation_id, task_id=job.task_id, task_key=job.task_key, queued_at=now,
            execution_key=execution_key, change_id=change.id, change_key=change_key, claim_id=claim_id,
            tool_call_id="call", started_at=now, task_evidence=task_snapshot, change_evidence=change_snapshot,
        )
        change.result = {**result, "_external_snapshot_mac": mac}
        claim = runner._AutoExternalClaim(job, change.id, change_key, claim_id, "call", now, now.isoformat(),
                                          execution, change_snapshot, task_snapshot, mac)
        await db.commit()
    return claim


@pytest.mark.parametrize("replacement", ["claim", "execution"])
async def test_new_identity_commit_precedes_old_cleanup(postgres_user_delete_apps, replacement):
    first_app, second_app = postgres_user_delete_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first_app), base_url="http://testserver") as client:
        claim = await _seed(first_app, client)
        pending = None
        async with first_app.state.sessionmaker() as db:
            try:
                await project_lifecycle.lock_record(db, claim.job.project_id)
                change = await db.get(AgentChange, claim.change_id)
                task = await db.get(Task, claim.job.task_id)
                new_claim = str(uuid4())
                if replacement == "claim":
                    new_execution = claim.job.execution_key
                else:
                    new_execution = task.execution_key = str(uuid4())
                new_mac = mcp_tools.external_snapshot_mac(
                    db.info.get("agent_view_hmac_key"), actor_id=claim.job.user_id, token_version=0,
                    project_id=claim.job.project_id, conversation_id=claim.job.conversation_id,
                    task_id=claim.job.task_id, task_key=claim.job.task_key, queued_at=claim.job.queued_at,
                    execution_key=new_execution, change_id=claim.change_id, change_key=claim.change_key,
                    claim_id=new_claim, tool_call_id=claim.call_id, started_at=claim.started_at,
                    task_evidence=claim.task_snapshot, change_evidence=claim.change_snapshot,
                )
                result = {"_external_claim_id": new_claim, "_external_task_key": claim.job.task_key,
                          "_external_execution": {"queued_at": claim.queued_at, "execution_key": new_execution,
                                                  "actor_token_version": 0}, "_external_snapshot_mac": new_mac}
                change.result = result
                await db.flush()
                holder = await db.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(runner._settle_external_claim(second_app.state.task_engine.ctx, claim,
                                                                            unknown=True))
                await asyncio.wait_for(_wait_for_blocker(second_app.state.sessionmaker, holder), 15)
                await db.commit()
                await asyncio.wait_for(pending, 15)
                async with second_app.state.sessionmaker() as verify:
                    saved = await verify.get(AgentChange, claim.change_id)
                    assert saved.status == "executing" and saved.result == result
            finally:
                await db.rollback()
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_old_cleanup_commit_precedes_new_identity(postgres_user_delete_apps, monkeypatch):
    first_app, second_app = postgres_user_delete_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first_app), base_url="http://testserver") as client:
        claim = await _seed(first_app, client)
        entered, release = asyncio.Event(), asyncio.Event()
        lock_record = project_lifecycle.lock_record
        holder = {}
        cleanup = None

        async def gated_lock(db, project_id):
            parent = await lock_record(db, project_id)
            if asyncio.current_task() is cleanup:
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
                await release.wait()
            return parent

        async def replace_claim():
            async with second_app.state.sessionmaker() as db:
                await project_lifecycle.lock_record(db, claim.job.project_id)
                change = await db.get(AgentChange, claim.change_id)
                assert change.status == "failed" and change.result["_external_outcome_unknown"] is True
                new_claim = str(uuid4())
                new_mac = mcp_tools.external_snapshot_mac(
                    db.info.get("agent_view_hmac_key"), actor_id=claim.job.user_id, token_version=0,
                    project_id=claim.job.project_id, conversation_id=claim.job.conversation_id,
                    task_id=claim.job.task_id, task_key=claim.job.task_key, queued_at=claim.job.queued_at,
                    execution_key=claim.job.execution_key, change_id=claim.change_id, change_key=claim.change_key,
                    claim_id=new_claim, tool_call_id=claim.call_id, started_at=claim.started_at,
                    task_evidence=claim.task_snapshot, change_evidence=claim.change_snapshot,
                )
                change.status = "executing"
                change.result = {"_external_claim_id": new_claim, "_external_task_key": claim.job.task_key,
                                 "_external_execution": {"queued_at": claim.queued_at,
                                                         "execution_key": claim.job.execution_key,
                                                         "actor_token_version": 0},
                                 "_external_snapshot_mac": new_mac}
                await db.commit()
                return new_claim

        monkeypatch.setattr(project_lifecycle, "lock_record", gated_lock)
        cleanup = asyncio.create_task(runner._settle_external_claim(first_app.state.task_engine.ctx, claim,
                                                                     unknown=True))
        writer = None
        try:
            await asyncio.wait_for(entered.wait(), 15)
            writer = asyncio.create_task(replace_claim())
            await asyncio.wait_for(_wait_for_blocker(second_app.state.sessionmaker, holder["pid"]), 15)
            release.set()
            await asyncio.wait_for(cleanup, 15)
            new_claim = await asyncio.wait_for(writer, 15)
            async with first_app.state.sessionmaker() as db:
                saved = await db.get(AgentChange, claim.change_id)
                assert saved.status == "executing" and saved.result["_external_claim_id"] == new_claim
        finally:
            release.set()
            for pending in (cleanup, writer):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
