"""分析派生收据、子任务及统计必须同事务提交，并按原 UUID 确认未知提交。"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.models import AnalysisIndexReceipt, StoryMemory, Task, TaskStatistic
from app.services import kb_index_service, memory_service, retrieval_service
from tests.test_analysis_memory_enqueue_fencing import _finish_analysis_with_receipt
from tests.test_collaboration_analysis_execution import _world


async def _completed_world(app, client, *, with_embedding=True):
    _, _, project, chapter_id, execution = await _world(app, client, with_embedding=with_embedding)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        await _finish_analysis_with_receipt(db, task, execution)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        analysis_id = task.checkpoint["_analysis_completion"]["analysis_id"]
        memory = StoryMemory(project_id=project["id"], chapter_id=chapter_id, analysis_id=analysis_id,
                             chapter_no=1, kind="summary", origin="auto", content="派生记忆正文")
        db.add(memory)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project["id"], "memory", [memory_service._chunk(memory)])
        await db.commit()
        return execution, memory.id


async def _admit(app, db, execution, attempt):
    return await kb_index_service.enqueue_memory_for_analysis(
        db, execution, attempt=attempt, cipher=app.state.cipher, allow_private=False,
    )


async def _confirm(app, db, execution, attempt, admission):
    return await kb_index_service.confirm_analysis_index_attempt(
        db, execution, attempt=attempt, admission=admission, cipher=app.state.cipher, allow_private=False,
    )


@pytest.mark.parametrize("commit", [False, True])
async def test_atomic_enqueue_and_exact_commit_confirmation(app, client, monkeypatch, commit):
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)
    execution, memory_id = await _completed_world(app, client)
    attempt = kb_index_service.new_analysis_index_attempt()
    async with app.state.sessionmaker() as db:
        admission = await _admit(app, db, execution, attempt)
        assert admission.state == "created"
        if commit:
            await db.commit()
        else:
            await db.rollback()
    async with app.state.sessionmaker() as db:
        if not commit:
            with pytest.raises(kb_index_service.AnalysisIndexCommitUncertain):
                await _confirm(app, db, execution, attempt, admission)
            assert await db.get(AnalysisIndexReceipt, attempt.receipt_key) is None
            assert await db.scalar(select(Task).where(Task.statistics_key == attempt.child_task_key)) is None
            assert await db.scalar(select(TaskStatistic).where(TaskStatistic.key == attempt.child_task_key)) is None
        else:
            confirmed = await _confirm(app, db, execution, attempt, admission)
            assert confirmed == admission
            receipt = await db.get(AnalysisIndexReceipt, attempt.receipt_key)
            task = await db.get(Task, admission.task_id)
            assert receipt.child_task_key == task.statistics_key == attempt.child_task_key
            assert task.user_id == execution.actor_id and task.input["_memory_ref_ids"] == [memory_id]
            assert task.input["_analysis_index_receipt"] == attempt.receipt_key
            assert await db.scalar(select(TaskStatistic).where(TaskStatistic.key == attempt.child_task_key))


async def test_deleted_child_does_not_allow_reenqueue(app, client, monkeypatch):
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)
    execution, _ = await _completed_world(app, client)
    attempt = kb_index_service.new_analysis_index_attempt()
    async with app.state.sessionmaker() as db:
        admission = await _admit(app, db, execution, attempt)
        await db.commit()
        await db.delete(await db.get(Task, admission.task_id))
        await db.commit()
    async with app.state.sessionmaker() as db:
        confirmed = await _confirm(app, db, execution, attempt, admission)
        assert confirmed.task_id is None and confirmed.child_task_key == attempt.child_task_key
        await db.rollback()
    async with app.state.sessionmaker() as db:
        other = kb_index_service.new_analysis_index_attempt()
        duplicate = await _admit(app, db, execution, other)
        assert duplicate.state == "existing" and duplicate.receipt_key == attempt.receipt_key
        assert await db.scalar(select(Task).where(Task.statistics_key == other.child_task_key)) is None


async def test_confirmation_never_accepts_another_attempt(app, client, monkeypatch):
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)
    execution, _ = await _completed_world(app, client)
    attempt = kb_index_service.new_analysis_index_attempt()
    async with app.state.sessionmaker() as db:
        admission = await _admit(app, db, execution, attempt)
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(kb_index_service.AnalysisIndexCommitUncertain):
            await _confirm(app, db, execution, kb_index_service.new_analysis_index_attempt(), admission)
        with pytest.raises(kb_index_service.AnalysisIndexCommitUncertain):
            await _confirm(app, db, execution, attempt, replace(admission, memory_hash="0" * 64))


async def test_statistics_failure_rolls_back_receipt_and_task(app, client, monkeypatch):
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)
    execution, _ = await _completed_world(app, client)
    attempt = kb_index_service.new_analysis_index_attempt()

    async def failed_statistics(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(kb_index_service.task_statistics_service, "record_created", failed_statistics)
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError):
            await _admit(app, db, execution, attempt)
        # Even a caller that catches the error and commits cannot leave half an admission.
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await db.get(AnalysisIndexReceipt, attempt.receipt_key) is None
        assert await db.scalar(select(Task).where(Task.statistics_key == attempt.child_task_key)) is None
        assert await db.scalar(select(TaskStatistic).where(TaskStatistic.key == attempt.child_task_key)) is None


async def test_replacement_parent_input_is_not_used_to_confirm(app, client, monkeypatch):
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)
    execution, _ = await _completed_world(app, client)
    attempt = kb_index_service.new_analysis_index_attempt()
    async with app.state.sessionmaker() as db:
        admission = await _admit(app, db, execution, attempt)
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.input = {**task.input, "request": {"chapter_id": execution.task_chapter_id + 1}}
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(kb_index_service.AnalysisIndexCommitUncertain):
            await _confirm(app, db, execution, attempt, admission)
        assert await db.get(AnalysisIndexReceipt, attempt.receipt_key) is not None
        assert await db.scalar(select(Task).where(Task.statistics_key == attempt.child_task_key)) is not None


@pytest.mark.parametrize("damage", ["order", "duplicate_ref", "duplicate_retrieval", "bool", "hash", "extra"])
def test_receipt_references_require_strict_canonical_structure(damage):
    first = {"ref_id": 1, "analysis_id": 3, "retrieval_id": 8, "version": 1, "content_hash": "a" * 64}
    second = {"ref_id": 2, "analysis_id": 3, "retrieval_id": 9, "version": 1, "content_hash": "b" * 64}
    refs = [first, second]
    if damage == "order":
        refs.reverse()
    elif damage == "duplicate_ref":
        second["ref_id"] = 1
    elif damage == "duplicate_retrieval":
        second["retrieval_id"] = 8
    elif damage == "bool":
        first["version"] = True
    elif damage == "hash":
        first["content_hash"] = "z" * 64
    else:
        first["text"] = "不得存正文"
    with pytest.raises(retrieval_service.EmbeddingPolicyError):
        retrieval_service.validate_analysis_index_refs(
            refs, analysis_id=3, memory_hash=retrieval_service._canonical_digest(refs),
        )


async def test_keyword_selection_persists_no_work_without_task(app, client, monkeypatch):
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)
    execution, _ = await _completed_world(app, client, with_embedding=False)
    attempt = kb_index_service.new_analysis_index_attempt()
    async with app.state.sessionmaker() as db:
        admission = await _admit(app, db, execution, attempt)
        assert admission.state == "no_work" and admission.child_task_key is None
        await db.commit()
    async with app.state.sessionmaker() as db:
        confirmed = await _confirm(app, db, execution, attempt, admission)
        assert confirmed == admission
        receipt = await db.get(AnalysisIndexReceipt, attempt.receipt_key)
        assert receipt.outcome == "no_work" and receipt.memory_refs
        assert await db.scalar(select(Task).where(Task.statistics_key == attempt.child_task_key)) is None
