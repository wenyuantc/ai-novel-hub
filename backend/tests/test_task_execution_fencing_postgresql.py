"""PostgreSQL 两连接验证领取、文档占用和作品锁的事务边界。"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import select, update

from app.models import KbDocument, Project, Task
from app.services import kb_index_service
from tests.helpers import create_project, register
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps  # noqa: F401


async def test_postgresql_claim_and_document_rotate_atomically(postgres_collaboration_apps, monkeypatch):  # noqa: F811
    app, _ = postgres_collaboration_apps
    engine, factory = app.state.task_engine, app.state.sessionmaker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client, "pg-fenced-owner")
        project = await create_project(client)
        response = await client.post(
            f"/api/v1/projects/{project['id']}/kb/documents",
            files={"file": ("owner.txt", "正文".encode(), "text/plain")},
        )
        assert response.status_code == 202, response.text
        payload = response.json()

    task_id, document_id = payload["task"]["id"], payload["document"]["id"]
    async with factory() as db:
        task = await db.get(Task, task_id)
        doc = await db.get(KbDocument, document_id)
        key, queued_at, queue_claim = task.statistics_key, task.queued_at, doc.index_claim_key
        assert task.status == "queued" and queue_claim and task.execution_key is None
    engine._running_rounds[task_id] = queued_at
    entered, release, other_acquired = asyncio.Event(), asyncio.Event(), asyncio.Event()
    activate = kb_index_service.activate_document_claim

    async def hold_activation(db, claim, *, execution_key):
        running = await activate(db, claim, execution_key=execution_key)
        entered.set()
        await release.wait()
        return running

    async def stop_after_claim(*_args):
        pass

    async def other_connection():
        async with factory() as db:
            await db.execute(update(Project).where(Project.id == project["id"]).values(id=Project.id))
            other_acquired.set()
            await db.rollback()

    monkeypatch.setattr(kb_index_service, "activate_document_claim", hold_activation)
    monkeypatch.setattr(engine, "_run", stop_after_claim)
    claiming = asyncio.create_task(engine._claim_and_run(task_id, key))
    other = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as db:
            visible = (await db.execute(select(Task.status, Task.execution_key, KbDocument.index_claim_key)
                                        .join(KbDocument, KbDocument.id == document_id)
                                        .where(Task.id == task_id))).one()
            assert tuple(visible) == ("queued", None, queue_claim)
        other = asyncio.create_task(other_connection())
        await asyncio.sleep(0.1)
        assert not other_acquired.is_set()
        release.set()
        await asyncio.wait_for(claiming, 5)
        await asyncio.wait_for(other, 5)
        async with factory() as db:
            task = await db.get(Task, task_id)
            doc = await db.get(KbDocument, document_id)
            assert task.status == "running" and task.execution_key
            assert doc.index_task_key == key and doc.index_task_queued_at == queued_at
            assert doc.index_claim_key == task.execution_key and doc.index_claim_key != queue_claim
    finally:
        release.set()
        for pending in (claiming, other):
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
