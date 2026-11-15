"""真实 PostgreSQL 双连接验证分析执行身份及提交与撤权的先后。"""

import asyncio
from contextlib import aclosing, suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AnalysisChange, ChapterAnalysis, StoryMemory, Task, User
from app.services import analysis_service, project_ai_config
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import analysis_json, create_ai_config
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_analysis_execution import _batch_world, _replace_config_with_same_id

postgres_analysis_execution_apps = _postgres_fixture


async def _world(app, owner, guest, fake_llm, *, with_embedding=False):
    project, _, base = await _setup(owner, guest)
    actor = (await owner.get("/api/v1/auth/me")).json()
    config = await create_ai_config(owner)
    if with_embedding:
        await create_ai_config(owner, name="向量模型", model_type="embedding", model="embed")
    chapter = await owner.post(f"{base}/chapters", json={"title": "并发分析", "content": "章节正文"})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    async with app.state.sessionmaker() as db:
        token_version = (await db.get(User, actor["id"])).token_version
        binding = await project_ai_config.resolve_project_model(
            db, actor["id"], project["id"], "text", cipher=app.state.cipher, config_id=config["id"],
        )
        selection = await project_ai_config.resolve_shared_embedding(
            db, actor["id"], project["id"], cipher=app.state.cipher,
        )
        task = Task(user_id=actor["id"], project_id=project["id"], chapter_id=chapter_id,
                    kind="chapter.analyze", status="queued",
                    input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id,
                           "_task_actor_token_version": token_version,
                           "_project_model": project_ai_config.serialize_project_model(binding),
                           "_shared_embedding": project_ai_config.serialize_shared_embedding(selection)})
        db.add(task)
        await db.flush()
        identity = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", identity.execution_key
        execution = analysis_service.analysis_execution_from_claim(identity)
        await db.commit()
    pending = await analysis_service.begin_analysis_pending(app.state.sessionmaker, execution)
    async with app.state.sessionmaker() as db:
        job = await analysis_service.build_analysis_job(
            db, user_id=actor["id"], chapter_id=chapter_id, ai_config_id=None, cipher=app.state.cipher,
            factory=fake_llm.factory, ai_timeout=5, allow_private=False, execution=execution, pending=pending,
        )
    fake_llm.push(analysis_json(summary="并发分析结果"))
    return project, chapter_id, execution, job


async def _counts(app):
    async with app.state.sessionmaker() as db:
        return [await db.scalar(select(func.count()).select_from(model))
                for model in (ChapterAnalysis, AnalysisChange, StoryMemory)]


