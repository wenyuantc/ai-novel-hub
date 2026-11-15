"""PostgreSQL 真实行锁和重复读下的 Agent 执行体隔离。"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.errors import ConflictError
from app.models import AgentMessage, Task
from app.services.agent import runner, transactions
from tests.test_agent_turn_identity_runner import _old_job
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def _replace(app, job):
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(Task, job.task_id))
        await db.flush()
        replacement = Task(id=job.task_id, user_id=job.user_id, project_id=job.project_id,
                           kind="agent.turn", status="running", execution_key=str(uuid4()),
                           checkpoint={"replacement": True})
        db.add(replacement)
        await db.commit()
        return replacement.statistics_key


async def test_guarded_result_commit_precedes_task_id_reuse(postgres_collaboration_apps, monkeypatch):
    app, other_app = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        job = await _old_job(app, client)
    locked, release = asyncio.Event(), asyncio.Event()
    original = runner._lock_execution
    holder_pid = None

    async def pause_after_lock(db, identity):
        nonlocal holder_pid
        await original(db, identity)
        holder_pid = await db.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()

    monkeypatch.setattr(runner, "_lock_execution", pause_after_lock)

    async def write_result():
        async with transactions.session(app.state.sessionmaker) as db:
            await runner._result(db, job, {"id": "call", "name": "read"}, {"ok": True})

    writer = asyncio.create_task(write_result())
    replacer = None
    try:
        await asyncio.wait_for(locked.wait(), 10)
        replacer = asyncio.create_task(_replace(other_app, job))
        await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, holder_pid), 10)
        assert not replacer.done()
        release.set()
        await asyncio.wait_for(writer, 10)
        assert await asyncio.wait_for(replacer, 10) != job.task_key
        async with app.state.sessionmaker() as db:
            message = await db.scalar(select(AgentMessage))
            assert message.turn_task_id is None and message.turn_task_key == job.task_key
            assert (await db.get(Task, job.task_id)).checkpoint == {"replacement": True}
    finally:
        release.set()
        for pending in (writer, replacer):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(pending for pending in (writer, replacer) if pending is not None),
                             return_exceptions=True)


async def test_repeatable_read_rejects_task_replaced_after_snapshot(postgres_collaboration_apps):
    app, other_app = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        job = await _old_job(app, client)
    async with transactions.session(app.state.sessionmaker) as db:
        assert await db.scalar(select(Task.statistics_key).where(Task.id == job.task_id)) == job.task_key
        assert await _replace(other_app, job) != job.task_key
        try:
            await runner._result(db, job, {"id": "call", "name": "read"}, {"ok": True})
        except DBAPIError as exc:
            assert transactions.is_serialization_error(exc)
        except ConflictError:
            pass
        else:
            pytest.fail("旧快照不应在已复用的任务 ID 上提交")
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(AgentMessage))) == []
        assert (await db.get(Task, job.task_id)).checkpoint == {"replacement": True}
