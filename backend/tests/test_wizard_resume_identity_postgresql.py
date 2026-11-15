"""向导续写来源身份在 PostgreSQL 上按行锁后的真实数据复核。"""

import asyncio

import httpx
from sqlalchemy import delete, select, text

from app.models import Task
from app.services import wizard_service
from app.services.task_engine.handlers.wizard import WizardHandler
from app.services.wizard_service import WizardResumeChanged
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_wizard_resume_identity import _running, _source

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_recreated_source_before_lock_is_not_adopted(postgres_collaboration_apps, fake_llm):
    owner_app, other_app = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner:
        user, project, source = await _source(owner_app, owner)
        work = None
        try:
            async with other_app.state.sessionmaker() as holder:
                await holder.execute(select(Task.id).where(Task.id == source["id"]).with_for_update())
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                # 先在同一事务完成替换和外键检查，再并发受理；避免测试自己反向取 User/Project 锁。
                await holder.execute(delete(Task).where(Task.id == source["id"]))
                holder.add(Task(
                    id=source["id"], user_id=user["id"], project_id=project["id"], kind="wizard.outline",
                    status="failed", input={"request": {"project_id": project["id"]}, "ai_history_epoch": 0},
                    output_text='{"volumes":[{"title":"替代结果"}',
                ))
                await holder.flush()
                work = asyncio.create_task(owner.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
                    "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
                }))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 10)
                assert not work.done()
                await holder.commit()
            result = await asyncio.wait_for(work, 10)
            assert result.status_code == 400, result.text
            async with owner_app.state.sessionmaker() as db:
                assert len(list(await db.scalars(select(Task)))) == 1
            assert not fake_llm.calls
        finally:
            if work is not None and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)


async def test_acceptance_lock_precedes_delete_but_job_rechecks_source(
    postgres_collaboration_apps, fake_llm, monkeypatch,
):
    owner_app, other_app = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner:
        _, project, source = await _source(owner_app, owner)
        ready, release = asyncio.Event(), asyncio.Event()
        holder_pid = None
        original = wizard_service.load_resume_source

        async def block_after_lock(db, **kwargs):
            nonlocal holder_pid
            row = await original(db, **kwargs)
            if kwargs.get("for_update"):
                holder_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                ready.set()
                await release.wait()
            return row

        monkeypatch.setattr(wizard_service, "load_resume_source", block_after_lock)
        work = asyncio.create_task(owner.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
            "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
        }))
        deleting = None
        try:
            await asyncio.wait_for(ready.wait(), 10)
            assert holder_pid is not None

            async def remove_source():
                async with other_app.state.sessionmaker() as db:
                    await db.execute(delete(Task).where(Task.id == source["id"]))
                    await db.commit()

            deleting = asyncio.create_task(remove_source())
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder_pid), 10)
            assert not deleting.done()
            release.set()
            accepted = await asyncio.wait_for(work, 10)
            assert accepted.status_code == 202, accepted.text
            await asyncio.wait_for(deleting, 10)
            task, execution = await _running(owner_app, accepted.json()["id"])
            try:
                await WizardHandler().build_job(owner_app.state.task_engine.ctx, task, execution=execution)
            except WizardResumeChanged:
                pass
            else:
                raise AssertionError("旧来源已删除，不应构造续写")
            assert not fake_llm.calls
        finally:
            release.set()
            for task in (work, deleting):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (work, deleting) if task is not None), return_exceptions=True)
