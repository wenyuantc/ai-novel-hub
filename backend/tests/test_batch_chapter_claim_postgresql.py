"""PostgreSQL 双连接验证批量 attempt 的 checkpoint 与清理 fencing。"""

import asyncio
import copy
from uuid import uuid4

import httpx
import pytest

from app.core.errors import ConflictError
from app.models import Chapter, SceneReviewAnalysisReceipt, Task
from app.services import batch_service, chapter_claim
from app.services.task_engine.handlers.batch import BatchHandler
from tests.test_batch_chapter_claim import _batch_world, _captured_running_attempt

pytest_plugins = ("tests.test_collaboration_review_read_postgresql",)


async def test_stale_checkpoint_waits_and_cannot_erase_new_claim(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        identity, execution, _task, old_job = await _batch_world(app, client, lambda: None)
        async with app.state.sessionmaker() as newer:
            first = await chapter_claim.claim_batch_chapter(newer, **identity, execution_key=execution)
            assert await chapter_claim.release_chapter_claim(newer, first)
            second = await chapter_claim.claim_batch_chapter(newer, **identity, execution_key=execution)
            started = asyncio.Event()

            async def old_write():
                async with app.state.sessionmaker() as db:
                    await db.connection()  # 先占用另一条真实 PostgreSQL 连接。
                    started.set()
                    with pytest.raises(ConflictError):
                        await batch_service._write_checkpoint(db, old_job)
                    await db.commit()

            pending = asyncio.create_task(old_write())
            try:
                await asyncio.wait_for(started.wait(), 10)
                done, _ = await asyncio.wait({pending}, timeout=0.2)
                assert not done
                await newer.commit()
                await asyncio.wait_for(pending, 10)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, identity["task_id"])
            chapter = await db.get(Chapter, identity["chapter_id"])
            assert chapter.generation_claim_key == second.claim_key
            assert task.checkpoint["current"]["_chapter_claim"]["claim_key"] == second.claim_key


