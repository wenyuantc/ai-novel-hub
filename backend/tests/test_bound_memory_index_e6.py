"""分析派生的作者记忆索引固定原任务、模型和每批正文。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.ai.providers import create_provider
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import (
    AIConfig,
    AnalysisIndexReceipt,
    Chapter,
    ChapterAnalysis,
    RetrievalChunk,
    SceneReviewAnalysisReceipt,
    StoryMemory,
    Task,
    User,
)
from app.services import (
    analysis_service,
    collaboration_ai_guard,
    kb_index_service,
    memory_service,
    project_ai_config,
    retrieval_service,
)
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import create_project, register, wait_task
from tests.test_ai_visibility_dispatch import _server


async def _owner_job(app, client, url, *, batch_retry=False):
    await app.state.task_engine.stop()
    user = await register(client, "bound-index-owner")
    project = await create_project(client)
    token = decode_access_token(client.cookies.get(ACCESS_TOKEN_COOKIE), app.state.settings.jwt_secret)
    assert token and token[0] == user["id"]
    async with app.state.sessionmaker() as db:
        config = AIConfig(user_id=user["id"], name="原向量", provider="openai_compat", model="old-model",
                          base_url=f"{url}/v1", api_key_enc=app.state.cipher.encrypt("bound-key"),
                          model_type="embedding", is_default=True)
        chapter = Chapter(project_id=project["id"], chapter_no=1, title="分析章")
        db.add_all([config, chapter])
        await db.flush()
        if batch_retry:
            source = Task(user_id=user["id"], project_id=project["id"], kind="chapter.batch", status="interrupted",
                          input={})
            db.add(source)
            await db.flush()
            middle = Task(user_id=user["id"], project_id=project["id"], kind="chapter.batch", status="failed",
                          input={}, retry_of_id=source.id)
            db.add(middle)
            await db.flush()
            parent = Task(user_id=user["id"], project_id=project["id"], kind="chapter.batch", status="succeeded",
                          input={}, retry_of_id=middle.id)
        else:
            source = middle = None
            parent = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter.id,
                          kind="chapter.analyze", status="succeeded", input={})
        db.add(parent)
        await db.flush()
        analysis = ChapterAnalysis(project_id=project["id"], chapter_id=chapter.id, chapter_no=1,
                                   task_id=middle.id if middle else parent.id,
                                   content_hash="a" * 64, summary="已完成分析")
        db.add(analysis)
        await db.flush()
        source_key = str(uuid4())
        if batch_retry:
            db.add(SceneReviewAnalysisReceipt(run_key=source_key, task_key=source.statistics_key,
                                               chapter_id=chapter.id, analysis_id=analysis.id,
                                               content_hash=analysis.content_hash, status="completed"))
        memories = [
            StoryMemory(project_id=project["id"], chapter_id=chapter.id, analysis_id=analysis.id, chapter_no=1,
                        kind="summary", origin="auto", content="需要向量的记忆"),
            StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", origin="manual",
                        content="其他模型的记忆"),
        ]
        db.add_all(memories)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project["id"], "memory", [
            memory_service._chunk(item) for item in memories
        ])
        await db.flush()
        other = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memories[1].id,
        ))
        other.embedding, other.embedding_key = b"other-vector", "other:model:2"
        selected = await project_ai_config.resolve_shared_embedding(
            db, user["id"], project["id"], cipher=app.state.cipher, allow_private=True,
        )
        target_index = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memories[0].id,
        ))
        snapshots = await retrieval_service.snapshot_embedding_inputs(db, project["id"], [target_index.id])
        refs = [retrieval_service.analysis_index_memory_ref(snapshots[0], analysis.id)]
        receipt_key, child_key, parent_key = str(uuid4()), str(uuid4()), parent.statistics_key
        task = Task(statistics_key=child_key, user_id=user["id"], project_id=project["id"],
                    kind="kb.index", status="queued",
                    serial_key=kb_index_service.serial_key(project["id"]), input={
                        "request": {"project_id": project["id"], "mode": "memory"},
                        "_task_actor_token_version": token[1],
                        "_shared_embedding": project_ai_config.serialize_shared_embedding(selected),
                        "_memory_ref_ids": [memories[0].id],
                        "_memory_analysis_id": analysis.id,
                        "_analysis_index_receipt": receipt_key,
                    })
        receipt = AnalysisIndexReceipt(
            receipt_key=receipt_key, source_kind="batch" if batch_retry else "single", source_key=source_key,
            source_task_key=source.statistics_key if source else parent_key, parent_task_key=parent_key,
            parent_execution_key=str(uuid4()), parent_queued_at=parent.queued_at,
            actor_id=user["id"], actor_token_version=token[1], project_id=project["id"], chapter_id=chapter.id,
            analysis_id=analysis.id, grant_invitation_id=None, grant_revision=None,
            analysis_hash=analysis_service._analysis_digest(analysis),
            content_hash=analysis.content_hash, input_hash=analysis_service.analysis_input_hash(parent.input),
            selection_hash=retrieval_service._canonical_digest(
                project_ai_config.serialize_shared_embedding(selected)), memory_refs=refs,
            memory_hash=retrieval_service._canonical_digest(refs), outcome="queued", child_task_key=child_key,
        )
        db.add_all([task, receipt])
        await db.flush()
        execution = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", execution.execution_key
        await db.commit()
        ids = [item.id for item in memories]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        job = await kb_index_service.build_index_job(
            db, task=task, execution=execution, cipher=app.state.cipher, factory=create_provider,
            ai_timeout=5, allow_private=True,
        )
    return user, project, ids, execution, job


@pytest.mark.parametrize("damage", ["none", "chain", "origin", "receipt", "parent_input"])
async def test_batch_retry_memory_requires_original_chain_and_completed_analysis(app, client, damage):
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        _, _, _, execution, job = await _owner_job(app, client, url, batch_retry=True)
        async with app.state.sessionmaker() as db:
            receipt = await db.get(AnalysisIndexReceipt, execution.input["_analysis_index_receipt"])
            parent = await db.scalar(select(Task).where(Task.statistics_key == receipt.parent_task_key))
            source = await db.scalar(select(Task).where(Task.statistics_key == receipt.source_task_key))
            if damage == "chain":
                parent.retry_of_id = None
            elif damage == "origin":
                source.kind = "chapter.analyze"
            elif damage == "receipt":
                review = await db.get(SceneReviewAnalysisReceipt, receipt.source_key)
                review.status = "failed"
            elif damage == "parent_input":
                parent.input = {"changed": True}
            await db.commit()
        events = [event async for event in kb_index_service.run_index_job(job, app.state.sessionmaker)]
        if damage == "none":
            assert events[-1][0] == "done" and len(requests) == 1
        else:
            assert events[-1][0] == "error" and requests == []


@pytest.mark.parametrize("change", ["none", "source", "analysis", "token", "config"])
async def test_bound_memory_batch_cannot_reselect_or_send_stale_source(app, client, monkeypatch, change):
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        user, project, memory_ids, _, job = await _owner_job(app, client, url)
        original = collaboration_ai_guard.SendGuard.prepare_provider
        changed = False

        async def after_preparation(guard, *args, **kwargs):
            nonlocal changed
            bound = await original(guard, *args, **kwargs)
            if change != "none" and not changed:
                changed = True
                async with app.state.sessionmaker() as db:
                    if change == "source":
                        memory = await db.get(StoryMemory, memory_ids[0])
                        memory.content = "更新后的记忆"
                        await db.flush()
                        await retrieval_service.upsert_project_sources(db, project["id"], "memory", [
                            memory_service._chunk(memory),
                        ])
                    elif change == "analysis":
                        memory = await db.get(StoryMemory, memory_ids[0])
                        analysis = await db.get(ChapterAnalysis, memory.analysis_id)
                        analysis.summary = "替换后的分析"
                    elif change == "token":
                        (await db.get(User, user["id"])).token_version += 1
                    else:
                        config = await db.scalar(select(AIConfig).where(AIConfig.user_id == user["id"],
                                                                         AIConfig.model_type == "embedding"))
                        config.model = "replacement-model"
                    await db.commit()
            return bound

        monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", after_preparation)
        events = [event async for event in kb_index_service.run_index_job(job, app.state.sessionmaker)]
        if change == "none":
            assert events[-1][0] == "done" and len(requests) == 1
        else:
            assert changed and events[-1][0] == "error" and requests == []
        async with app.state.sessionmaker() as db:
            rows = {row.ref_id: row for row in (await db.scalars(select(RetrievalChunk).where(
                RetrievalChunk.source == "memory", RetrievalChunk.ref_id.in_(memory_ids),
            ))).all()}
            assert rows[memory_ids[1]].embedding == b"other-vector"
            assert rows[memory_ids[1]].embedding_key == "other:model:2"
            if change == "none":
                assert rows[memory_ids[0]].embedding is not None
                assert rows[memory_ids[0]].embedding_key == "openai_compat:old-model:2"
            else:
                assert rows[memory_ids[0]].embedding is None


async def test_bound_memory_without_receipt_cannot_report_done_when_no_vectors_missing(app, client):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        _, _, memory_ids, execution, job = await _owner_job(app, client, url)
        async with app.state.sessionmaker() as db:
            row = await db.scalar(select(RetrievalChunk).where(
                RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_ids[0],
            ))
            row.embedding, row.embedding_key = b"existing-vector", "other:model:2"
            receipt_key = execution.input["_analysis_index_receipt"]
            await db.delete(await db.get(AnalysisIndexReceipt, receipt_key))
            await db.commit()
        events = [event async for event in kb_index_service.run_index_job(job, app.state.sessionmaker)]
        assert events[-1][0] == "error" and requests == []


async def test_bound_memory_job_completes_through_engine(app, client):
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        _, _, memory_ids, execution, _ = await _owner_job(app, client, url)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, execution.task_id)
            task.status = "queued"
            task.execution_key = None
            task.queued_at += timedelta(microseconds=1)
            await db.commit()
        engine = app.state.task_engine
        engine.ctx.factory = create_provider
        engine.ctx.settings.allow_private_ai_endpoints = True
        await engine.start()
        completed = await wait_task(client, (execution.task_id, execution.task_key), within=12)
        assert completed["status"] == "succeeded", completed
        async with app.state.sessionmaker() as db:
            row = await db.scalar(select(RetrievalChunk).where(
                RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_ids[0],
            ))
            assert row.embedding_key == "openai_compat:old-model:2"
        assert len(requests) == 1


async def test_bound_task_retry_never_reselects_current_default(app, client):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        _, project, _, execution, _ = await _owner_job(app, client, url)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, execution.task_id)
            task.status, task.execution_key = "failed", None
            config = await db.scalar(select(AIConfig).where(AIConfig.user_id == execution.actor_id,
                                                             AIConfig.model_type == "embedding"))
            config.model = "新默认模型"
            await db.commit()
        response = await client.post(
            f"/api/v1/tasks/{execution.task_id}/retry", params={"task_key": execution.task_key},
        )
        assert response.status_code == 409, response.text
        async with app.state.sessionmaker() as db:
            tasks = (await db.scalars(select(Task).where(
                Task.project_id == project["id"], Task.kind == "kb.index",
            ))).all()
            assert len(tasks) == 1 and tasks[0].statistics_key == execution.task_key
        assert requests == []


@pytest.mark.parametrize("change", ["source", "token", "config", "new_vector"])
async def test_bound_memory_response_cannot_overwrite_newer_state(app, client, monkeypatch, change):
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        user, project, memory_ids, _, job = await _owner_job(app, client, url)
        original = collaboration_ai_guard.SendGuard.require_task_write
        changed = False

        async def change_before_write(guard, db):
            nonlocal changed
            if not changed:
                changed = True
                async with app.state.sessionmaker() as other:
                    if change == "source":
                        memory = await other.get(StoryMemory, memory_ids[0])
                        memory.content = "响应后更新的内容"
                        await other.flush()
                        await retrieval_service.upsert_project_sources(other, project["id"], "memory", [
                            memory_service._chunk(memory),
                        ])
                    elif change == "token":
                        (await other.get(User, user["id"])).token_version += 1
                    elif change == "config":
                        config = await other.scalar(select(AIConfig).where(
                            AIConfig.user_id == user["id"], AIConfig.model_type == "embedding",
                        ))
                        config.model = "响应后替换的模型"
                    else:
                        row = await other.scalar(select(RetrievalChunk).where(
                            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_ids[0],
                        ))
                        row.embedding, row.embedding_key = b"new-vector", "new:model:2"
                        row.version += 1
                    await other.commit()
            return await original(guard, db)

        monkeypatch.setattr(collaboration_ai_guard.SendGuard, "require_task_write", change_before_write)
        events = [event async for event in kb_index_service.run_index_job(job, app.state.sessionmaker)]
        assert changed and events[-1][0] == "error" and len(requests) == 1
        async with app.state.sessionmaker() as db:
            row = await db.scalar(select(RetrievalChunk).where(
                RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_ids[0],
            ))
            if change == "new_vector":
                assert row.embedding == b"new-vector" and row.embedding_key == "new:model:2"
            else:
                assert row.embedding is None and row.embedding_key is None
