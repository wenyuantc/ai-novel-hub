"""E4-g2 PostgreSQL：协作者批量的逐章保存与撤权两种提交顺序，以及部分完成后的收尾（双实例）。

执行引擎只在第二个实例运行，撤权经第一个实例提交，只能依赖数据库锁与锁内重验（无本进程通知）；
“进程内取消”变体在撤权提交后显式触发第二实例的协作复查，复现撤权取消落在某章保存提交之后的窗口。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import Chapter, GenerationLog, Task
from app.services import batch_service, generation_service, version_service
from app.services.task_engine import engine as engine_module
from tests.conftest import _new_client
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_batch_entry_e4g2 import (
    BATCH_NOTE,
    STOPPED,
    _assert_saved,
    _assert_untouched,
    _change,
    _start,
    _terminal,
    _world,
)
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_e4g2_apps = _postgres_fixture


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


def _allow_private(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True
        app.state.settings.auto_analyze_enabled = False


async def test_pg_chapter_save_commits_first_then_revoke_keeps_it(postgres_e4g2_apps, fake_llm, monkeypatch):
    """第一章保存事务先持锁：撤权等待其提交；第一章保留，批量在下一章前的断点屏障失败关闭。"""
    first_app, second_app = postgres_e4g2_apps
    _allow_private(first_app, second_app)
    holder = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    original = version_service.ensure_baseline

    async def hold_inside_save(db, chapter):
        if not holder.done():
            holder.set_result(await db.scalar(select(func.pg_backend_pid())))
            await release.wait()
        return await original(db, chapter)

    monkeypatch.setattr(version_service, "ensure_baseline", hold_inside_save)
    # 保存提交后执行体先等撤权真正提交，再进入下一步；避免与撤权争抢下一屏障的偶然排程。
    revoked = asyncio.Event()
    original_sync = batch_service._sync_completed

    async def after_save(ctx, job, claim):
        await asyncio.wait_for(revoked.wait(), 15)
        return await original_sync(ctx, job, claim)

    monkeypatch.setattr(batch_service, "_sync_completed", after_save)
    revoke = None
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            fake_llm.push("保存先提交的第一章")
            fake_llm.push("不应发送的第二章")
            created = await _start(guest, world)
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            pid = await asyncio.wait_for(holder, 15)
            revoke = asyncio.create_task(_change(first_app, owner, world, "revoked"))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, pid), 15)
            assert not revoke.done()
            release.set()
            await asyncio.wait_for(revoke, 15)
            revoked.set()
            row = await _terminal(first_app, created.json()["id"])
            assert row.status == "failed" and row.error in STOPPED, row.error
            assert len(fake_llm.calls) == 1
            await _assert_saved(first_app, world.chapters[0]["id"], "保存先提交的第一章", row.id, world.guest["id"])
            await _assert_untouched(first_app, world.chapters[1]["id"])
            assert row.checkpoint["current"] is None
    finally:
        release.set()
        revoked.set()
        if revoke is not None and not revoke.done():
            revoke.cancel()
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_pg_revoke_commits_first_then_chapter_save_fails_closed(postgres_e4g2_apps, fake_llm, monkeypatch,
                                                                      change):
    first_app, second_app = postgres_e4g2_apps
    _allow_private(first_app, second_app)
    reached, release = asyncio.Event(), asyncio.Event()
    original = generation_service._save_chapter_result

    async def wait_before_save(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(generation_service, "_save_chapter_result", wait_before_save)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
            fake_llm.push("撤权后不能保存的第一章")
            created = await _start(guest, world)
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            await asyncio.wait_for(reached.wait(), 15)
            await asyncio.wait_for(_change(first_app, owner, world, change), 15)
            release.set()
            row = await _terminal(first_app, created.json()["id"])
            assert row.status == "failed" and row.result is None and row.error in STOPPED, row.error
            assert len(fake_llm.calls) == 1
            for chapter in world.chapters:
                await _assert_untouched(first_app, chapter["id"])
            async with first_app.state.sessionmaker() as db:
                assert await db.scalar(select(GenerationLog.id).where(GenerationLog.task_id == row.id)) is None
                assert await db.scalar(select(func.count(Task.id)).where(
                    Task.resource_key == f"batch:{world.project['id']}", Task.status.in_(("queued", "running")),
                )) == 0
    finally:
        release.set()
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("window", ["after_chapter_commit", "before_done"])
async def test_pg_partial_completion_then_revoke_finalizes(postgres_e4g2_apps, fake_llm, monkeypatch, window):
    """部分/全部章节已提交后撤权：任务不得停在 running，已提交章节保留，活动资源释放。

    after_chapter_commit：撤权提交后第二实例的协作复查取消执行体，取消落在第一章保存提交之后、执行体同步断点之前
    （执行体快照无法证明），由持久断点证明后以失败终态收尾；before_done：全部章节已持久化、完成核验前撤权，
    按持久断点重建结果记为成功。
    """
    first_app, second_app = postgres_e4g2_apps
    _allow_private(first_app, second_app)
    engine = second_app.state.task_engine
    fired, boxes = [], {}

    async def revoke_now():
        await _change(first_app, boxes["owner"], boxes["world"], "revoked")

    if window == "after_chapter_commit":
        original_sync = batch_service._sync_completed

        async def after_commit(ctx, job, claim):
            if not fired:
                fired.append(window)
                await asyncio.shield(asyncio.ensure_future(revoke_now()))
                # 第二实例收到“通知”后按数据库复查并取消本执行体；等待处即被取消（不依赖计时）。
                engine.notify_collaboration_change(job.project_id, job.user_id)
                await asyncio.wait_for(asyncio.Event().wait(), 15)
            return await original_sync(ctx, job, claim)

        monkeypatch.setattr(batch_service, "_sync_completed", after_commit)
    else:
        engine_cls = type(engine)
        original_event = engine_cls._event_task

        async def before_done(self, db, execution, current, event):
            if event == "done" and not fired:
                fired.append(window)
                await asyncio.shield(asyncio.ensure_future(revoke_now()))
            return await original_event(self, db, execution, current, event)

        monkeypatch.setattr(engine_cls, "_event_task", before_done)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            boxes.update(owner=owner, world=world)
            fake_llm.push("已提交的第一章")
            fake_llm.push("已提交的第二章")
            created = await _start(guest, world)
            assert created.status_code == 202, created.text
            await engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert fired == [window]
            assert row.execution_key is None
            await _assert_saved(first_app, world.chapters[0]["id"], "已提交的第一章", row.id, world.guest["id"])
            if window == "after_chapter_commit":
                assert row.status == "failed" and row.error == engine_module.COLLABORATION_FAILURE_MESSAGES[
                    "collaboration_revoked"], row.error
                assert len(fake_llm.calls) == 1
                await _assert_untouched(first_app, world.chapters[1]["id"])
                assert row.checkpoint["phase"] == "analyze" and "_batch_completion" in row.checkpoint
            else:
                assert row.status == "succeeded" and row.error == BATCH_NOTE, row.error
                assert row.result == batch_service.summarize(row.checkpoint, 2, 2)
                await _assert_saved(first_app, world.chapters[1]["id"], "已提交的第二章", row.id, world.guest["id"])
            assert row.checkpoint["current"] is None
            async with first_app.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(Chapter.id)).where(
                    Chapter.project_id == world.project["id"], Chapter.status == "generating",
                )) == 0
            assert not engine.has_cleanup_failure(world.project["id"])
    finally:
        await engine.stop()
