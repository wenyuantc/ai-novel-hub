"""E4 真实 PostgreSQL：撤权与排队协作任务领取的两种提交顺序，最终都按原轮次失败关闭并释放占用。"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import select, text

from app.models import Chapter, ProjectCollaborator, Task
from app.services import collaboration_service
from app.services.task_engine import engine as engine_module
from tests.helpers import wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_task_revocation_e4 import _queue_chapter, _world
from tests.test_collaboration_task_revocation_e4 import chapter_unready as _chapter_unready

# E4-f 起章节 handler 已就绪；本文件验证领取锁内复核，测试内临时撤销就绪声明以保持「有效任务只复核不执行」。
chapter_unready = _chapter_unready

postgres_e4_apps = _postgres_fixture
MESSAGE = engine_module.COLLABORATION_FAILURE_MESSAGES["collaboration_revoked"]


def _clients(owner_app, guest_app):
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver"),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver"),
    )


async def _wait_failed(maker, task_id, within=20.0):
    async with asyncio.timeout(within):
        while True:
            async with maker() as db:
                row = await db.get(Task, task_id)
                if row.status != "queued":
                    return row
            await asyncio.sleep(0.05)


async def _assert_released(maker, task_id, chapter_id, prev_status):
    row = await _wait_failed(maker, task_id)
    assert row.status == "failed" and row.error == MESSAGE and row.execution_key is None
    assert "_chapter_claim" not in row.input
    async with maker() as db:
        chapter = await db.get(Chapter, chapter_id)
        assert (chapter.status, chapter.generation_claim_key, chapter.generation_claim_task_key) == (
            prev_status, None, None,
        )


async def _cancel(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_postgres_revoke_commits_before_waiting_claim(postgres_e4_apps, monkeypatch, chapter_unready):
    owner_app, guest_app = postgres_e4_apps
    owner, guest = _clients(owner_app, guest_app)
    async with owner, guest:
        project, chapters, actor = await _world(owner, guest)
        maker = owner_app.state.sessionmaker
        task_id, _, claim = await _queue_chapter(owner_app, project["id"], chapters[0], actor["id"])
        async with maker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == actor["id"],
            ))
        reached, release, holder = asyncio.Event(), asyncio.Event(), {}
        original = collaboration_service.add_success

        async def gated(db, snapshot):
            event = await original(db, snapshot)
            if snapshot.action == "revoke":
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return event

        monkeypatch.setattr(collaboration_service, "add_success", gated)
        claims = []
        original_failure = engine_module.TaskEngine._collaboration_claim_failure

        async def observed(db, task):
            result = await original_failure(db, task)
            claims.append(result)
            return result

        monkeypatch.setattr(engine_module.TaskEngine, "_collaboration_claim_failure", staticmethod(observed))
        # 另一实例先运行调度：有效但 handler 未就绪的任务反复领取复核，不会执行。
        await guest_app.state.task_engine.start()
        await wait_until(lambda: claims, within=10)
        revoke = asyncio.create_task(owner.delete(
            f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}",
            params={"expected_revision": member.revision},
        ))
        try:
            await asyncio.wait_for(reached.wait(), 10)
            # 撤权事务持锁未提交：领取必须等待，而不能按旧授权在锁外继续。
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            async with maker() as db:
                assert (await db.get(Task, task_id)).status == "queued"
            release.set()
            assert (await asyncio.wait_for(revoke, 10)).status_code == 204
            await _assert_released(maker, task_id, chapters[0]["id"], claim.prev_status)
            assert claims[-1] == "collaboration_revoked" and set(claims[:-1]) <= {None}
        finally:
            release.set()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()


async def test_postgres_claim_snapshot_before_waiting_revoke(postgres_e4_apps, monkeypatch, chapter_unready):
    owner_app, guest_app = postgres_e4_apps
    owner, guest = _clients(owner_app, guest_app)
    async with owner, guest:
        project, chapters, actor = await _world(owner, guest)
        maker = owner_app.state.sessionmaker
        task_id, _, claim = await _queue_chapter(owner_app, project["id"], chapters[0], actor["id"])
        async with maker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == actor["id"],
            ))
        reached, release, holder, results = asyncio.Event(), asyncio.Event(), {}, []
        original = engine_module.TaskEngine._collaboration_claim_failure

        async def gated(db, task):
            result = await original(db, task)
            results.append(result)
            if len(results) == 1:
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return result

        monkeypatch.setattr(engine_module.TaskEngine, "_collaboration_claim_failure", staticmethod(gated))
        await guest_app.state.task_engine.start()
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            # 领取已在锁内读到仍有效的授权：撤权须等待该事务结束，旧授权不会被用于执行。
            revoke = asyncio.create_task(owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}",
                params={"expected_revision": member.revision},
            ))
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            release.set()
            assert (await asyncio.wait_for(revoke, 10)).status_code == 204
            assert results[0] is None
            await _assert_released(maker, task_id, chapters[0]["id"], claim.prev_status)
            assert "collaboration_revoked" in results[1:]
        finally:
            release.set()
            await _cancel(revoke)
            await guest_app.state.task_engine.stop()
