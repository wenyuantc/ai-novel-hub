"""管理员接管仅能基于原执行事实，不重放无法证明的分析和批量收尾。"""

from contextlib import aclosing
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Chapter, Task
from app.services import analysis_service, batch_service, chapter_claim, task_service
from app.services.task_engine.registry import get_handler
from tests.helpers import analysis_json
from tests.test_batch_chapter_claim import _batch_world
from tests.test_collaboration_analysis_execution import _job, _world


async def _mark_cancel(app, task_id):
    operation = str(uuid4())
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task.admin_cancel_operation_id = operation
        await db.commit()
    return operation


@pytest.mark.parametrize("checkpoint", [None, {"_analysis_pending": None}, {"_analysis_completion": {}}])
async def test_admin_orphan_analysis_without_proof_remains_running(app, client, monkeypatch, checkpoint):
    world = await _world(app, client)
    execution = world[-1]
    operation = await _mark_cancel(app, execution.task_id)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.checkpoint = checkpoint
        await db.commit()
    calls = []

    async def abort(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(get_handler("chapter.analyze"), "on_abort", abort)
    engine = app.state.task_engine
    await engine.admin_cancellations._drain(execution.task_key, operation)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, execution.task_id)
        assert (row.status, row.execution_key, row.admin_cancel_operation_id, row.checkpoint) == (
            "running", execution.execution_key, operation, checkpoint,
        )
        assert engine.execution_is_uncertain(row) and engine.has_cleanup_failure(row.project_id)
    assert not calls


async def test_admin_orphan_analysis_can_finish_only_verified_original_receipt(app, client, fake_llm):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json(summary="已提交的原分析"))
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        result = [event async for event in events][-1][1]
    operation = await _mark_cancel(app, execution.task_id)
    engine = app.state.task_engine
    await engine.admin_cancellations._drain(execution.task_key, operation)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.status == "succeeded" and task.admin_cancel_operation_id is None
        assert task.execution_key is None and task.result == result
        assert "_analysis_completion" in task.checkpoint
    assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("state", ["analyze_missing", "analyze_corrupt", "review_missing", "legacy"])
async def test_admin_orphan_batch_unknown_does_not_abort(app, client, make_client, monkeypatch, state):
    identity, execution, _task, _job = await _batch_world(app, client, make_client)
    operation = await _mark_cancel(app, identity["task_id"])
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        checkpoint = deepcopy(task.checkpoint)
        if state == "legacy":
            checkpoint = None
        elif state == "review_missing":
            checkpoint["phase"] = "awaiting_review"
            checkpoint["rich_review"] = {"phase": "awaiting_review"}
        else:
            checkpoint["phase"] = "analyze"
            if state == "analyze_corrupt":
                checkpoint["_batch_completion"] = None
        task.checkpoint = checkpoint
        await db.commit()
    calls = []

    async def abort(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(get_handler("chapter.batch"), "on_abort", abort)
    engine = app.state.task_engine
    await engine.admin_cancellations._drain(identity["task_key"], operation)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, identity["task_id"])
        assert (row.status, row.execution_key, row.admin_cancel_operation_id, row.checkpoint) == (
            "running", execution, operation, checkpoint,
        )
        assert engine.execution_is_uncertain(row) and engine.has_cleanup_failure(row.project_id)
    assert not calls


async def test_admin_orphan_batch_generate_preserves_unknown_receipt_and_releases_claim(app, client, make_client,
                                                                                        monkeypatch):
    identity, execution, _task, _job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        row = await db.get(Task, identity["task_id"])
        row.checkpoint = {**row.checkpoint, "_batch_attempt": str(uuid4())}
        await db.commit()
    operation = await _mark_cancel(app, identity["task_id"])
    engine = app.state.task_engine
    async with app.state.sessionmaker() as db:
        frozen = await db.get(Task, identity["task_id"])
        assert batch_service.capture_abort_snapshot(frozen, recovery=True).claim.claim_key == claim.claim_key
    original_complete = engine.complete_state
    errors, attempts = [], []

    async def checked_complete(*args, **kwargs):
        try:
            outcome = await original_complete(*args, **kwargs)
            attempts.append(outcome[1])
            return outcome
        except Exception as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(engine, "complete_state", checked_complete)
    engine.admin_cancellations.recovering[identity["task_key"]] = (
        engine.admin_cancellations.round_at(_task), execution,
    )
    await engine.admin_cancellations._drain(identity["task_key"], operation)
    assert not errors and attempts == [True]
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert row.status == "interrupted" and row.admin_cancel_operation_id is None
        assert row.execution_key is None and row.checkpoint["current"] is None
        marker = row.checkpoint["_batch_outcome_unknown"]
        assert marker["claim_key"] == claim.claim_key and marker["execution_key"] == execution
        assert marker["operation_id"] == operation and chapter.generation_claim_key is None
        with pytest.raises(ConflictError):
            await task_service.retry_task(db, engine, row.user_id, row.id, expected_task_key=row.statistics_key)


@pytest.mark.parametrize("outcome", ["committed", "rolled_back"])
async def test_admin_batch_unknown_receipt_commit_ack_loss_is_proven(app, client, make_client, monkeypatch, outcome):
    identity, execution, original_task, _job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        row = await db.get(Task, identity["task_id"])
        row.checkpoint = {**row.checkpoint, "_batch_attempt": str(uuid4())}
        await db.commit()
    operation = await _mark_cancel(app, identity["task_id"])
    engine = app.state.task_engine
    engine.admin_cancellations.recovering[identity["task_key"]] = (
        engine.admin_cancellations.round_at(original_task), execution,
    )
    real_commit, lost = AsyncSession.commit, False

    async def uncertain_commit(db):
        nonlocal lost
        with db.no_autoflush:
            checkpoint = await db.scalar(select(Task.checkpoint).where(Task.id == identity["task_id"]))
        if not lost and isinstance(checkpoint, dict) and "_batch_outcome_unknown" in checkpoint:
            lost = True
            if outcome == "committed":
                await real_commit(db)
            else:
                await db.rollback()
            raise RuntimeError("模拟未知提交确认")
        await real_commit(db)

    monkeypatch.setattr(AsyncSession, "commit", uncertain_commit)
    await engine.admin_cancellations._drain(identity["task_key"], operation)
    assert lost
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        if outcome == "committed":
            assert task.status == "interrupted" and task.execution_key is None
            assert task.admin_cancel_operation_id is None
            assert task.checkpoint["_batch_outcome_unknown"]["claim_key"] == claim.claim_key
            assert chapter.generation_claim_key is None
        else:
            assert task.status == "running" and task.execution_key == execution
            assert task.admin_cancel_operation_id == operation
            assert "_batch_outcome_unknown" not in task.checkpoint
            assert chapter.generation_claim_key == claim.claim_key
