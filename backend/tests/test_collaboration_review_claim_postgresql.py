"""PostgreSQL 双连接：提案与章节复位/任务换轮按原事实竞争。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.models import Chapter, SceneReviewProposal, Task
from app.services import chapter_claim, rich_document_review
from tests.test_collaboration_review_claim import _claimed_proposal_world, _persist
from tests.test_rich_document_api import KEY

pytest_plugins = ("tests.test_collaboration_review_read_postgresql",)


async def test_later_claim_wins_before_stale_proposal_save(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        job, plans, logs, document_url, source, _ = await _claimed_proposal_world(app, client)
        execution, queued_at = str(uuid4()), datetime.now(UTC)
        async with app.state.sessionmaker() as newer:
            assert await chapter_claim.reset_generating_chapter(
                newer, job.user_id, job.project_id, job.chapter_id, "draft",
            )
            task = Task(user_id=job.user_id, project_id=job.project_id, chapter_id=None, kind="chapter.batch",
                        status="running", resource_key=f"batch:{job.project_id}", queued_at=queued_at,
                        execution_key=execution, input={"request": {"project_id": job.project_id},
                                                       "chapter_ids": [job.chapter_id]},
                        checkpoint={"current": None})
            newer.add(task)
            await newer.flush()
            claim = await chapter_claim.claim_batch_chapter(
                newer, task_id=task.id, task_key=task.statistics_key, actor_id=job.user_id, project_id=job.project_id,
                chapter_id=job.chapter_id, queued_at=queued_at, execution_key=execution, grant_identity=None,
            )
            entered = asyncio.Event()

            async def stale_save():
                async with app.state.sessionmaker() as db:
                    chapter = await db.get(Chapter, job.chapter_id)
                    entered.set()
                    with pytest.raises(ConflictError):
                        await rich_document_review.persist_pending(db, chapter, job, plans, logs)
                    await db.commit()

            blocked = asyncio.create_task(stale_save())
            try:
                await asyncio.wait_for(entered.wait(), 10)
                done, _ = await asyncio.wait({blocked}, timeout=0.2)
                assert not done  # 新 claim 尚未提交，旧写入必须等待同一数据库锁。
                await newer.commit()
                await asyncio.wait_for(blocked, 10)
            finally:
                if not blocked.done():
                    blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)
        async with app.state.sessionmaker() as db:
            chapter = await db.get(Chapter, job.chapter_id)
            assert chapter.status == "generating" and chapter.generation_claim_key == claim.claim_key
            assert await db.scalar(select(func.count()).select_from(SceneReviewProposal)) == 0
        assert (await client.get(document_url)).json() == source


async def test_proposal_save_wins_before_manual_reset(postgres_review_apps, monkeypatch):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        job, plans, logs, document_url, source, _ = await _claimed_proposal_world(app, client)
        original = rich_document_review._write_payload
        entered, release, reset_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed_write(db, proposal, payload):
            entered.set()
            await release.wait()
            await original(db, proposal, payload)

        async def reset():
            async with app.state.sessionmaker() as db:
                reset_started.set()
                result = await chapter_claim.reset_generating_chapter(
                    db, job.user_id, job.project_id, job.chapter_id, "done",
                )
                await db.commit()
                return result

        monkeypatch.setattr(rich_document_review, "_write_payload", delayed_write)
        saving = asyncio.create_task(_persist(app, job, plans, logs))
        resetting = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            resetting = asyncio.create_task(reset())
            await asyncio.wait_for(reset_started.wait(), 10)
            done, _ = await asyncio.wait({resetting}, timeout=0.2)
            assert not done
            release.set()
            identity = await asyncio.wait_for(saving, 10)
            assert not await asyncio.wait_for(resetting, 10)
        finally:
            release.set()
            for pending in (saving, resetting):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(pending for pending in (saving, resetting) if pending), return_exceptions=True)
        async with app.state.sessionmaker() as db:
            chapter = await db.get(Chapter, job.chapter_id)
            proposal = await db.scalar(select(SceneReviewProposal).where(
                SceneReviewProposal.token == identity["token"],
            ))
            assert chapter.status == "draft" and chapter.generation_claim_key is None
            assert proposal.status == "pending"
        assert (await client.get(document_url)).json() == source


async def test_task_round_changes_while_accept_waits(postgres_review_apps, monkeypatch):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
        identity = await _persist(app, job, plans, logs, pause=True)
        original = rich_document_review._owned_proposal
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused_lookup(db, *args, **kwargs):
            found = await original(db, *args, **kwargs)
            if not kwargs.get("lock", False):
                entered.set()
                await release.wait()
            return found

        monkeypatch.setattr(rich_document_review, "_owned_proposal", paused_lookup)
        path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
        request = asyncio.create_task(client.post(path, json={
            "task_key": identity["task_key"], "run_key": identity["run_key"],
            "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}],
            "confirm_format_loss": True,
        }))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            async with app.state.sessionmaker() as other:
                task = await other.get(Task, job.task_id)
                task.queued_at += timedelta(microseconds=1)
                await other.commit()
            release.set()
            response = await asyncio.wait_for(request, 10)
            assert response.status_code == 409, response.text
        finally:
            release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        assert (await client.get(document_url)).json() == source
        async with app.state.sessionmaker() as db:
            proposal = await db.scalar(select(SceneReviewProposal).where(
                SceneReviewProposal.token == identity["token"],
            ))
            assert proposal.status == "pending"


async def test_single_pending_event_rejects_input_changed_while_waiting(postgres_review_apps):
    app, _ = postgres_review_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        job, plans, logs, _document_url, _source, _scene = await _claimed_proposal_world(app, client)
        identity = await _persist(app, job, plans, logs)
        async with app.state.sessionmaker() as newer:
            task = await newer.scalar(select(Task).where(Task.id == job.task_id).with_for_update())
            task.input = {**task.input, "unexpected": "changed"}
            await newer.flush()
            started = asyncio.Event()

            async def verify_old():
                async with app.state.sessionmaker() as db:
                    await db.connection()
                    started.set()
                    with pytest.raises(ConflictError):
                        await rich_document_review.verify_single_pending_event(
                            db, job.execution_identity, job.chapter_claim, identity,
                        )

            pending = asyncio.create_task(verify_old())
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
            proposal = await db.scalar(select(SceneReviewProposal).where(
                SceneReviewProposal.token == identity["token"],
            ))
            assert proposal.status == "pending"
