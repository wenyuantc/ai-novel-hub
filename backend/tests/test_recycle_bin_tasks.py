"""使用事件屏障验证删除必须等待真实执行链，而不是只看数据库终态。"""

import asyncio
from contextlib import suppress

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from app.models import AgentChange, Chapter, KbDocument, Project, Task
from app.services import task_service
from app.services.agent import service as agent_service
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, create_ai_config, create_project, register, stream_task
from tests.mcp_fakes import FakeMcpConnector
from tests.test_agent_mcp_skills import _stuck_external, call, send
from tests.test_agent_mcp_skills import setup as setup_agent
from tests.test_recycle_bin import RECYCLE, item
from tests.test_recycle_bin import admin as admin


async def setup_chapter(client):
    owner = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (
        await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章", "content": "原稿"})
    ).json()
    return owner, project, chapter


async def generate(client, chapter):
    response = await client.post(
        f"/api/v1/chapters/{chapter['id']}/generate", json={"target_words": 300, "auto_continue": False}
    )
    assert response.status_code == 202, response.text
    return response.json()


async def test_running_task_stops_before_trash_and_restore_does_not_restart(app, admin, client, fake_llm):
    _, project, chapter = await setup_chapter(client)
    gate = Gate()
    fake_llm.push(["尚未保存的片段", gate, "不得继续"])
    task = await generate(client, chapter)
    await gate.wait_reached()
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    assert fake_llm.aborted == 1
    assert not app.state.task_engine.project_work(project["id"])
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task["id"])).status == "cancelled"
        row = await db.get(Chapter, chapter["id"])
        assert row.status == "draft" and row.content == "原稿"
    recycled = await item(admin, project["id"])
    before = len(fake_llm.calls)
    assert (
        await admin.post(
            f"{RECYCLE}/{project['id']}/restore", json={"deleted_at": recycled["deleted_at"], "confirm": True}
        )
    ).status_code == 204
    assert len(fake_llm.calls) == before
    assert (await client.get("/api/v1/tasks", params={"active": True})).json() == []


async def test_queued_and_paused_tasks_are_both_cancelled(app, admin, client):
    owner, project, chapter = await setup_chapter(client)
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        (await db.get(Chapter, chapter["id"])).status = "generating"
        queued = Task(
            user_id=owner["id"],
            project_id=project["id"],
            chapter_id=chapter["id"],
            kind="chapter.generate",
            status="queued",
            input={"chapter_id": chapter["id"], "prev_status": "draft"},
        )
        paused = Task(
            user_id=owner["id"],
            project_id=project["id"],
            chapter_id=chapter["id"],
            kind="chapter.analyze",
            status="paused",
            input={},
        )
        db.add_all([queued, paused])
        await db.commit()
        ids = [queued.id, paused.id]
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        assert [t.status for t in await db.scalars(select(Task).where(Task.id.in_(ids)).order_by(Task.id))] == [
            "cancelled",
            "cancelled",
        ]
        assert (await db.get(Chapter, chapter["id"])).status == "draft"