@pytest.mark.parametrize(("change", "isolation"), [
    ("replace", "READ COMMITTED"),
    ("resume", "READ COMMITTED"),
    ("disable", "READ COMMITTED"),
    ("resume", "REPEATABLE READ"),
    ("input", "READ COMMITTED"),
    ("input", "REPEATABLE READ"),
    ("token", "READ COMMITTED"),
    ("token", "REPEATABLE READ"),
    ("cancel_requested", "READ COMMITTED"),
    ("cancel_requested", "REPEATABLE READ"),
    ("model_aba", "READ COMMITTED"),
    ("model_aba", "REPEATABLE READ"),
    ("embedding_aba", "READ COMMITTED"),
])
async def test_postgres_analysis_rejects_prior_execution_before_save(
    postgres_analysis_execution_apps, fake_llm, monkeypatch, change, isolation,
):
    owner_app, guest_app = postgres_analysis_execution_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, chapter_id, execution, job = await _world(
            owner_app, owner, guest, fake_llm, with_embedding=change == "embedding_aba",
        )
        engine = owner_app.state.engine.execution_options(isolation_level=isolation)
        maker = async_sessionmaker(engine, expire_on_commit=False, info=owner_app.state.sessionmaker.kw["info"])
        ready, release = asyncio.Event(), asyncio.Event()
        info = {}
        original = analysis_service._analysis_write_barrier

        async def gated(db, identity, *, status="running"):
            info["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            info["isolation"] = await db.scalar(text("SHOW transaction_isolation"))
            ready.set()
            await release.wait()
            return await original(db, identity, status=status)

        monkeypatch.setattr(analysis_service, "_analysis_write_barrier", gated)

        async def consume():
            async with aclosing(analysis_service.run_analysis_job(job, maker)) as events:
                return [item async for item in events]

        pending = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 15)
            assert info["isolation"] == isolation.lower()
            async with guest_app.state.sessionmaker() as db:
                assert await db.scalar(text("SELECT pg_backend_pid()")) != info["pid"]
                task = await db.get(Task, execution.task_id)
                if change == "replace":
                    await db.delete(task)
                    await db.flush()
                    db.add(Task(id=execution.task_id, user_id=execution.actor_id, project_id=project["id"],
                                chapter_id=chapter_id, kind="chapter.analyze", status="running",
                                queued_at=execution.queued_at, execution_key=str(uuid4()),
                                input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id}))
                elif change == "resume":
                    task.queued_at += timedelta(seconds=1)
                    task.execution_key = str(uuid4())
                elif change == "input":
                    task.input = {**task.input, "request": {**task.input["request"], "ai_config_id": 999}}
                elif change == "token":
                    (await db.get(User, execution.actor_id)).token_version += 1
                elif change == "cancel_requested":
                    task.admin_cancel_requested_at = datetime.now(UTC)
                elif change in {"model_aba", "embedding_aba"}:
                    config_id = (execution.model_binding.config_id if change == "model_aba"
                                 else execution.embedding_selection.model.config_id)
                    await _replace_config_with_same_id(db, config_id)
                else:
                    await db.execute(update(User).where(User.id == execution.actor_id).values(is_active=False))
                await db.commit()
            release.set()
            with pytest.raises(VisibilityPolicyError):
                await asyncio.wait_for(pending, 15)
            assert await _counts(owner_app) == [0, 0, 0]
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_postgres_analysis_save_wins_when_it_already_holds_original_task(
    postgres_analysis_execution_apps, fake_llm, monkeypatch,
):
    owner_app, guest_app = postgres_analysis_execution_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _, _, execution, job = await _world(owner_app, owner, guest, fake_llm)
        ready, release = asyncio.Event(), asyncio.Event()
        info = {}
        original = analysis_service._analysis_write_barrier

        async def gated(db, identity, *, status="running"):
            await original(db, identity, status=status)
            info["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            await release.wait()

        monkeypatch.setattr(analysis_service, "_analysis_write_barrier", gated)

        async def consume():
            async with aclosing(analysis_service.run_analysis_job(job, owner_app.state.sessionmaker)) as events:
                return [item async for item in events]

        async def resume():
            async with guest_app.state.sessionmaker() as db:
                assert await db.scalar(text("SELECT pg_backend_pid()")) != info["pid"]
                await db.execute(update(Task).where(Task.id == execution.task_id).values(
                    queued_at=execution.queued_at + timedelta(seconds=1), execution_key=str(uuid4()),
                ))
                await db.commit()

        pending, mutation = asyncio.create_task(consume()), None
        try:
            await asyncio.wait_for(ready.wait(), 15)
            mutation = asyncio.create_task(resume())
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, info["pid"]), 15)
            release.set()
            events = await asyncio.wait_for(pending, 15)
            await asyncio.wait_for(mutation, 15)
            assert events[-1][0] == "done"
            assert (await _counts(owner_app))[0] == 1
        finally:
            release.set()
            for task in (pending, mutation):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("outcome", ["committed", "rolled_back", "replaced", "corrupt"])
