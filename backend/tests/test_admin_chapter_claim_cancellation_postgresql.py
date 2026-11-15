"""管理员章节取消在 PostgreSQL 上的真实两连接事务边界。"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import update

from app.models import Chapter, Project, Task, User
from app.services import chapter_claim
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps  # noqa: F401


async def test_postgresql_admin_chapter_cancel_is_atomic(postgres_collaboration_apps, monkeypatch):  # noqa: F811
    app, _ = postgres_collaboration_apps
    engine, factory = app.state.task_engine, app.state.sessionmaker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        actor = await register(client, "pg-chapter-cancel")
        async with factory() as db:
            (await db.get(User, actor["id"])).is_admin = True
            await db.commit()
        await create_ai_config(client)
        project = await create_project(client)
        chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})).json()
        created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
        assert created.status_code == 202, created.text
        task_id = created.json()["id"]
        async with factory() as db:
            task = await db.get(Task, task_id)
            original = await db.get(Chapter, chapter["id"])
            key, original_claim = task.statistics_key, original.generation_claim_key
            assert task.status == "queued" and original_claim
        entered, release, other_acquired = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_release = chapter_claim.release_chapter_claim

        async def hold_release(db, claim):
            changed = await real_release(db, claim)
            assert changed
            entered.set()
            await release.wait()
            return changed

        async def other_connection():
            async with factory() as db:
                await db.execute(update(Project).where(Project.id == project["id"]).values(id=Project.id))
                other_acquired.set()
                await db.rollback()

        monkeypatch.setattr(chapter_claim, "release_chapter_claim", hold_release)
        other = None
        try:
            accepted = await client.post(f"/api/v1/admin/tasks/{key}/cancel", json={
                "confirm": True, "reason": "验证章节原子取消",
            })
            assert accepted.status_code == 202, accepted.text
            await asyncio.wait_for(entered.wait(), 5)
            async with factory() as db:
                task, current = await db.get(Task, task_id), await db.get(Chapter, chapter["id"])
                assert task.status == "queued" and task.admin_cancel_operation_id == accepted.json()["operation_id"]
                assert current.status == "generating" and current.generation_claim_key == original_claim
            other = asyncio.create_task(other_connection())
            await asyncio.sleep(0.1)
            assert not other_acquired.is_set()
            release.set()
            await asyncio.wait_for(other, 5)
            await asyncio.wait_for(engine.wait_admin_cancellations(), 5)
            async with factory() as db:
                task, current = await db.get(Task, task_id), await db.get(Chapter, chapter["id"])
                assert task.status == "cancelled" and task.execution_key is None
                assert task.admin_cancel_operation_id is None and "_admin_chapter_receipt" not in task.input
                assert current.status == "draft" and current.generation_claim_key is None
        finally:
            release.set()
            if other is not None and not other.done():
                other.cancel()
                with suppress(asyncio.CancelledError):
                    await other
            await engine.wait_admin_cancellations()