async def test_concurrent_queued_cancel_cleanup_is_waited_and_new_writes_are_blocked(app, client, monkeypatch):
    owner, project, chapter = await setup_chapter(client)
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        (await db.get(Chapter, chapter["id"])).status = "generating"
        task = Task(
            user_id=owner["id"],
            project_id=project["id"],
            chapter_id=chapter["id"],
            kind="chapter.generate",
            status="queued",
            input={"chapter_id": chapter["id"], "prev_status": "draft"},
        )
        db.add(task)
        await db.commit()
        task_id = task.id
    entered, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handler = get_handler("chapter.generate")
    original_abort = handler.on_abort
    original_stop = task_service.stop_project_tasks

    async def abort(ctx, task):
        entered.set()
        await release.wait()
        await original_abort(ctx, task)

    async def stop(db, engine, pid):
        deleting.set()
        return await original_stop(db, engine, pid)

    monkeypatch.setattr(handler, "on_abort", abort)
    monkeypatch.setattr(task_service, "stop_project_tasks", stop)
    cancellation = asyncio.create_task(client.post(f"/api/v1/tasks/{task_id}/cancel"))
    await asyncio.wait_for(entered.wait(), 3)
    deletion = asyncio.create_task(client.delete(f"/api/v1/projects/{project['id']}"))
    try:
        await asyncio.wait_for(deleting.wait(), 3)
        assert not deletion.done()
        assert (await client.patch(f"/api/v1/projects/{project['id']}", json={"title": "禁止修改"})).status_code == 409
        assert (await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})).status_code == 404
        assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 409
    finally:
        release.set()
    assert (await cancellation).status_code == 200
    assert (await asyncio.wait_for(deletion, 5)).status_code == 204
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).status == "draft"


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_database_terminal_task_still_waits_for_success_callback(app, client, fake_llm, monkeypatch):
    _, project, chapter = await setup_chapter(client)
    entered, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handler = get_handler("chapter.generate")
    original_success = handler.on_success
    original_stop = task_service.stop_project_tasks

    async def success(ctx, task):
        entered.set()
        await release.wait()
        await original_success(ctx, task)

    async def stop(db, engine, pid):
        deleting.set()
        return await original_stop(db, engine, pid)

    monkeypatch.setattr(handler, "on_success", success)
    monkeypatch.setattr(task_service, "stop_project_tasks", stop)
    fake_llm.push("已经保存的新正文")
    task = await generate(client, chapter)
    await asyncio.wait_for(entered.wait(), 5)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task["id"])).status == "succeeded"
    deletion = asyncio.create_task(client.delete(f"/api/v1/projects/{project['id']}"))
    try:
        await asyncio.wait_for(deleting.wait(), 3)
        assert not deletion.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(deletion, 5)).status_code == 204
    async with app.state.sessionmaker() as db:
        tasks = list(await db.scalars(select(Task).where(Task.project_id == project["id"])))
        assert len(tasks) == 1  # 成功回调没有绕过删除屏障派生分析任务
        assert (await db.get(Chapter, chapter["id"])).content == "已经保存的新正文"


async def test_timeout_or_cancelled_request_never_leaves_a_tombstone(app, client, fake_llm, monkeypatch):
    _, project, chapter = await setup_chapter(client)
    entered, release = asyncio.Event(), asyncio.Event()
    handler = get_handler("chapter.generate")
    original_success = handler.on_success

    async def success(ctx, task):
        entered.set()
        await release.wait()
        await original_success(ctx, task)

    monkeypatch.setattr(handler, "on_success", success)
    fake_llm.push("已保存")
    await generate(client, chapter)
    await asyncio.wait_for(entered.wait(), 5)
    engine = app.state.task_engine
    engine.cancel_wait_seconds = 0.03
    response = await client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped"
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        assert row.deleted_at is None and row.deletion_started_at is None and row.deletion_token is None
    # 再次删除时模拟 HTTP 取消，可靠 finally 同样解除持久化意图。
    started = asyncio.Event()
    original_stop = task_service.stop_project_tasks

    async def stop(db, eng, pid):
        started.set()
        return await original_stop(db, eng, pid)

    monkeypatch.setattr(task_service, "stop_project_tasks", stop)
    engine.cancel_wait_seconds = 5
    deletion = asyncio.create_task(client.delete(f"/api/v1/projects/{project['id']}"))
    await asyncio.wait_for(started.wait(), 3)
    deletion.cancel()
    with suppress(asyncio.CancelledError):
        await deletion
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        assert row.deleted_at is None and row.deletion_started_at is None and row.deletion_token is None
    release.set()
    work = engine.project_work(project["id"])
    if work:
        await asyncio.wait(work, timeout=3)
    assert (await client.get(f"/api/v1/projects/{project['id']}")).status_code == 200


async def test_failed_abort_does_not_claim_deletion_succeeded(app, client, monkeypatch):
    owner, project, chapter = await setup_chapter(client)
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        task = Task(
            user_id=owner["id"],
            project_id=project["id"],
            chapter_id=chapter["id"],
            kind="chapter.generate",
            status="queued",
            input={"chapter_id": chapter["id"], "prev_status": "draft"},
        )
        db.add(task)
        await db.commit()
    handler = get_handler("chapter.generate")

    async def fail(ctx, task):
        raise RuntimeError("测试失败，不含实际用户内容")

    monkeypatch.setattr(handler, "on_abort", fail)
    response = await client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped"
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        assert row.deleted_at is None and row.deletion_started_at is None


