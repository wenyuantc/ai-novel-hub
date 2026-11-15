"""E4-g1 PostgreSQL：协作者章节分析的写回与撤权两种提交顺序（双实例，无本进程通知）。

执行引擎只在第二个实例运行，撤权经第一个实例提交，只能依赖数据库锁与写回事务内的锁后重验。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import AnalysisChange, ChapterAnalysis, StoryMemory, Task
from app.services import analysis_service, writeback_service
from app.services.task_engine import engine as engine_module
from tests.conftest import _new_client
from tests.helpers import analysis_json, wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_chapter_entry_e4f import _change, _terminal, _world
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

ANALYSIS_NOTE = engine_module.ANALYSIS_RECEIPT_FINALIZED_NOTE

postgres_e4g_apps = _postgres_fixture


def _allow_private(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True


async def _counts(app):
    async with app.state.sessionmaker() as db:
        return [await db.scalar(select(func.count()).select_from(model))
                for model in (ChapterAnalysis, AnalysisChange, StoryMemory)]


async def test_pg_writeback_commits_first_then_revoke_keeps_result(postgres_e4g_apps, fake_llm, monkeypatch):
    first_app, second_app = postgres_e4g_apps
    _allow_private(first_app, second_app)
    holder = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    original = writeback_service.apply_analysis

    async def hold_inside_writeback(db, analysis, data):
        # 此时写回事务已持认证/User、发送写门禁、作品与原 Task 锁。
        if not holder.done():
            holder.set_result(await db.scalar(select(func.pg_backend_pid())))
            await release.wait()
        return await original(db, analysis, data)

    monkeypatch.setattr(writeback_service, "apply_analysis", hold_inside_writeback)
    revoke = None
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            fake_llm.push(analysis_json(summary="写回先提交的分析"))
            created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            pid = await asyncio.wait_for(holder, 15)
            # 撤权必须等待写回事务提交。
            revoke = asyncio.create_task(_change(first_app, owner, world, "revoked"))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, pid), 15)
            assert not revoke.done()
            release.set()
            await asyncio.wait_for(revoke, 15)
            task_id = created.json()["id"]
            engine = second_app.state.task_engine
            await wait_until(lambda: task_id not in engine._running, within=15)
            row = await _terminal(first_app, task_id, within=15)
            # 写回先提交：分析、回写与记忆保留，不因随后撤权回滚。done 先核验 → 普通成功；
            # 撤权先于 done 核验提交 → 凭完成收据受限收尾。两者都必须释放执行屏障与串行键。
            assert row.status == "succeeded" and row.execution_key is None, row.error
            assert row.error in ("", None, ANALYSIS_NOTE), row.error
            assert row.user_id == world.guest["id"]
            async with first_app.state.sessionmaker() as db:
                analysis = await db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.task_id == task_id))
                assert analysis is not None and analysis.summary == "写回先提交的分析"
                assert await db.scalar(select(func.count(StoryMemory.id)).where(
                    StoryMemory.chapter_id == world.chapter["id"])) >= 1
            assert not engine.has_cleanup_failure(world.project["id"])
            again = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
            assert again.status_code == 404, again.text
    finally:
        release.set()
        if revoke is not None and not revoke.done():
            revoke.cancel()
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_pg_revoke_commits_first_then_writeback_fails_closed(postgres_e4g_apps, fake_llm, monkeypatch, change):
    first_app, second_app = postgres_e4g_apps
    _allow_private(first_app, second_app)
    reached, release = asyncio.Event(), asyncio.Event()
    original = analysis_service._analysis_write_barrier

    async def wait_before_writeback(db, execution, *, status="running"):
        # begin_analysis_pending 也会调用（发送前）；模型调用之后的第一次才是写回事务。
        if status == "running" and fake_llm.calls and not reached.is_set():
            reached.set()
            await release.wait()
        return await original(db, execution, status=status)

    monkeypatch.setattr(analysis_service, "_analysis_write_barrier", wait_before_writeback)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            owner_ai = change == "owner_ai"
            world = await _world(owner, guest, allow_owner_ai=True, editor_model=not owner_ai)
            fake_llm.push(analysis_json(summary="撤权后不能写回的分析"))
            created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            await asyncio.wait_for(reached.wait(), 15)
            # 模型输出已全部产出、写回尚未取锁：授权变化在另一实例完整提交。
            await asyncio.wait_for(_change(first_app, owner, world, change), 15)
            release.set()
            row = await _terminal(first_app, created.json()["id"], within=15)
            assert row.status == "failed" and row.result is None, row.error
            assert row.error == "分析任务执行身份或授权已变化，已停止", row.error
            assert not (row.checkpoint or {}).get("_analysis_pending")
            assert not (row.checkpoint or {}).get("_analysis_completion")
            assert len(fake_llm.calls) == 1
            assert await _counts(first_app) == [0, 0, 0]
    finally:
        release.set()
        await second_app.state.task_engine.stop()


async def test_pg_change_between_writeback_commit_and_done_completes_from_receipt(
    postgres_e4g_apps, fake_llm, monkeypatch,
):
    """确定性覆盖：写回已提交、done 核验取锁前撤权在另一实例完整提交 → 凭完成收据受限收尾。"""
    first_app, second_app = postgres_e4g_apps
    _allow_private(first_app, second_app)
    engine_cls = type(second_app.state.task_engine)
    original = engine_cls._event_task
    fired, boxes = [], []

    async def wrapped(self, db, execution, current, event):
        if event == "done" and not fired:
            fired.append(event)
            owner, world = boxes
            work = asyncio.ensure_future(_change(first_app, owner, world, "revoked"))
            await asyncio.shield(work)
        return await original(self, db, execution, current, event)

    monkeypatch.setattr(engine_cls, "_event_task", wrapped)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            boxes.extend([owner, world])
            fake_llm.push(analysis_json(summary="提交后才撤权的分析"))
            created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
            assert created.status_code == 202, created.text
            task_id = created.json()["id"]
            await second_app.state.task_engine.start()
            row = await _terminal(first_app, task_id, within=15)
            assert fired == ["done"]
            assert row.status == "succeeded" and row.error == ANALYSIS_NOTE and row.execution_key is None
            async with first_app.state.sessionmaker() as db:
                assert await db.scalar(select(ChapterAnalysis.id).where(ChapterAnalysis.task_id == task_id))
                assert await analysis_service.completed_terminal_analysis_receipt(
                    db, await db.get(Task, task_id)) is not None
            assert len(fake_llm.calls) == 1
    finally:
        await second_app.state.task_engine.stop()
