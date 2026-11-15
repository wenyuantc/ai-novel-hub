"""PostgreSQL 验证外部操作和任务删除之间的真实项目锁顺序。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import text, update

from app.core.errors import ConflictError
from app.models import AgentChange, AgentConversation, Task
from app.services import setting_ai_visibility, task_service, task_statistics_service
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps  # noqa: F401


@pytest.mark.parametrize("linked", ["id", "uuid", "turn_uuid"])
@pytest.mark.parametrize("outcome", ["executing", "unknown"])
async def test_task_delete_waits_for_external_claim_postgresql(postgres_collaboration_apps, linked, outcome):  # noqa: F811
    app, other_app = postgres_collaboration_apps
    maker = app.state.sessionmaker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        actor = await register(client, "pg-agent-evidence")
        project = await create_project(client)
    async with maker() as db:
        task = Task(user_id=actor["id"], project_id=project["id"], kind="agent.turn", status="cancelled")
        db.add(task)
        await task_statistics_service.record_created(db, task)
        conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        change = AgentChange(
            project_id=project["id"], conversation_id=conversation.id,
            turn_task_id=task.id if linked == "id" else None, tool_call_id="protected", tool_name="call",
            turn_task_key=task.statistics_key if linked == "turn_uuid" else None,
            kind="external", status="proposed", arguments={}, preview={},
        )
        db.add(change)
        await db.commit()
        task_id, task_key, change_id = task.id, task.statistics_key, change.id
    entered, release = asyncio.Event(), asyncio.Event()
    holder_pid = asyncio.get_running_loop().create_future()

    async def claim_external():
        async with maker() as db:
            await setting_ai_visibility.lock_author_visibility_write(db, actor["id"], project["id"], manage=False)
            holder_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
            result = {"_external_task_key": task_key} if linked == "uuid" else {}
            if outcome == "unknown":
                result["_external_outcome_unknown"] = None
            await db.execute(update(AgentChange).where(AgentChange.id == change_id).values(
                status="executing" if outcome == "executing" else "failed", result=result,
            ))
            entered.set()
            await release.wait()
            await db.commit()

    async def delete_task():
        # 同一应用内授权协调器先在进程内串行两者，观察不到数据库阻塞；
        # 用共享数据库的另一实例删除，才能证明数据库锁本身让删除等待外部占用提交。
        async with other_app.state.sessionmaker() as db:
            with pytest.raises(ConflictError):
                await task_service.delete_task(db, actor["id"], task_id, expected_task_key=task_key)

    holder = asyncio.create_task(claim_external())
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 8)
        deletion = asyncio.create_task(delete_task())
        await asyncio.wait_for(_wait_for_blocker(maker, await holder_pid), 8)
        assert not deletion.done()
        release.set()
        await asyncio.wait_for(holder, 8)
        await asyncio.wait_for(deletion, 8)
        async with maker() as db:
            assert (await db.get(Task, task_id)).statistics_key == task_key
            expected_status = "executing" if outcome == "executing" else "failed"
            assert (await db.get(AgentChange, change_id)).status == expected_status
    finally:
        release.set()
        for pending in (holder, deletion):
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