async def test_external_confirmation_request_is_drained_before_trash(app, admin, client, fake_llm, monkeypatch):
    entered, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector

    async def remote(name, arguments):
        entered.set()
        await release.wait()
        return "远端可能已经执行"

    project, base, url, _ = await setup_agent(client, connector, handler=remote)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    task_id = await send(client, url)
    assert (await stream_task(client, task_id))[-1][0] == "paused"
    change = (await client.get(url)).json()["changes"][0]
    original_stop = task_service.stop_project_tasks

    async def stop(db, engine, pid):
        deleting.set()
        return await original_stop(db, engine, pid)

    monkeypatch.setattr(task_service, "stop_project_tasks", stop)
    confirmation = asyncio.create_task(client.post(f"{base}/changes/{change['id']}/confirm"))
    await asyncio.wait_for(entered.wait(), 5)
    deletion = asyncio.create_task(admin.delete(f"/api/v1/projects/{project['id']}"))
    try:
        await asyncio.wait_for(deleting.wait(), 3)
        assert not deletion.done()
    finally:
        release.set()
    assert (await confirmation).status_code == 409
    assert (await asyncio.wait_for(deletion, 5)).status_code == 204
    assert connector.open == 0 and len(connector.calls) == 1
    async with app.state.sessionmaker() as db:
        row = await db.get(AgentChange, change["id"])
        assert row.status == "failed" and "远端可能已经执行" not in str(row.result)
        assert (await db.get(Task, task_id)).status == "cancelled"


async def test_running_abort_failure_keeps_terminal_state_and_next_delete_can_retry(app, client, fake_llm, monkeypatch):
    _, project, chapter = await setup_chapter(client)
    gate = Gate()
    fake_llm.push([gate, "不会生成"])
    task = await generate(client, chapter)
    await gate.wait_reached()
    handler = get_handler("chapter.generate")
    original = handler.on_abort
    attempts = 0

    async def fail_once(ctx, task):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("首次回退失败")
        await original(ctx, task)

    monkeypatch.setattr(handler, "on_abort", fail_once)
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 409
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task["id"])).status == "cancelled"
        assert (await db.get(Project, project["id"])).deleted_at is None
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    assert attempts == 2


async def test_kb_abort_database_failure_is_not_silently_treated_as_safe(app, client, monkeypatch):
    owner, project, _ = await setup_chapter(client)
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        document = KbDocument(
            project_id=project["id"],
            filename="设定.md",
            mime="text/markdown",
            sha256="0" * 64,
            text="原始设定",
            status="pending",
        )
        db.add(document)
        await db.flush()
        task = Task(
            user_id=owner["id"],
            project_id=project["id"],
            kind="kb.index",
            status="queued",
            input={"request": {"project_id": project["id"], "mode": "document", "document_id": document.id}},
        )
        db.add(task)
        await db.commit()
        document_id = document.id
    original = AsyncSession.execute
    failed = False

    async def fail_once(session, statement, *args, **kwargs):
        nonlocal failed
        if (
            not failed
            and isinstance(statement, Update)
            and statement.table.name == "kb_documents"
            and statement.compile().params.get("status") == "failed"
        ):
            failed = True
            raise RuntimeError("模拟文档回退写入故障")
        return await original(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_once)
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 409
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).deleted_at is None
        assert (await db.get(KbDocument, document_id)).status == "pending"
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        assert (await db.get(KbDocument, document_id)).status == "failed"


async def test_waiting_for_conversation_lock_cannot_recover_external_after_trash(app, client, fake_llm, monkeypatch):
    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    base, _, _, change = await _stuck_external(client, fake_llm, connector, app, minutes_ago=3)
    pid = int(base.split("/")[-2])
    async with app.state.sessionmaker() as db:
        cid = (await db.get(AgentChange, change["id"])).conversation_id
    lock = agent_service.conversation_lock(cid)
    waiting = asyncio.Event()

    class ObservedLock:
        async def __aenter__(self):
            waiting.set()
            return await lock.__aenter__()

        async def __aexit__(self, *args):
            return await lock.__aexit__(*args)

    await lock.acquire()
    monkeypatch.setattr(agent_service, "conversation_lock", lambda conversation_id: ObservedLock())
    confirmation = asyncio.create_task(client.post(f"{base}/changes/{change['id']}/confirm"))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
        async with app.state.sessionmaker() as db:
            before = (await db.get(AgentChange, change["id"])).status
    finally:
        lock.release()
    assert (await confirmation).status_code == 404
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change["id"])).status == before == "executing"
    assert connector.calls == []
