"""分析派生索引在 PostgreSQL 作品屏障内的双连接去重。"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.models import AIConfig, Chapter, SceneReviewAnalysisReceipt, StoryMemory, Task, User
from app.services import (
    analysis_service,
    kb_index_service,
    memory_service,
    project_ai_config,
    retrieval_service,
    task_statistics_service,
)
from tests.helpers import create_project, register
from tests.test_analysis_memory_enqueue_fencing import (
    _completed_batch,
    _enqueue,
    _finish_analysis_with_receipt,
)
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps  # noqa: F401


@pytest.fixture(autouse=True)
def _bound_memory_ready(monkeypatch):
    # 与 SQLite 收据式受理用例一致：生产链路仍由门控关闭，这里直接验证受理接口。
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)


async def _seed(app):
    """与 test_analysis_memory_enqueue_fencing.analysis_index_world 相同：冻结模型/向量绑定与原 token 版本。"""
    factory = app.state.sessionmaker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        actor = await register(client, "pg-analysis-index-owner")
        project = await create_project(client)
    async with factory() as db:
        chapter = Chapter(project_id=project["id"], chapter_no=1, title="章节", content="正文")
        memory = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", content="记忆")
        config = AIConfig(user_id=actor["id"], name="向量", provider="openai_compat", model="embed",
                          base_url="https://embed.example.test/v1", model_type="embedding", is_default=True,
                          api_key_enc=app.state.cipher.encrypt("pg-index-test-key"))
        text_config = AIConfig(user_id=actor["id"], name="文本", provider="openai_compat", model="chat",
                               base_url="https://chat.example.test/v1", model_type="text", is_default=True,
                               api_key_enc=app.state.cipher.encrypt("pg-text-test-key"))
        db.add_all([chapter, memory, config, text_config])
        await db.flush()
        binding = await project_ai_config.resolve_project_model(
            db, actor["id"], project["id"], "text", cipher=app.state.cipher, config_id=text_config.id,
        )
        selection = await project_ai_config.resolve_shared_embedding(
            db, actor["id"], project["id"], cipher=app.state.cipher,
        )
        token_version = (await db.get(User, actor["id"])).token_version
        task = Task(user_id=actor["id"], project_id=project["id"], chapter_id=chapter.id,
                    kind="chapter.analyze", status="running", execution_key=str(uuid4()),
                    input={"request": {"chapter_id": chapter.id}, "_task_actor_token_version": token_version,
                           "_project_model": project_ai_config.serialize_project_model(binding),
                           "_shared_embedding": project_ai_config.serialize_shared_embedding(selection)})
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        await db.refresh(task)
        execution = analysis_service.capture_analysis_execution(task)
        await _finish_analysis_with_receipt(db, task, execution)
    async with factory() as db:
        task = await db.get(Task, execution.task_id)
        memory = await db.get(StoryMemory, memory.id)
        memory.chapter_id = execution.task_chapter_id
        memory.analysis_id = task.checkpoint["_analysis_completion"]["analysis_id"]
        memory.origin = "auto"
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project["id"], "memory", [memory_service._chunk(memory)])
        await db.commit()
    return execution


async def test_postgresql_analysis_enqueue_waits_then_deduplicates(postgres_collaboration_apps):  # noqa: F811
    app, _ = postgres_collaboration_apps
    factory = app.state.sessionmaker
    execution = await _seed(app)

    entered = asyncio.Event()

    async def second():
        async with factory() as db:
            entered.set()
            child = await _enqueue(app, db, execution)
            await db.commit()
            return child.state

    async with factory() as first:
        first_id = (await _enqueue(app, first, execution)).task_id
        assert first_id
        contender = asyncio.create_task(second())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(0.1)
            assert not contender.done()
            await first.commit()
            assert await asyncio.wait_for(contender, 5) == "existing"
        finally:
            await first.rollback()
            if not contender.done():
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
    async with factory() as db:
        rows = list(await db.scalars(select(Task).where(Task.kind == "kb.index")))
        assert len(rows) == 1 and (rows[0].id, rows[0].user_id, rows[0].project_id) == (
            first_id, execution.actor_id, execution.project_id,
        )


async def test_postgresql_batch_receipt_is_locked_until_enqueue_commits(postgres_collaboration_apps):  # noqa: F811
    app, _ = postgres_collaboration_apps
    factory = app.state.sessionmaker
    original = await _seed(app)
    execution, reference, _ = await _completed_batch(app, original, retry=True)
    entered = asyncio.Event()

    async def other():
        async with factory() as db:
            entered.set()
            await db.execute(update(SceneReviewAnalysisReceipt).where(
                SceneReviewAnalysisReceipt.run_key == reference.run_key,
            ).values(status="failed"))
            await db.commit()

    async with factory() as first:
        child_id = (await _enqueue(app, first, execution, reference=reference)).task_id
        assert child_id
        contender = asyncio.create_task(other())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(0.1)
            assert not contender.done()
            await first.commit()
            await asyncio.wait_for(contender, 5)
        finally:
            await first.rollback()
            if not contender.done():
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
    async with factory() as db:
        child = await db.get(Task, child_id)
        receipt = await db.get(SceneReviewAnalysisReceipt, reference.run_key)
        assert child.user_id == execution.actor_id and child.project_id == execution.project_id
        assert receipt.status == "failed"
