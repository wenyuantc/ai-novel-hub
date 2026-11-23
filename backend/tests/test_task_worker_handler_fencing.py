"""独立业务写事务须核对原执行者；单独验证登记被围栏以及任务归属改变。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.models import Chapter, GenerationLog, Task
from app.services import (
    analysis_service,
    batch_service,
    chapter_claim,
    generation_log_service,
    task_service,
    wizard_service,
)
from app.services.task_engine.execution_identity import from_queued_task
from tests.test_batch_chapter_claim import _batch_world
from tests.test_chapter_claim_core import _world as chapter_world
from tests.test_collaboration_analysis_execution import _world as analysis_world
from tests.worker_helpers import fence_worker


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_project_drain_retries_exact_terminal_claim_after_worker_stops(app, client, make_client, batch):
    info, execution_key = await chapter_world(app, client, make_client, batch=batch)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    owner = await engine.lease.register()
    async with maker.begin() as db:
        task = await db.get(Task, info["task_id"])
        if batch:
            task.worker_key = owner
            task.checkpoint = {**task.checkpoint, "_batch_attempt": str(uuid4())}
            await db.flush()
            await chapter_claim.claim_batch_chapter(db, **info, execution_key=execution_key, worker_key=owner)
        else:
            task.input = {**task.input, "prev_status": "draft"}
            await db.flush()
            queued = await chapter_claim.claim_queued_chapter(db, **info)
            task.status, task.worker_key, task.execution_key = "running", owner, str(uuid4())
            await db.flush()
            await chapter_claim.activate_queued_chapter(db, queued, execution_key=task.execution_key, worker_key=owner)
        snapshot = engine.capture_abort_snapshot(task, recovery=True)
        assert snapshot is not None
        engine.remember_abort_failure(task.project_id, snapshot)
        task.status = "failed"
    await engine.lease.stop()
    assert await engine.lease.register() != owner
    claim = snapshot.claim.claim if batch else snapshot.claim
    async with maker() as db:
        with pytest.raises(ConflictError):
            await chapter_claim.release_chapter_claim(db, claim)
        assert not await chapter_claim.release_chapter_claim(db, claim, terminal_status="cancelled")
    async with maker() as db:
        await task_service.stop_project_tasks(db, engine, info["project_id"])
    async with maker() as db:
        task = await db.get(Task, info["task_id"])
        chapter = await db.get(Chapter, info["chapter_id"])
        assert task.status == "failed" and task.execution_key is None
        assert chapter.status == "draft" and chapter.generation_claim_key is None
        assert not engine.has_cleanup_failure(info["project_id"])


async def _lose_ownership(engine, peer, task_id, transition):
    if transition == "fenced":
        await fence_worker(engine, mirror=False)
    else:
        next_owner = await peer.lease.register()
        async with engine.ctx.sessionmaker.begin() as db:
            (await db.get(Task, task_id)).worker_key = next_owner


@pytest.mark.parametrize("transition", ["adopted", "fenced"])
async def test_old_chapter_cannot_release_or_log(app, second_app, client, make_client, transition):
    info, _ = await chapter_world(app, client, make_client)
    maker = app.state.sessionmaker
    worker_key = await app.state.task_engine.lease.register()
    async with maker() as db:
        queued = await chapter_claim.claim_queued_chapter(db, **info)
        row = await db.get(Task, queued.task_id)
        execution = from_queued_task(row, str(uuid4()), worker_key=worker_key)
        row.status, row.worker_key, row.execution_key = "running", worker_key, execution.execution_key
        await db.flush()
        claim = await chapter_claim.activate_queued_chapter(
            db, queued, execution_key=execution.execution_key, worker_key=worker_key,
        )
        await db.commit()

    log = dict(project_id=claim.project_id, actor_user_id=claim.actor_id, chapter_id=claim.chapter_id,
               task_id=claim.task_id, kind="generate", ai_config_id=None, provider="", model="",
               params={}, messages=[], status="error")
    assert await generation_log_service.save_log(maker, execution=execution, **log) is not None
    await _lose_ownership(app.state.task_engine, second_app.state.task_engine, claim.task_id, transition)
    assert await generation_log_service.save_log(maker, execution=execution, **log) is None

    async with maker() as db:
        if transition == "fenced":
            with pytest.raises(ConflictError):
                await chapter_claim.release_chapter_claim(db, claim)
        else:
            assert not await chapter_claim.release_chapter_claim(db, claim)
    async with maker() as db:
        chapter = await db.get(Chapter, claim.chapter_id)
        assert (chapter.status, chapter.generation_claim_key, chapter.content) == (
            "generating", claim.claim_key, "不应被旧清理修改",
        )
        assert await db.scalar(select(func.count(GenerationLog.id)).where(GenerationLog.task_id == claim.task_id)) == 1


@pytest.mark.parametrize("kind", ["analysis", "wizard"])
@pytest.mark.parametrize("transition", ["adopted", "fenced"])
async def test_old_cleanup_preserves_pending(app, second_app, client, kind, transition):
    _, _, _, _, original = await analysis_world(app, client)
    maker = app.state.sessionmaker
    owner = await app.state.task_engine.lease.register()
    async with maker() as db:
        row = await db.get(Task, original.task_id)
        if kind == "wizard":
            row.kind = "wizard.characters"
            row.input = {**row.input, "_wizard_actor_token_version": original.actor_token_version}
            row.status, row.execution_key = "queued", None
            execution = from_queued_task(row, original.execution_key, worker_key=owner)
            row.status, row.execution_key = "running", original.execution_key
        else:
            identity = replace(original.task_identity, worker_key=owner)
            execution = analysis_service.analysis_execution_from_claim(identity)
        row.worker_key = owner
        await db.commit()

    if kind == "analysis":
        pending = await analysis_service.begin_analysis_pending(maker, execution)
    else:
        pending = await wizard_service.begin_wizard_pending(maker, execution)
    async with maker() as db:
        expected = (await db.get(Task, original.task_id)).checkpoint
        assert expected and f"_{kind}_pending" in expected

    await _lose_ownership(app.state.task_engine, second_app.state.task_engine, original.task_id, transition)
    if kind == "analysis" and transition == "fenced":
        with pytest.raises(analysis_service.AnalysisCommitUncertain):
            await analysis_service.discard_analysis_pending(maker, pending)
    elif kind == "analysis":
        await analysis_service.discard_analysis_pending(maker, pending)
    else:
        assert not await wizard_service.discard_wizard_pending(maker, pending)
    async with maker() as db:
        row = await db.get(Task, original.task_id)
        assert (row.status, row.checkpoint) == ("running", expected)


@pytest.mark.parametrize("transition", ["adopted", "fenced"])
async def test_old_batch_checkpoint_write_cannot_advance(app, second_app, client, make_client, transition):
    info, _, _, job = await _batch_world(app, client, make_client)
    maker = app.state.sessionmaker
    owner = await app.state.task_engine.lease.register()
    async with maker() as db:
        task = await db.get(Task, info["task_id"])
        original = task.checkpoint
        task.worker_key = owner
        await db.commit()
    job.execution_identity = replace(job.execution_identity, worker_key=owner)
    job.task_execution_identity = job.execution_identity
    await _lose_ownership(app.state.task_engine, second_app.state.task_engine, job.task_id, transition)

    job.checkpoint["items"][0]["error"] = "迟到的旧断点"
    async with maker() as db:
        with pytest.raises(ConflictError):
            await batch_service._write_checkpoint(db, job)
    async with maker() as db:
        assert (await db.get(Task, job.task_id)).checkpoint == original