async def test_old_abort_does_not_clear_later_attempt(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        identity, execution, task, job = await _batch_world(app, client, lambda: None)
        async with app.state.sessionmaker() as db:
            first = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
            await db.commit()
        job.active_claim = first
        task._batch_job = job
        async with app.state.sessionmaker() as other:
            assert await chapter_claim.release_chapter_claim(other, first)
            second = await chapter_claim.claim_batch_chapter(other, **identity, execution_key=execution)
            await other.commit()
        with pytest.raises(ConflictError):
            await BatchHandler().on_abort(app.state.task_engine.ctx, task, execution=job.execution_identity)
        async with app.state.sessionmaker() as db:
            chapter = await db.get(Chapter, identity["chapter_id"])
            persisted = await db.get(Task, identity["task_id"])
            assert chapter.status == "generating" and chapter.generation_claim_key == second.claim_key
            assert persisted.checkpoint["current"]["_chapter_claim"]["claim_key"] == second.claim_key


async def test_old_terminal_cas_cannot_finish_later_attempt(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        identity, execution, task, job = await _batch_world(app, client, lambda: None)
        task._batch_job = job
        async with app.state.sessionmaker() as newer:
            claim = await chapter_claim.claim_batch_chapter(newer, **identity, execution_key=execution)
            started = asyncio.Event()

            async def finish_old():
                started.set()
                return await app.state.task_engine.complete_state(
                    task, expected_status="running", status="failed", result=None, error="旧任务结束",
                    text="", progress=0, abort_ok=True,
                )

            pending = asyncio.create_task(finish_old())
            try:
                await asyncio.wait_for(started.wait(), 10)
                done, _ = await asyncio.wait({pending}, timeout=0.2)
                assert not done
                await newer.commit()
                _row, committed = await asyncio.wait_for(pending, 10)
                assert not committed
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with app.state.sessionmaker() as db:
            persisted = await db.get(Task, identity["task_id"])
            chapter = await db.get(Chapter, identity["chapter_id"])
            assert persisted.status == "running" and persisted.execution_key == execution
            assert chapter.generation_claim_key == claim.claim_key


async def test_abort_snapshot_rejects_attempt_aba_after_second_release(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        identity, execution, _task, _job, first, snapshot = await _captured_running_attempt(app, client, lambda: None)
        async with app.state.sessionmaker() as newer:
            assert await chapter_claim.release_chapter_claim(newer, first)
            task = await newer.get(Task, identity["task_id"])
            checkpoint = copy.deepcopy(task.checkpoint)
            checkpoint["_batch_attempt"] = str(uuid4())
            task.checkpoint = checkpoint
            await newer.flush()
            second = await chapter_claim.claim_batch_chapter(newer, **identity, execution_key=execution)
            assert await chapter_claim.release_chapter_claim(newer, second)
            await newer.commit()
        with pytest.raises(ConflictError):
            await batch_service.restore_abort_snapshot(app.state.task_engine.ctx, snapshot)
        async with app.state.sessionmaker() as db:
            persisted = await db.get(Task, identity["task_id"])
            assert persisted.status == "running" and persisted.checkpoint["current"] is None
            assert persisted.checkpoint["_batch_attempt"] != snapshot.checkpoint["_batch_attempt"]


async def test_old_analysis_failure_waits_for_replaced_receipt(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        identity, _execution, _task, job = await _batch_world(app, client, lambda: None)
        ref = {"run_key": str(uuid4()), "task_key": identity["task_key"],
               "chapter_id": identity["chapter_id"], "content_hash": "a" * 64}
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, identity["task_id"])
            checkpoint = copy.deepcopy(task.checkpoint)
            checkpoint.update(phase="analyze", rich_analysis=ref)
            task.checkpoint = checkpoint
            db.add(SceneReviewAnalysisReceipt(**ref, status="running"))
            await db.commit()
        job.checkpoint = copy.deepcopy(checkpoint)
        job.persisted_checkpoint = copy.deepcopy(checkpoint)
        async with app.state.sessionmaker() as newer:
            receipt = await newer.get(SceneReviewAnalysisReceipt, ref["run_key"])
            receipt.content_hash = "b" * 64
            await newer.flush()
            started = asyncio.Event()

            async def fail_old():
                started.set()
                with pytest.raises(ConflictError):
                    await batch_service._fail_review_analysis(app.state.task_engine.ctx, job, ref)

            pending = asyncio.create_task(fail_old())
            try:
                await asyncio.wait_for(started.wait(), 10)
                done, _ = await asyncio.wait({pending}, timeout=0.2)
                assert not done
                await newer.commit()
                await asyncio.wait_for(pending, 10)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with app.state.sessionmaker() as db:
            receipt = await db.get(SceneReviewAnalysisReceipt, ref["run_key"])
            assert receipt.content_hash == "b" * 64 and receipt.status == "running"


async def test_uncertain_release_cannot_commit_over_new_attempt(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        identity, execution, _task, _job, first, snapshot = await _captured_running_attempt(app, client, lambda: None)
        async with app.state.sessionmaker() as newer:
            assert await chapter_claim.release_chapter_claim(newer, first)
            task = await newer.get(Task, identity["task_id"])
            checkpoint = copy.deepcopy(task.checkpoint)
            checkpoint["_batch_attempt"] = str(uuid4())
            task.checkpoint = checkpoint
            await newer.flush()
            second = await chapter_claim.claim_batch_chapter(newer, **identity, execution_key=execution)
            started = asyncio.Event()

            async def release_old():
                async with app.state.sessionmaker() as db:
                    await db.connection()
                    started.set()
                    with pytest.raises(ConflictError):
                        await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=None)
                    await db.commit()

            pending = asyncio.create_task(release_old())
            try:
                await asyncio.wait_for(started.wait(), 10)
                done, _ = await asyncio.wait({pending}, timeout=0.2)
                assert not done
                await newer.commit()
                await asyncio.wait_for(pending, 10)
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, identity["task_id"])
            chapter = await db.get(Chapter, identity["chapter_id"])
            assert "_batch_outcome_unknown" not in task.checkpoint
            assert task.checkpoint["current"]["_chapter_claim"]["claim_key"] == second.claim_key
            assert chapter.generation_claim_key == second.claim_key


async def test_uncertain_release_and_marker_commit_atomically(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        identity, _execution, _task, _job, claim, snapshot = await _captured_running_attempt(app, client, lambda: None)
        async with app.state.sessionmaker() as writer:
            await batch_service.release_uncertain_attempt(writer, snapshot, expected_operation_id=None)
            task = await writer.get(Task, identity["task_id"])
            task.status, task.execution_key = "interrupted", None
            await writer.flush()
            # 另一条连接在提交前仍只能看到完整旧占用，而非无凭据的半释放。
            async with app.state.sessionmaker() as reader:
                prior_task = await reader.get(Task, identity["task_id"])
                prior_chapter = await reader.get(Chapter, identity["chapter_id"])
                assert prior_task.status == "running" and prior_task.checkpoint == snapshot.checkpoint
                assert prior_chapter.generation_claim_key == claim.claim_key
            await writer.commit()
        async with app.state.sessionmaker() as reader:
            assert await batch_service.confirm_uncertain_attempt(
                reader, snapshot, status="interrupted", expected_operation_id=None,
            )
