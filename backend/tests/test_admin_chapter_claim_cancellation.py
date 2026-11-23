"""管理员取消必须绑定受理时的章节占用与实际执行体。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Project, Task, TaskStatistic, User
from app.services import chapter_claim, project_access, task_service, task_statistics_service
from app.services.task_engine.handlers import chapter as chapter_handler
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, create_ai_config, create_project, register


async def _chapter_task(app, client):
    engine = app.state.task_engine
    await engine.stop()
    actor = await register(client, "chapter-admin")
    async with app.state.sessionmaker() as db:
        (await db.get(User, actor["id"])).is_admin = True
        await db.commit()
    await create_ai_config(client)
    project = await create_project(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "原章节"})).json()
    response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
    assert response.status_code == 202, response.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, response.json()["id"])
        row = await db.get(Chapter, chapter["id"])
        assert task.status == "queued" and row.status == "generating"
        assert chapter_handler.capture_queued_claim(task).claim_key == row.generation_claim_key
        return task.id, task.statistics_key, chapter["id"]


async def _accept(client, task_key):
    response = await client.post(f"/api/v1/admin/tasks/{task_key}/cancel", json={
        "confirm": True, "reason": "检查章节取消",
    })
    assert response.status_code == 202, response.text
    return response.json()["operation_id"]


async def _make_running(app, task_id):
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        await project_access.require_project(db, task.user_id, task.project_id, "ai.use", for_update=True,
                                             visibility_write=True)
        queued = chapter_handler.capture_queued_claim(task)
        await db.execute(update(Task).where(Task.id == task_id).values(status="running", execution_key=str(uuid4())))
        await db.refresh(task)
        running = await chapter_claim.activate_queued_chapter(db, queued, execution_key=task.execution_key)
        await task_statistics_service.record_state_change(db, task, "running")
        await db.commit()
        return running


async def test_peer_start_leaves_admin_intent_and_chapter_for_live_worker(
    app, second_app, client, fake_llm, monkeypatch,
):
    task_id, key, chapter_id = await _chapter_task(app, client)
    owner, peer = app.state.task_engine, second_app.state.task_engine
    stream = Gate()
    fake_llm.push(["首段", stream, "尾段"])
    try:
        with monkeypatch.context() as hold:
            # 取消意图已经提交，但原执行者尚未消费；此刻另一进程启动并完成自己的扫描。
            hold.setattr(owner.admin_cancellations, "request", lambda *_: None)
            await owner.start()
            await stream.wait_reached()
            operation = await _accept(client, key)
            await peer.start()
            await peer.wait_admin_cancellations()
            async with second_app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
                chapter = await db.get(Chapter, chapter_id)
                assert (row.status, row.worker_key, row.admin_cancel_operation_id) == (
                    "running", owner.lease.worker_key, operation,
                )
                assert chapter.status == "generating" and chapter.generation_claim_task_key == key
            assert not owner._running[task_id].done()
        await owner.scan_admin_cancellations()
        await owner.wait_admin_cancellations()
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task_id)
            chapter = await db.get(Chapter, chapter_id)
            assert (row.status, row.admin_cancel_operation_id, row.execution_key) == ("cancelled", None, None)
            assert chapter.status == "draft" and chapter.generation_claim_key is None
    finally:
        stream.release()


async def test_admin_queued_chapter_finishes_and_releases_same_claim(app, client):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "cancelled" and task.admin_cancel_operation_id is None
        assert task.execution_key is None and "_chapter_claim" not in task.input
        assert chapter.status == "draft" and chapter.generation_claim_key is None
        assert (await db.get(TaskStatistic, task_key)).status == "cancelled"


async def test_admin_chapter_release_failure_rolls_back_task_and_claim(app, client, monkeypatch):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    release = chapter_claim.release_chapter_claim
    attempts = []

    async def fail_after_release(db, claim):
        attempts.append(claim.claim_key)
        assert await release(db, claim)
        raise RuntimeError("模拟章节清理提交前失败")

    monkeypatch.setattr(chapter_claim, "release_chapter_claim", fail_after_release)
    operation = await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert attempts and task.status == "queued" and task.admin_cancel_operation_id == operation
        assert chapter.status == "generating" and chapter.generation_claim_key == attempts[0]
        assert (await db.get(TaskStatistic, task_key)).status == "queued"
    monkeypatch.setattr(chapter_claim, "release_chapter_claim", release)
    await app.state.task_engine.scan_admin_cancellations()
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "cancelled" and task.admin_cancel_operation_id is None
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_admin_orphan_running_uses_original_execution_and_claim(app, client):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    running = await _make_running(app, task_id)
    assert running.execution_key
    await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "cancelled" and task.execution_key is None
        assert task.admin_cancel_operation_id is None and "_chapter_claim" not in task.input
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_admin_old_intent_cannot_release_new_chapter_claim(app, client, monkeypatch):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    engine = app.state.task_engine
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *_args: None)
    operation = await _accept(client, task_key)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    new_claim = str(uuid4())
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        task.input = {**task.input, "_chapter_claim": {**task.input["_chapter_claim"], "claim_key": new_claim}}
        chapter.generation_claim_key = new_claim
        await db.commit()
    engine.request_admin_cancel(task_key, operation)
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "queued" and task.admin_cancel_operation_id == operation
        assert chapter.status == "generating" and chapter.generation_claim_key == new_claim


async def test_admin_chapter_commit_ack_loss_keeps_original_receipt(app, client, monkeypatch):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    original = AsyncSession.commit
    lost = False

    async def commit_then_lose(db):
        nonlocal lost
        is_completion = not lost and await db.scalar(select(Task.status).where(Task.id == task_id)) == "cancelled"
        await original(db)
        if is_completion:
            lost = True
            raise RuntimeError("模拟提交确认丢失")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose)
    await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert lost and task.status == "cancelled"
        assert task.admin_cancel_operation_id is None and task.execution_key is None
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_admin_receipt_clear_ack_loss_does_not_repeat_chapter_release(app, client, monkeypatch):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    original_commit = AsyncSession.commit
    original_release = chapter_claim.release_chapter_claim
    lost, releases = False, []

    async def count_release(db, claim):
        releases.append(claim.claim_key)
        return await original_release(db, claim)

    async def commit_then_lose(db):
        nonlocal lost
        state = (await db.execute(select(Task.status, Task.execution_key, Task.admin_cancel_operation_id).where(
            Task.id == task_id,
        ))).one_or_none()
        clearing = not lost and state is not None and tuple(state) == ("cancelled", None, None)
        await original_commit(db)
        if clearing:
            lost = True
            raise RuntimeError("模拟收据清理确认丢失")

    monkeypatch.setattr(chapter_claim, "release_chapter_claim", count_release)
    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose)
    await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert lost and len(releases) == 1
        assert task.status == "cancelled" and task.admin_cancel_operation_id is None and task.execution_key is None
        assert "_admin_chapter_receipt" not in task.input
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_admin_lost_ack_never_releases_new_chapter_claim(app, client, monkeypatch):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    factory = app.state.sessionmaker
    original = AsyncSession.commit
    lost, new_task_id = False, None

    async def commit_then_replace(db):
        nonlocal lost, new_task_id
        state = (await db.execute(select(Task.status, Task.input, Task.user_id, Task.project_id).where(
            Task.id == task_id,
        ))).one_or_none()
        completed = (not lost and state is not None and state.status == "cancelled"
                     and "_admin_chapter_receipt" in state.input)
        await original(db)
        if not completed:
            return
        lost = True
        async with factory() as newer:
            await project_access.require_project(newer, state.user_id, state.project_id, "ai.use", for_update=True,
                                                 visibility_write=True)
            replacement = Task(
                user_id=state.user_id, project_id=state.project_id, chapter_id=chapter_id,
                kind="chapter.generate", status="queued", resource_key=f"chapter:{chapter_id}",
                input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id, "prev_status": "draft"},
            )
            newer.add(replacement)
            await task_statistics_service.record_created(newer, replacement)
            await chapter_handler.bind_queued_claim(newer, replacement)
            await original(newer)
            new_task_id = replacement.id
        raise RuntimeError("原取消已提交但确认丢失")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_replace)
    await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with factory() as db:
        old, new = await db.get(Task, task_id), await db.get(Task, new_task_id)
        chapter = await db.get(Chapter, chapter_id)
        assert lost and old.status == "cancelled" and old.admin_cancel_operation_id is None
        assert old.execution_key is None and new.status == "queued"
        assert chapter.status == "generating"
        assert chapter.generation_claim_key == new.input["_chapter_claim"]["claim_key"]


async def test_admin_receipt_recovery_clears_only_original_marker(app, client, monkeypatch):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    drain = app.state.task_engine.admin_cancellations
    actual = drain._clear_chapter_receipt

    async def pause_clear(*_args):
        return None

    monkeypatch.setattr(drain, "_clear_chapter_receipt", pause_clear)
    operation = await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "cancelled" and task.admin_cancel_operation_id == operation and task.execution_key
        assert task.input["_admin_chapter_receipt"]["operation_id"] == operation
        assert chapter.status == "draft" and chapter.generation_claim_key is None
    monkeypatch.setattr(drain, "_clear_chapter_receipt", actual)
    await app.state.task_engine.scan_admin_cancellations()
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        assert task.status == "cancelled" and task.execution_key is None and task.admin_cancel_operation_id is None
        assert "_admin_chapter_receipt" not in task.input


async def test_admin_unproven_legacy_claim_is_not_guessed(app, client):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        task.input = {key: value for key, value in task.input.items() if key != "_chapter_claim"}
        chapter.generation_claim_key = chapter.generation_claim_task_key = chapter.generation_claim_prev_status = None
        await db.commit()
    operation = await _accept(client, task_key)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "queued" and task.admin_cancel_operation_id == operation
        assert chapter.status == "generating" and chapter.generation_claim_key is None


async def test_admin_old_running_intent_cannot_release_new_execution(app, client, monkeypatch):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    await _make_running(app, task_id)
    engine = app.state.task_engine
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *_args: None)
    operation = await _accept(client, task_key)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    new_claim, new_execution = str(uuid4()), str(uuid4())
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        task.execution_key = new_execution
        task.input = {**task.input, "_chapter_claim": {**task.input["_chapter_claim"], "claim_key": new_claim}}
        chapter.generation_claim_key = new_claim
        await db.commit()
    engine.request_admin_cancel(task_key, operation)
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "running" and task.admin_cancel_operation_id == operation
        assert task.execution_key == new_execution and chapter.generation_claim_key == new_claim


async def test_admin_waits_for_original_running_executor_to_stop(app, client):
    task_id, task_key, chapter_id = await _chapter_task(app, client)
    running = await _make_running(app, task_id)
    engine = app.state.task_engine
    engine.cancel_wait_seconds = .01
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def executor():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    work = asyncio.create_task(executor())
    engine._running[task_id] = work
    engine._running_statistics_keys[task_id] = task_key
    engine._running_rounds[task_id] = running.queued_at.replace(tzinfo=None)
    engine._running_execution_keys[task_id] = running.execution_key
    try:
        await asyncio.wait_for(started.wait(), 3)
        operation = await _accept(client, task_key)
        await asyncio.wait_for(cancelled.wait(), 3)
        await engine.wait_admin_cancellations()
        async with app.state.sessionmaker() as db:
            task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
            assert task.status == "running" and task.admin_cancel_operation_id == operation
            assert chapter.generation_claim_key == running.claim_key
        release.set()
        await work
        # 即使内存映射暂时没有活 coroutine，领取、完成或准入登记尚存时也不能猜它已退出。
        for registry in (engine._claiming, engine._finalizing):
            registry.add(task_id)
            await engine.scan_admin_cancellations()
            await engine.wait_admin_cancellations()
            async with app.state.sessionmaker() as db:
                assert (await db.get(Task, task_id)).admin_cancel_operation_id == operation
            registry.discard(task_id)
        engine._admitted[task_id] = (task_key, running.queued_at, running.actor_id)
        await engine.scan_admin_cancellations()
        await engine.wait_admin_cancellations()
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, task_id)).admin_cancel_operation_id == operation
        engine._admitted.pop(task_id)
        await engine.scan_admin_cancellations()
        await engine.wait_admin_cancellations()
        async with app.state.sessionmaker() as db:
            task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
            assert task.status == "cancelled" and task.admin_cancel_operation_id is None
            assert chapter.status == "draft" and chapter.generation_claim_key is None
    finally:
        release.set()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        engine._running.pop(task_id, None)
        engine._running_statistics_keys.pop(task_id, None)
        engine._running_rounds.pop(task_id, None)
        engine._running_execution_keys.pop(task_id, None)
        engine._claiming.discard(task_id)
        engine._finalizing.discard(task_id)
        engine._admitted.pop(task_id, None)


async def test_unproven_legacy_analysis_does_not_retry_arbitrary_chapter_abort(app, client, monkeypatch):
    """design §5：失败关闭不得遗留永久占用；删除作品按原快照只重试同一任务 UUID/轮次的 abort。"""
    engine = app.state.task_engine
    await engine.stop()
    user = await register(client, "legacy-analysis-owner")
    async with app.state.sessionmaker() as db:
        project = Project(user_id=user["id"], title="旧无证轮次")
        db.add(project)
        await db.flush()
        chapter = Chapter(project_id=project.id, chapter_no=1, status="draft", content="不能被旧清理触碰")
        task = Task(user_id=user["id"], project_id=project.id, kind="chapter.analyze", status="queued",
                    resource_key="old-analysis-without-chapter")
        other = Task(user_id=user["id"], project_id=project.id, kind="chapter.analyze", status="cancelled")
        db.add_all([chapter, task, other])
        await task_statistics_service.record_created(db, task)
        await task_statistics_service.record_created(db, other)
        await db.commit()
        task_id, task_key, queued_at = task.id, task.statistics_key, task.queued_at
        project_id, chapter_id, other_id = project.id, chapter.id, other.id
        chapter_before = (chapter.status, chapter.content, chapter.generation_claim_key,
                          chapter.generation_claim_task_key)
    handler = get_handler("chapter.analyze")
    real_abort = handler.on_abort
    calls = []

    async def flaky_abort(ctx, row, **kwargs):
        # 只记录被重试的身份；真实分析 abort 不触碰章节，旧版“任意章节回退”不得出现。
        calls.append((row.id, row.statistics_key, row.queued_at))
        if len(calls) == 1:
            raise RuntimeError("旧回退失败")
        await real_abort(ctx, row, **kwargs)

    monkeypatch.setattr(handler, "on_abort", flaky_abort)
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError):
            await task_service.cancel_task(db, engine, user["id"], task_id)
    async with app.state.sessionmaker() as db:
        pending = await db.get(Task, task_id)
        assert pending.status == "queued" and pending.execution_key is not None
    assert engine.has_cleanup_failure(project_id)

    deleted = await client.delete(f"/api/v1/projects/{project_id}")
    assert deleted.status_code == 204, deleted.text
    # 重试只作用于原任务的同一 UUID/轮次，不触及其他任务。
    assert len(calls) == 2 and {call[:2] for call in calls} == {(task_id, task_key)}
    assert all(chapter_claim._utc(call[2]) == chapter_claim._utc(queued_at) for call in calls)
    assert other_id not in {call[0] for call in calls}
    assert not engine.has_cleanup_failure(project_id)
    async with app.state.sessionmaker() as db:
        row, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert row.statistics_key == task_key and row.status == "cancelled" and row.execution_key is None
        assert (chapter.status, chapter.content, chapter.generation_claim_key,
                chapter.generation_claim_task_key) == chapter_before
        assert (await db.get(Project, project_id)).deleted_at is not None
