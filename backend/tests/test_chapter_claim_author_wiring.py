"""作者单章与手动复位接线不能让旧执行体清理新占用。"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import AIConfig, Chapter, GenerationLog, KbChunk, KbDocument, RetrievalChunk, Task
from app.models.writing_stats import WritingDailyStat
from app.services import ai_config_service, chapter_claim, generation_service, retrieval_service
from app.services.task_engine.handlers.chapter import ChapterHandler, bind_queued_claim, capture_queued_claim
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register, wait_task, wait_until
from tests.test_chapter_claim_core import _world


async def _run_task(client, response):
    assert response.status_code == 202, response.text
    task = response.json()
    stream = await client.get(f"/api/v1/tasks/{task['id']}/stream", params={"task_key": task["task_key"]})
    assert stream.status_code == 200, stream.text
    events = parse_sse(stream.text)
    return events[-1][1]["task"], events


async def _seed_retrieval(app, project_id, model, dimension):
    async with app.state.sessionmaker() as db:
        document = KbDocument(project_id=project_id, filename="设定.txt", sha256="a" * 64, status="ready")
        db.add(document)
        await db.flush()
        chunk = KbChunk(document_id=document.id, seq=1, content="星舰航行的资料")
        db.add(chunk)
        await db.flush()
        await retrieval_service.upsert_many(db, project_id, "kb", [
            retrieval_service.ChunkInput(ref_id=chunk.id, text=chunk.content),
        ])
        row = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.source == "kb",
                                                            RetrievalChunk.ref_id == chunk.id))
        vector = retrieval_service.normalize_vector([1.0] * dimension)
        await retrieval_service.store_embeddings(db, [row], [vector], f"openai_compat:{model}:{dimension}")
        await db.commit()


async def test_author_binding_rolls_back_with_task_and_captures_persisted_queue(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        task.input = {**task.input, "prev_status": "draft"}
        await db.flush()
        claim = await bind_queued_claim(db, task)
        assert capture_queued_claim(task) == claim
        await db.rollback()
    async with app.state.sessionmaker() as db:
        chapter, task = await db.get(Chapter, identity["chapter_id"]), await db.get(Task, identity["task_id"])
        assert chapter.status == "draft" and chapter.generation_claim_key is None
        assert "_chapter_claim" not in task.input and "prev_status" not in task.input
        task.input = {**task.input, "prev_status": "draft"}
        await db.flush()
        claim = await bind_queued_claim(db, task)
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter, task = await db.get(Chapter, identity["chapter_id"]), await db.get(Task, identity["task_id"])
        assert capture_queued_claim(task) == claim
        assert chapter.generation_claim_key == claim.claim_key


async def test_forged_or_legacy_task_snapshots_cannot_be_captured(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        task.input = {**task.input, "prev_status": "draft"}
        await db.flush()
        with pytest.raises(ConflictError):
            capture_queued_claim(task)  # 补发统计 UUID 不等于绑定过章节占用。
        await bind_queued_claim(db, task)
        original = deepcopy(task.input)
        for pointer in (
            {**original["_chapter_claim"], "extra": "untrusted"},
            {**original["_chapter_claim"], "chapter_id": True},
            {**original["_chapter_claim"], "claim_key": str(uuid4()).upper()},
        ):
            task.input = {**original, "_chapter_claim": pointer}
            with pytest.raises(ConflictError):
                capture_queued_claim(task)
        task.input = {**original, "_collaboration_grant": {}}
        with pytest.raises(ConflictError):
            capture_queued_claim(task)
        await db.rollback()


async def test_author_queue_ignores_client_claim_and_persists_server_identity(app, client):
    await app.state.task_engine.stop()
    owner = await register(client, "chapter-author-queue")
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})).json()
    forged = str(uuid4())
    response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={
        "_chapter_claim": {"claim_key": forged, "prev_status": "done"}, "task_key": forged,
    })
    assert response.status_code == 202, response.text
    assert "_chapter_claim" not in response.text and forged not in response.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, response.json()["id"])
        row = await db.get(Chapter, chapter["id"])
        claim = capture_queued_claim(task)
        assert task.user_id == owner["id"] and task.statistics_key != forged
        assert str(UUID(claim.claim_key)) == claim.claim_key and claim.claim_key != forged
        assert row.status == "generating" and row.generation_claim_key == claim.claim_key


async def test_author_task_commits_output_and_releases_original_claim(app, client, fake_llm):
    owner = await register(client, "chapter-author-task")
    project = await create_project(client)
    await create_ai_config(client)
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/chapters", json={"title": "章节", "content": "原正文"})
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    fake_llm.push("新正文")
    task, _ = await _run_task(client, await client.post(f"/api/v1/chapters/{chapter_id}/generate"))
    assert task["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapter_id)
        row = await db.get(Task, task["id"])
        assert (chapter.status, chapter.content, chapter.generation_claim_key,
                chapter.generation_claim_task_key, chapter.generation_claim_prev_status) == (
                    "done", "新正文", None, None, None,
                )
        assert "_chapter_claim" not in row.input
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == row.id))
        assert (log.model_source, log.model_config_owner_id, log.actor_user_id) == ("actor", owner["id"], owner["id"])


async def test_author_model_change_blocks_automatic_continuation(app, client, fake_llm):
    await register(client, "chapter-author-model-change")
    project = await create_project(client)
    config = await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "章节", "content": "原正文",
    })).json()
    gate = Gate()
    fake_llm.push(["首轮", gate])
    created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={
        "target_words": 300, "auto_continue": True,
    })
    assert created.status_code == 202, created.text
    await gate.wait_reached()
    changed = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "new-model"})
    assert changed.status_code == 200, changed.text
    gate.release()
    task = await wait_task(client, created.json(), within=10)
    assert task["status"] == "failed" and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        saved = await db.get(Chapter, chapter["id"])
        assert (saved.status, saved.content, saved.generation_claim_key) == ("draft", "原正文", None)


async def test_mutated_transport_cannot_send_automatic_continuation(app, client, fake_llm):
    await register(client, "chapter-author-transport-change")
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})).json()
    gate = Gate()
    fake_llm.push(["首轮", gate])
    created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={
        "target_words": 300, "auto_continue": True,
    })
    assert created.status_code == 202, created.text
    await gate.wait_reached()
    for provider in fake_llm._providers:
        provider.api_key = "mutated-key"
    gate.release()
    task = await wait_task(client, created.json(), within=10)
    assert task["status"] == "failed" and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        saved = await db.get(Chapter, chapter["id"])
        assert (saved.status, saved.content, saved.generation_claim_key) == ("draft", "", None)


async def test_author_embedding_uses_bound_chapter_send_guard(app, client, fake_llm):
    await register(client, "chapter-author-embedding")
    project = await create_project(client)
    await create_ai_config(client)
    await create_ai_config(client, name="向量模型", model="embedding-test", model_type="embedding", is_default=True)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "星舰启航"})).json()
    await _seed_retrieval(app, project["id"], "embedding-test", fake_llm.embed_dim)
    fake_llm.push("正文")
    task, _ = await _run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/generate"))
    assert task["status"] == "succeeded" and len(fake_llm.embed_calls) >= 1
    assert "星舰航行的资料" in fake_llm.calls[0]["messages"][-1]["content"]


async def test_embedding_default_aba_cannot_mix_model_snapshot_and_credentials(app, client, fake_llm, monkeypatch):
    await register(client, "chapter-author-embedding-aba")
    project = await create_project(client)
    await create_ai_config(client)
    first = await create_ai_config(client, name="向量 A", base_url="https://a.example.test/v1/", api_key="vector-a",
                                   model="same-model", model_type="embedding", is_default=True)
    second = await create_ai_config(client, name="向量 B", base_url="https://b.example.test/v1/", api_key="vector-b",
                                    model="same-model", model_type="embedding")
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "星舰启航"})).json()
    await _seed_retrieval(app, project["id"], "same-model", fake_llm.embed_dim)
    build = ai_config_service.build_provider
    switched, seen = False, []

    async def set_default(config_id):
        async with app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id.in_([first["id"], second["id"]])).values(
                is_default=False,
            ))
            await db.execute(update(AIConfig).where(AIConfig.id == config_id).values(is_default=True))
            await db.commit()

    async def build_then_restore(config, *args, **kwargs):
        nonlocal switched
        if config.model_type == "embedding":
            # 任务已在受理时冻结 A；真正构造 provider 时默认值短暂切换到 B。
            switched = True
            await set_default(second["id"])
        provider = await build(config, *args, **kwargs)
        if config.model_type == "embedding":
            seen.append((provider.api_key, provider.base_url))
            await set_default(first["id"])
        return provider

    monkeypatch.setattr(ai_config_service, "build_provider", build_then_restore)
    fake_llm.push("正文")
    created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate")
    assert created.status_code == 202, created.text
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, created.json()["id"])
        assert original.input["_shared_embedding"]["model"]["config_id"] == first["id"]
    task, _ = await _run_task(client, created)
    assert task["status"] == "succeeded" and switched and fake_llm.embed_calls
    assert seen == [("vector-a", "https://a.example.test/v1")]
    assert all(provider.api_key != "vector-b" for provider in fake_llm._providers)


async def test_invalid_configured_embedding_fails_closed(app, client, fake_llm):
    await register(client, "chapter-author-invalid-embedding")
    project = await create_project(client)
    await create_ai_config(client)
    embedding = await create_ai_config(client, name="损坏向量", model="embedding-test", model_type="embedding")
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})).json()
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == embedding["id"]).values(api_key_enc="invalid"))
        await db.commit()
    created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate")
    assert created.status_code == 202, created.text
    task = await wait_task(client, created.json(), within=10)
    assert task["status"] == "failed" and not fake_llm.calls and not fake_llm.embed_calls
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, chapter["id"])
        assert row.status == "draft" and row.generation_claim_key is None


async def test_lost_commit_confirmation_recovers_exact_completed_result(app, client, fake_llm, monkeypatch):
    await register(client, "chapter-author-commit-unknown")
    project = await create_project(client)
    await create_ai_config(client)
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/chapters", json={"title": "章节", "content": "原正文"})
    chapter_id = response.json()["id"]
    fake_llm.push("只提交一次的新正文")
    commit = AsyncSession.commit
    write_receipt = generation_service._write_completion_receipt
    target = None
    lost = False

    async def mark_result_transaction(db, *args, **kwargs):
        nonlocal target
        receipt = await write_receipt(db, *args, **kwargs)
        target = db
        return receipt

    async def lose_confirmation(db):
        nonlocal lost
        if db is target and not lost:
            lost = True
            await commit(db)
            raise ConnectionError("模拟正文提交确认丢失")
        return await commit(db)

    monkeypatch.setattr(generation_service, "_write_completion_receipt", mark_result_transaction)
    monkeypatch.setattr(AsyncSession, "commit", lose_confirmation)
    task, events = await _run_task(client, await client.post(f"/api/v1/chapters/{chapter_id}/generate"))
    assert lost and task["status"] == "succeeded" and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapter_id)
        row = await db.get(Task, task["id"])
        receipt = row.checkpoint["_chapter_claim"]
        assert receipt["phase"] == "completed" and receipt["task_key"] == row.statistics_key
        assert chapter.content == "只提交一次的新正文" and chapter.generation_claim_key is None
        ai_words = await db.scalar(select(func.sum(WritingDailyStat.ai_words)).where(
            WritingDailyStat.user_id == row.user_id, WritingDailyStat.project_id == project["id"],
        ))
        assert ai_words == len("只提交一次的新")
    response = await client.get(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
    assert "_chapter_claim" not in response.text and receipt["claim_key"] not in response.text
    assert "_chapter_claim" not in repr(events) and receipt["claim_key"] not in repr(events)


async def test_unverifiable_committed_result_preserves_running_on_recovery(app, client, fake_llm, monkeypatch):
    await register(client, "chapter-author-unconfirmed")
    project = await create_project(client)
    await create_ai_config(client)
    chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
    chapter_id = chapter.json()["id"]
    fake_llm.push("已落库但确认不可用")
    commit, write_receipt = AsyncSession.commit, generation_service._write_completion_receipt
    verify_receipt = generation_service.completed_chapter_receipt
    target = None
    lost = False

    async def mark_result_transaction(db, *args, **kwargs):
        nonlocal target
        receipt = await write_receipt(db, *args, **kwargs)
        target = db
        return receipt

    async def lose_confirmation(db):
        nonlocal lost
        if db is target and not lost:
            lost = True
            await commit(db)
            raise ConnectionError("模拟确认丢失")
        return await commit(db)

    async def proof_unavailable(_db, _claim):
        return None

    monkeypatch.setattr(generation_service, "_write_completion_receipt", mark_result_transaction)
    monkeypatch.setattr(generation_service, "completed_chapter_receipt", proof_unavailable)
    monkeypatch.setattr(AsyncSession, "commit", lose_confirmation)
    created = await client.post(f"/api/v1/chapters/{chapter_id}/generate")
    assert created.status_code == 202, created.text
    original = created.json()
    stream = await client.get(f"/api/v1/tasks/{original['id']}/stream", params={"task_key": original["task_key"]})
    assert stream.status_code == 200
    events = parse_sse(stream.text)
    assert events[-1][0] == "error" and "尚未确认" in events[-1][1]["detail"]
    await wait_until(lambda: original["id"] not in app.state.task_engine._running)
    await app.state.task_engine.stop()
    await app.state.task_engine.start()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, original["id"])
        saved = await db.get(Chapter, chapter_id)
        assert task.status == "running" and task.execution_key is not None
        receipt = deepcopy(task.checkpoint["_chapter_claim"])
        assert receipt["phase"] == "completed"
        assert saved.content == "已落库但确认不可用" and saved.status == "done"
    claim = chapter_claim.ChapterClaim(
        task_id=receipt["task_id"], task_key=receipt["task_key"], actor_id=receipt["actor_id"],
        project_id=receipt["project_id"], chapter_id=receipt["chapter_id"],
        queued_at=datetime.fromisoformat(receipt["queued_at"]), execution_key=receipt["execution_key"],
        claim_key=receipt["claim_key"], prev_status=receipt["prev_status"], grant_identity=None,
        phase="running", kind="single",
    )
    monkeypatch.setattr(generation_service, "completed_chapter_receipt", verify_receipt)
    async with app.state.sessionmaker() as db:
        assert await verify_receipt(db, claim) == receipt
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, original["id"])
        # Python 认为 True == 1，用 SQL 写入以模拟真实 JSON 损坏。
        await db.execute(update(Task).where(Task.id == task.id).values(
            checkpoint={**task.checkpoint, "_chapter_claim": {**receipt, "schema": True}},
        ))
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await verify_receipt(db, claim) is None  # bool 不能伪装版本号 1。
    await app.state.task_engine.recover()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, original["id"])).status == "running"
    retry = await client.post(f"/api/v1/tasks/{original['id']}/retry", params={"task_key": original["task_key"]})
    assert retry.status_code == 409 and len(fake_llm.calls) == 1 and lost


async def test_author_reset_then_old_abort_preserves_new_batch_claim(app, client, make_client):
    identity, _ = await _world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        task.input = {**task.input, "prev_status": "draft"}
        await db.flush()
        queued = await chapter_claim.claim_queued_chapter(db, **identity)
        await db.commit()
    execution_key = str(uuid4())
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="running", execution_key=execution_key,
        ))
        old_claim = await chapter_claim.activate_queued_chapter(db, queued, execution_key=execution_key)
        await db.commit()
        old_task = await db.get(Task, identity["task_id"])
    response = await client.patch(
        f"/api/v1/projects/{identity['project_id']}/chapters/{identity['chapter_id']}", json={"status": "done"},
    )
    assert response.status_code == 200, response.text

    new_round = datetime(2026, 4, 1, tzinfo=UTC)
    new_execution = str(uuid4())
    async with app.state.sessionmaker() as db:
        task = Task(user_id=identity["actor_id"], project_id=identity["project_id"], kind="chapter.batch",
                    status="running", resource_key=f"batch:{identity['project_id']}", queued_at=new_round,
                    execution_key=new_execution, input={"request": {"project_id": identity["project_id"]},
                    "chapter_ids": [identity["chapter_id"]]}, checkpoint={"current": None})
        db.add(task)
        await db.flush()
        new_claim = await chapter_claim.claim_batch_chapter(
            db, task_id=task.id, task_key=task.statistics_key, actor_id=identity["actor_id"],
            project_id=identity["project_id"], chapter_id=identity["chapter_id"], queued_at=new_round,
            execution_key=new_execution, grant_identity=None,
        )
        await db.commit()
    await ChapterHandler().on_abort(app.state.task_engine.ctx, old_task, claim=old_claim)
    stale_job = generation_service.ChapterJob(
        chapter_id=identity["chapter_id"], prev_status="draft", provider=None, model="test", messages=[],
        project_id=identity["project_id"], user_id=identity["actor_id"], chapter_claim=old_claim,
    )
    with pytest.raises(ConflictError):
        await generation_service._save_chapter_result(stale_job, app.state.sessionmaker, {}, {})
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert (chapter.status, chapter.content, chapter.generation_claim_key) == (
            "generating", "不应被旧清理修改", new_claim.claim_key,
        )