async def test_postgres_analysis_commit_ack_loss_requires_original_receipt(
    postgres_analysis_execution_apps, fake_llm, monkeypatch, outcome,
):
    owner_app, guest_app = postgres_analysis_execution_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, chapter_id, execution, job = await _world(owner_app, owner, guest, fake_llm)
        original_commit = AsyncSession.commit
        lost = False

        async def uncertain_commit(db):
            nonlocal lost
            if not lost and any(isinstance(row, ChapterAnalysis) for row in db.identity_map.values()):
                lost = True
                if outcome == "rolled_back":
                    await db.rollback()
                else:
                    await original_commit(db)
                    if outcome in {"replaced", "corrupt"}:
                        async with guest_app.state.sessionmaker() as other:
                            task = await other.get(Task, execution.task_id)
                            if outcome == "replaced":
                                await other.delete(task)
                                await other.flush()
                                other.add(Task(
                                    id=execution.task_id, user_id=execution.actor_id, project_id=project["id"],
                                    chapter_id=chapter_id, kind="chapter.analyze", status="running",
                                    queued_at=execution.queued_at, execution_key=str(uuid4()),
                                    input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id},
                                ))
                            else:
                                checkpoint = dict(task.checkpoint)
                                checkpoint["_analysis_completion"] = {
                                    **checkpoint["_analysis_completion"], "analysis_hash": "0" * 64,
                                }
                                task.checkpoint = checkpoint
                            await original_commit(other)
                raise RuntimeError("模拟 PostgreSQL 提交确认丢失")
            return await original_commit(db)

        monkeypatch.setattr(AsyncSession, "commit", uncertain_commit)
        if outcome == "committed":
            async with aclosing(analysis_service.run_analysis_job(job, owner_app.state.sessionmaker)) as events:
                result = [event async for event in events]
            assert result[-1][0] == "done"
        else:
            with pytest.raises(analysis_service.AnalysisCommitUncertain):
                async with aclosing(analysis_service.run_analysis_job(job, owner_app.state.sessionmaker)) as events:
                    async for _ in events:
                        pass
        assert lost and len(fake_llm.calls) == 1
        async with owner_app.state.sessionmaker() as db:
            task = await db.get(Task, execution.task_id)
            if outcome == "replaced":
                assert task.statistics_key != execution.task_key
            else:
                assert task.statistics_key == execution.task_key and task.execution_key == execution.execution_key
                key = "_analysis_pending" if outcome == "rolled_back" else "_analysis_completion"
                assert key in task.checkpoint
            count = await db.scalar(select(func.count()).select_from(ChapterAnalysis))
            assert count == (0 if outcome == "rolled_back" else 1)


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
async def test_postgres_batch_analysis_does_not_save_after_attempt_moves(
    postgres_analysis_execution_apps, fake_llm, monkeypatch, isolation,
):
    owner_app, other_app = postgres_analysis_execution_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner:
        actor, _, _, chapter_id, _, execution, ref = await _batch_world(owner_app, owner)
        async with owner_app.state.sessionmaker() as db:
            job = await analysis_service.build_analysis_job(
                db, user_id=actor["id"], chapter_id=chapter_id, ai_config_id=None, cipher=owner_app.state.cipher,
                factory=fake_llm.factory, ai_timeout=5, allow_private=False, execution=execution, review_analysis=ref,
            )
        fake_llm.push(analysis_json())
        maker = async_sessionmaker(owner_app.state.engine.execution_options(isolation_level=isolation),
                                   expire_on_commit=False, info=owner_app.state.sessionmaker.kw["info"])
        ready, release = asyncio.Event(), asyncio.Event()
        info = {}
        original = analysis_service._analysis_write_barrier

        async def gated(db, identity, *, status="running"):
            info["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            info["isolation"] = await db.scalar(text("SHOW transaction_isolation"))
            ready.set()
            await release.wait()
            return await original(db, identity, status=status)

        monkeypatch.setattr(analysis_service, "_analysis_write_barrier", gated)

        async def consume():
            async with aclosing(analysis_service.run_analysis_job(job, maker)) as events:
                return [event async for event in events]

        pending = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(ready.wait(), 15)
            assert info["isolation"] == isolation.lower()
            async with other_app.state.sessionmaker() as db:
                assert await db.scalar(text("SELECT pg_backend_pid()")) != info["pid"]
                await db.execute(update(Task).where(Task.id == execution.task_id).values(step_cursor=1))
                await db.commit()
            release.set()
            with pytest.raises(VisibilityPolicyError):
                await asyncio.wait_for(pending, 15)
            assert await _counts(owner_app) == [0, 0, 0]
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_postgres_batch_analysis_save_wins_after_attempt_lock(
    postgres_analysis_execution_apps, fake_llm, monkeypatch,
):
    owner_app, other_app = postgres_analysis_execution_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner:
        actor, _, _, chapter_id, _, execution, ref = await _batch_world(owner_app, owner)
        async with owner_app.state.sessionmaker() as db:
            job = await analysis_service.build_analysis_job(
                db, user_id=actor["id"], chapter_id=chapter_id, ai_config_id=None, cipher=owner_app.state.cipher,
                factory=fake_llm.factory, ai_timeout=5, allow_private=False, execution=execution, review_analysis=ref,
            )
        fake_llm.push(analysis_json())
        ready, release = asyncio.Event(), asyncio.Event()
        info = {}
        original = analysis_service._require_batch_analysis

        async def gated(db, identity, reference, *, read_lock, status="running"):
            await original(db, identity, reference, read_lock=read_lock, status=status)
            if not read_lock:
                info["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                ready.set()
                await release.wait()

        monkeypatch.setattr(analysis_service, "_require_batch_analysis", gated)

        async def consume():
            async with aclosing(analysis_service.run_analysis_job(job, owner_app.state.sessionmaker)) as events:
                return [event async for event in events]

        async def move():
            async with other_app.state.sessionmaker() as db:
                assert await db.scalar(text("SELECT pg_backend_pid()")) != info["pid"]
                await db.execute(update(Task).where(Task.id == execution.task_id).values(step_cursor=1))
                await db.commit()

        pending, mutation = asyncio.create_task(consume()), None
        try:
            await asyncio.wait_for(ready.wait(), 15)
            mutation = asyncio.create_task(move())
            await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, info["pid"]), 15)
            release.set()
            assert (await asyncio.wait_for(pending, 15))[-1][0] == "done"
            await asyncio.wait_for(mutation, 15)
            assert (await _counts(owner_app))[0] == 1
        finally:
            release.set()
            for task in (pending, mutation):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
