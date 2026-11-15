"""分析派生记忆索引必须绑定原 actor、任务身份与实际执行轮次。"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import BadRequestError, UnauthorizedError
from app.models import (
    AIConfig,
    AnalysisIndexReceipt,
    Chapter,
    ChapterAnalysis,
    Project,
    RetrievalChunk,
    SceneReviewAnalysisReceipt,
    StoryMemory,
    Task,
    TaskStatistic,
    User,
)
from app.services import (
    analysis_service,
    kb_index_service,
    memory_service,
    project_access,
    project_ai_config,
    retrieval_service,
    task_statistics_service,
)
from tests.helpers import create_project, register


@pytest.fixture(autouse=True)
def _bound_memory_ready(monkeypatch):
    # 生产链路仍由门控关闭；此处与 test_analysis_index_receipt_enqueue_e6 一样直接验证收据式受理接口。
    monkeypatch.setattr(kb_index_service, "_BOUND_MEMORY_READY", True)


async def _enqueue(app, db, execution, *, reference=None, attempt=None):
    return await kb_index_service.enqueue_memory_for_analysis(
        db, execution, attempt=attempt or kb_index_service.new_analysis_index_attempt(), reference=reference,
        cipher=app.state.cipher, allow_private=False,
    )


_BATCH_PENDING = ("待实现：chapter.batch 派生索引尚未接入收据式受理"
                  "（enqueue_memory_for_analysis 目前只接受 chapter.analyze）")
_BINDING_KEYS = ("_task_actor_token_version", "_project_model", "_shared_embedding")


async def _bound_input(db, execution, request):
    """批量任务沿用原 actor 的冻结绑定；新设计不接受缺证输入。"""
    source = await db.get(Task, execution.task_id)
    return {"request": request, **{key: source.input[key] for key in _BINDING_KEYS}}


async def _finish_analysis_with_receipt(db, task, execution):
    await project_access.require_project(db, execution.actor_id, execution.project_id, "content.edit", for_update=True,
                                         visibility_write=True)
    pending = analysis_service.AnalysisPending(execution, str(uuid4()))
    task.checkpoint = {"_analysis_pending": analysis_service._pending_document(pending)}
    analysis = ChapterAnalysis(project_id=execution.project_id, chapter_id=execution.task_chapter_id,
                               task_id=execution.task_id, chapter_no=1, content_hash="a" * 64, summary="分析摘要")
    db.add(analysis)
    await db.flush()
    result = {
        "analysis_id": analysis.id, "chapter_id": analysis.chapter_id, "chapter_no": analysis.chapter_no,
        "summary": analysis.summary, "scores": analysis.scores, "metrics": analysis.metrics,
        "warnings": analysis.warnings, "change_counts": {},
    }
    await analysis_service._write_analysis_completion(db, pending, analysis, result)
    task.status, task.result = "succeeded", result
    await db.commit()


@pytest.fixture
async def analysis_index_world(app, client):
    await app.state.task_engine.stop()
    user = await register(client, "analysis-index-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=project["id"], chapter_no=1, title="章节", content="正文")
        memory = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", content="真实记忆")
        config = AIConfig(user_id=user["id"], name="向量", provider="openai_compat", model="embed",
                          base_url="https://embed.example.test/v1", model_type="embedding", is_default=True,
                          api_key_enc=app.state.cipher.encrypt("index-test-key"))
        text_config = AIConfig(user_id=user["id"], name="文本", provider="openai_compat", model="chat",
                               base_url="https://chat.example.test/v1", model_type="text", is_default=True,
                               api_key_enc=app.state.cipher.encrypt("text-test-key"))
        db.add_all([chapter, memory, config, text_config])
        await db.flush()
        # 分析任务只信任受理时冻结的模型 / 向量绑定与 actor token 版本。
        binding = await project_ai_config.resolve_project_model(
            db, user["id"], project["id"], "text", cipher=app.state.cipher, config_id=text_config.id,
        )
        selection = await project_ai_config.resolve_shared_embedding(
            db, user["id"], project["id"], cipher=app.state.cipher,
        )
        token_version = (await db.get(User, user["id"])).token_version
        task = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter.id,
                    kind="chapter.analyze", status="running", execution_key=str(uuid4()),
                    input={"request": {"chapter_id": chapter.id}, "_task_actor_token_version": token_version,
                           "_project_model": project_ai_config.serialize_project_model(binding),
                           "_shared_embedding": project_ai_config.serialize_shared_embedding(selection)})
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        await db.refresh(task)
        identity = analysis_service.capture_analysis_execution(task)
        await _finish_analysis_with_receipt(db, task, identity)
    async with app.state.sessionmaker() as db:
        # 收据式索引只覆盖本次分析派生的记忆，记忆与检索块按真实写入路径关联。
        task = await db.get(Task, identity.task_id)
        memory = await db.get(StoryMemory, memory.id)
        memory.chapter_id = identity.task_chapter_id
        memory.analysis_id = task.checkpoint["_analysis_completion"]["analysis_id"]
        memory.origin = "auto"
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project["id"], "memory", [memory_service._chunk(memory)])
        await db.commit()
        return identity, memory.id


async def _children(db):
    return list(await db.scalars(select(Task).where(Task.kind == "kb.index").order_by(Task.id)))


async def test_standalone_without_completion_receipt_cannot_enqueue(app, analysis_index_world):
    execution, _ = analysis_index_world
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.checkpoint, task.result = None, None
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        await db.rollback()
        assert await _children(db) == []


async def test_batch_without_reference_or_completed_receipt_cannot_enqueue(app, analysis_index_world):
    owner, _ = analysis_index_world
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner.actor_id, project_id=owner.project_id, kind="chapter.batch", status="running",
                    execution_key=str(uuid4()),
                    input=await _bound_input(db, owner, {"project_id": owner.project_id}))
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        await db.refresh(task)
        execution = analysis_service.capture_analysis_execution(task)
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        await db.rollback()
        assert await _children(db) == []


async def test_analysis_enqueue_is_same_transaction_and_uses_original_actor(app, analysis_index_world):
    execution, _ = analysis_index_world
    async with app.state.sessionmaker() as db:
        admission = await _enqueue(app, db, execution)
        assert admission.state == "created"
        child_id = admission.task_id
        children = await _children(db)
        assert child_id and len(children) == 1
        child = children[0]
        assert (child.id, child.user_id, child.project_id, child.kind, child.status) == (
            child_id, execution.actor_id, execution.project_id, "kb.index", "queued",
        )
        assert child.input["request"] == {"project_id": execution.project_id, "mode": "memory"}
        assert await db.get(TaskStatistic, child.statistics_key) is not None
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await _children(db) == []
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(TaskStatistic.kind == "kb.index")) == 0


@pytest.mark.parametrize("changed", ["task_key", "execution_key", "queued_at", "actor", "project", "chapter", "grant"])
async def test_analysis_enqueue_rejects_changed_original_identity(app, analysis_index_world, changed):
    execution, _ = analysis_index_world
    value = {
        "task_key": {"task_key": str(uuid4())},
        "execution_key": {"execution_key": str(uuid4())},
        "queued_at": {"queued_at": execution.queued_at + timedelta(microseconds=1)},
        "actor": {"actor_id": execution.actor_id + 1},
        "project": {"project_id": execution.project_id + 1},
        "chapter": {"task_chapter_id": execution.task_chapter_id + 1},
        "grant": {"grant_identity": (str(uuid4()), 1)},
    }[changed]
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, replace(execution, **value))
        await db.rollback()
        assert await _children(db) == []


@pytest.mark.parametrize("change", ["running", "execution_cleared", "admin_cancel", "user_disabled", "deleting"])
async def test_analysis_enqueue_rejects_lost_execution_or_authorization(app, analysis_index_world, change):
    execution, _ = analysis_index_world
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        if change == "running":
            task.status = "running"
        elif change == "execution_cleared":
            task.execution_key = None
        elif change == "admin_cancel":
            task.admin_cancel_operation_id = str(uuid4())
        elif change == "user_disabled":
            (await db.get(User, execution.actor_id)).is_active = False
        else:
            project = await db.get(Project, execution.project_id)
            project.deletion_started_at, project.deletion_token = execution.queued_at, str(uuid4())
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        await db.rollback()
        assert await _children(db) == []


async def test_batch_running_may_enqueue_once_and_deduplicates(app, analysis_index_world):
    original, _ = analysis_index_world
    execution, reference, _ = await _completed_batch(app, original)
    async with app.state.sessionmaker() as db:
        first = await _enqueue(app, db, execution, reference=reference)
        child_id = first.task_id
        assert first.state == "created" and child_id
        assert (await _enqueue(app, db, execution, reference=reference)).state == "existing"
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert [row.id for row in await _children(db)] == [child_id]


@pytest.mark.parametrize("token_changed", [False, True])
async def test_batch_retry_reuses_original_receipt_and_cannot_renew_old_authorization(
    app, analysis_index_world, token_changed,
):
    original, _ = analysis_index_world
    execution, reference, _ = await _completed_batch(app, original)
    async with app.state.sessionmaker() as db:
        first = await _enqueue(app, db, execution, reference=reference)
        assert first.state == "created" and first.task_id
        await db.commit()
    async with app.state.sessionmaker() as db:
        old = await db.get(Task, execution.task_id)
        old.status, old.execution_key = "interrupted", None
        retry_input = dict(old.input)
        if token_changed:
            user = await db.get(User, execution.actor_id)
            user.token_version += 1
            retry_input["_task_actor_token_version"] = user.token_version
        retry = Task(user_id=old.user_id, project_id=old.project_id, kind="chapter.batch", status="running",
                     execution_key=str(uuid4()), retry_of_id=old.id, input=retry_input,
                     checkpoint=dict(old.checkpoint))
        db.add(retry)
        await task_statistics_service.record_created(db, retry)
        await db.commit()
        await db.refresh(retry)
        retried = analysis_service.capture_analysis_execution(retry)
    assert retried.task_key != execution.task_key
    async with app.state.sessionmaker() as db:
        if token_changed:
            # 原收据绑定旧 token 版本：重试只能失败关闭，不能以新会话补签或另建子任务。
            with pytest.raises(analysis_service.AnalysisExecutionChanged):
                await _enqueue(app, db, retried, reference=reference)
            await db.rollback()
        else:
            again = await _enqueue(app, db, retried, reference=reference)
            assert again.state == "existing" and again.receipt_key == first.receipt_key
            await db.commit()
        # 旧执行在被重试取代后同样不能再受理。
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution, reference=reference)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert [row.id for row in await _children(db)] == [first.task_id]
        assert await db.scalar(select(func.count(AnalysisIndexReceipt.receipt_key))) == 1


async def _completed_batch(app, original, *, retry=False):
    async with app.state.sessionmaker() as db:
        source = None
        if retry:
            source = Task(user_id=original.actor_id, project_id=original.project_id,
                          kind="chapter.batch", status="interrupted", input={})
            db.add(source)
            await task_statistics_service.record_created(db, source)
        batch = Task(user_id=original.actor_id, project_id=original.project_id, kind="chapter.batch", status="running",
                     execution_key=str(uuid4()), retry_of_id=source.id if source is not None else None,
                     input=await _bound_input(db, original, {"project_id": original.project_id}))
        db.add(batch)
        await task_statistics_service.record_created(db, batch)
        await db.flush()
        ref = {"run_key": str(uuid4()),
               "task_key": source.statistics_key if source is not None else batch.statistics_key,
               "chapter_id": original.task_chapter_id, "content_hash": "b" * 64}
        if source is not None:
            source.checkpoint = {"phase": "analyze", "items": [{"chapter_id": original.task_chapter_id}],
                                 "rich_analysis": ref}
        batch.checkpoint = {"phase": "analyze", "items": [{"chapter_id": original.task_chapter_id}],
                            "rich_analysis": ref}
        analysis = ChapterAnalysis(project_id=original.project_id, chapter_id=original.task_chapter_id,
                                   task_id=source.id if source is not None else batch.id, chapter_no=1,
                                   content_hash=ref["content_hash"])
        db.add(analysis)
        await db.flush()
        db.add(SceneReviewAnalysisReceipt(run_key=ref["run_key"], task_key=ref["task_key"],
                                         chapter_id=ref["chapter_id"], content_hash=ref["content_hash"],
                                         analysis_id=analysis.id, status="completed"))
        # 真实批量分析与结果同事务写入本次分析的自动记忆，检索块尚无向量。
        memory = StoryMemory(project_id=original.project_id, chapter_id=original.task_chapter_id, chapter_no=1,
                             kind="plot_point", origin="auto", analysis_id=analysis.id, content="批量分析记忆")
        db.add(memory)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, original.project_id, "memory",
                                                       [memory_service._chunk(memory)])
        await db.commit()
        await db.refresh(batch)
        execution = analysis_service.capture_analysis_execution(batch)
        return execution, analysis_service.capture_analysis_reference(ref), analysis.id


@pytest.mark.parametrize("damage", ["missing", "pending", "hash", "result", "analysis_removed"])
async def test_standalone_rejects_damaged_completion_receipt(app, analysis_index_world, damage):
    execution, _ = analysis_index_world
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        checkpoint = dict(task.checkpoint)
        if damage == "missing":
            checkpoint.pop("_analysis_completion")
        elif damage == "pending":
            checkpoint["_analysis_pending"] = {"nonce": str(uuid4())}
        elif damage == "hash":
            checkpoint["_analysis_completion"] = {**checkpoint["_analysis_completion"], "analysis_hash": "0" * 64}
        elif damage == "result":
            task.result = {**task.result, "summary": "另一个结果"}
        else:
            await db.delete(await db.get(ChapterAnalysis, checkpoint["_analysis_completion"]["analysis_id"]))
        task.checkpoint = checkpoint
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        await db.rollback()
        assert await _children(db) == []


@pytest.mark.parametrize("damage", ["cursor", "phase", "item", "reference", "status", "hash", "analysis_removed"])
async def test_batch_rejects_changed_cursor_or_completed_receipt(app, analysis_index_world, damage):
    original, _ = analysis_index_world
    execution, reference, analysis_id = await _completed_batch(app, original)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        receipt = await db.get(SceneReviewAnalysisReceipt, reference.run_key)
        if damage == "cursor":
            task.step_cursor = execution.batch_cursor + 1
        elif damage == "phase":
            task.checkpoint = {**task.checkpoint, "phase": "generate"}
        elif damage == "item":
            task.checkpoint = {**task.checkpoint, "items": [{"chapter_id": reference.chapter_id + 1}]}
        elif damage == "reference":
            reference = replace(reference, run_key=str(uuid4()))
        elif damage == "status":
            receipt.status = "running"
        elif damage == "hash":
            (await db.get(ChapterAnalysis, analysis_id)).content_hash = "c" * 64
        else:
            await db.delete(await db.get(ChapterAnalysis, analysis_id))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution, reference=reference)
        await db.rollback()
        assert await _children(db) == []


async def test_batch_retry_accepts_completed_receipt_from_proven_old_task(app, analysis_index_world):
    original, _ = analysis_index_world
    execution, reference, _ = await _completed_batch(app, original, retry=True)
    assert reference.task_key != execution.task_key
    async with app.state.sessionmaker() as db:
        child_id = (await _enqueue(app, db, execution, reference=reference)).task_id
        assert child_id
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert [task.id for task in await _children(db)] == [child_id]


async def test_batch_retry_rejects_deleted_reference_source_even_when_analysis_is_current(app, analysis_index_world):
    original, _ = analysis_index_world
    execution, reference, analysis_id = await _completed_batch(app, original, retry=True)
    async with app.state.sessionmaker() as db:
        source = await db.scalar(select(Task).where(Task.statistics_key == reference.task_key))
        (await db.get(ChapterAnalysis, analysis_id)).task_id = execution.task_id
        await db.delete(source)
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution, reference=reference)
        await db.rollback()
        assert await _children(db) == []


async def test_batch_retry_accepts_current_analysis_with_proven_original_source(app, analysis_index_world):
    original, _ = analysis_index_world
    execution, reference, analysis_id = await _completed_batch(app, original, retry=True)
    async with app.state.sessionmaker() as db:
        (await db.get(ChapterAnalysis, analysis_id)).task_id = execution.task_id
        await db.commit()
    async with app.state.sessionmaker() as db:
        child_id = (await _enqueue(app, db, execution, reference=reference)).task_id
        assert child_id
        await db.commit()


async def test_batch_multiple_retries_accept_proven_intermediate_analysis(app, analysis_index_world):
    original, _ = analysis_index_world
    execution, reference, analysis_id = await _completed_batch(app, original, retry=True)
    async with app.state.sessionmaker() as db:
        source = await db.scalar(select(Task).where(Task.statistics_key == reference.task_key))
        middle = Task(user_id=execution.actor_id, project_id=execution.project_id, kind="chapter.batch",
                      status="interrupted", retry_of_id=source.id,
                      input={"request": {"project_id": execution.project_id}},
                      checkpoint={"phase": "analyze", "items": [{"chapter_id": reference.chapter_id}],
                                  "rich_analysis": reference.as_dict()})
        db.add(middle)
        await task_statistics_service.record_created(db, middle)
        (await db.get(Task, execution.task_id)).retry_of_id = middle.id
        (await db.get(ChapterAnalysis, analysis_id)).task_id = middle.id
        await db.commit()
    async with app.state.sessionmaker() as db:
        child_id = (await _enqueue(app, db, execution, reference=reference)).task_id
        assert child_id
        await db.commit()


async def test_batch_retry_rejects_unrelated_same_owner_source(app, analysis_index_world):
    original, _ = analysis_index_world
    execution, reference, analysis_id = await _completed_batch(app, original, retry=True)
    async with app.state.sessionmaker() as db:
        other = Task(user_id=execution.actor_id, project_id=execution.project_id, kind="chapter.batch",
                     status="interrupted", input={})
        db.add(other)
        await task_statistics_service.record_created(db, other)
        reference = replace(reference, task_key=other.statistics_key)
        task = await db.get(Task, execution.task_id)
        task.checkpoint = {**task.checkpoint, "rich_analysis": reference.as_dict()}
        other.checkpoint = {"phase": "analyze", "items": [{"chapter_id": reference.chapter_id}],
                            "rich_analysis": reference.as_dict()}
        receipt = await db.get(SceneReviewAnalysisReceipt, reference.run_key)
        receipt.task_key = reference.task_key
        (await db.get(ChapterAnalysis, analysis_id)).task_id = execution.task_id
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution, reference=reference)
        await db.rollback()
        assert await _children(db) == []


@pytest.mark.parametrize("pending_kind", ["new", "dirty", "deleted"])
async def test_analysis_enqueue_never_flushes_pending_caller_objects(app, analysis_index_world, pending_kind):
    execution, memory_id = analysis_index_world
    async with app.state.sessionmaker() as db:
        if pending_kind == "new":
            pending = StoryMemory(project_id=execution.project_id, chapter_no=1, kind="manual", content="调用方未提交")
            db.add(pending)
        else:
            pending = await db.get(StoryMemory, memory_id)
            if pending_kind == "dirty":
                pending.content = "调用方未提交"
            else:
                await db.delete(pending)
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        if pending_kind == "new":
            assert pending in db.new and pending.id is None
        elif pending_kind == "dirty":
            assert pending in db.dirty and pending.content == "调用方未提交"
        else:
            assert pending in db.deleted
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(StoryMemory.id)).where(StoryMemory.content == "调用方未提交")) == 0
        assert await _children(db) == []


async def test_analysis_enqueue_rechecks_pending_objects_before_savepoint(app, analysis_index_world, monkeypatch):
    execution, _ = analysis_index_world
    original = retrieval_service.snapshot_embedding_inputs
    captured = []

    async def add_pending(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        pending = StoryMemory(project_id=execution.project_id, chapter_no=1, kind="manual", content="锁内待写")
        db.add(pending)
        captured.append(pending)
        return result

    monkeypatch.setattr(retrieval_service, "snapshot_embedding_inputs", add_pending)
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        assert len(captured) == 1 and captured[0].id is None and captured[0] in db.new
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(StoryMemory.id)).where(StoryMemory.content == "锁内待写")) == 0
        assert await _children(db) == []


async def test_reused_integer_task_id_cannot_enqueue_for_old_execution(app, analysis_index_world):
    original, _ = analysis_index_world
    async with app.state.sessionmaker() as db:
        old = await db.get(Task, original.task_id)
        await db.delete(old)
        await db.flush()
        replacement = Task(id=original.task_id, user_id=original.actor_id, project_id=original.project_id,
                           chapter_id=original.task_chapter_id, kind="chapter.analyze", status="succeeded",
                           queued_at=original.queued_at, execution_key=str(uuid4()), input={"request": {}})
        db.add(replacement)
        await task_statistics_service.record_created(db, replacement)
        await db.commit()
        assert replacement.id == original.task_id and replacement.statistics_key != original.task_key
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, original)
        await db.rollback()
        assert await _children(db) == []


async def test_same_task_new_execution_rejects_late_on_success(app, analysis_index_world):
    original, _ = analysis_index_world
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, original.task_id)
        task.queued_at = original.queued_at + timedelta(microseconds=1)
        task.execution_key = str(uuid4())
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, original)
        await db.rollback()
        assert await _children(db) == []


async def test_analysis_enqueue_without_stale_memory_is_noop(app, analysis_index_world):
    execution, memory_id = analysis_index_world
    async with app.state.sessionmaker() as db:
        config = await db.scalar(select(AIConfig).where(
            AIConfig.user_id == execution.actor_id, AIConfig.model_type == "embedding",
        ))
        chunk = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_id,
        ))
        chunk.embedding, chunk.embedding_key = b"existing", f"{retrieval_service.config_model_key(config)}:3"
        await db.commit()
    async with app.state.sessionmaker() as db:
        admission = await _enqueue(app, db, execution)
        assert admission.state == "no_work" and admission.task_id is None
        await db.commit()
        assert await _children(db) == []


async def test_analysis_enqueue_rejects_frozen_vector_selection_after_model_removed(app, analysis_index_world):
    # 冻结的 vector 选择不会因配置删除自动改成关键词（revalidate_shared_embedding 契约），只能失败关闭。
    execution, _ = analysis_index_world
    assert execution.embedding_selection.mode == "vector"
    async with app.state.sessionmaker() as db:
        await db.delete(await db.scalar(select(AIConfig).where(
            AIConfig.user_id == execution.actor_id, AIConfig.model_type == "embedding",
        )))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises((BadRequestError, analysis_service.AnalysisExecutionChanged)):
            await _enqueue(app, db, execution)
        await db.rollback()
        assert await _children(db) == []
        assert await db.scalar(select(func.count(AnalysisIndexReceipt.receipt_key))) == 0


async def test_analysis_enqueue_statistics_failure_rolls_back_only_child(app, analysis_index_world, monkeypatch):
    execution, _ = analysis_index_world
    original = task_statistics_service.record_created

    async def fail_after_flush(db, task):
        await original(db, task)
        raise RuntimeError("模拟统计插入后失败")

    monkeypatch.setattr(task_statistics_service, "record_created", fail_after_flush)
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError, match="统计插入后失败"):
            await _enqueue(app, db, execution)
        # 调用方处理异常后继续提交，内部保存点不能留下孤立任务或统计。
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await _children(db) == []
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(TaskStatistic.kind == "kb.index")) == 0


async def test_concurrent_analysis_enqueues_serialize_and_deduplicate(app, analysis_index_world):
    execution, _ = analysis_index_world
    factory = app.state.sessionmaker
    entered = asyncio.Event()

    async def second():
        async with factory() as db:
            entered.set()
            result = await _enqueue(app, db, execution)
            await db.commit()
            return result.state

    pending = None
    async with factory() as first:
        child_id = (await _enqueue(app, first, execution)).task_id
        assert child_id
        pending = asyncio.create_task(second())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(0.05)
            assert not pending.done()
            await first.commit()
            assert await asyncio.wait_for(pending, 5) == "existing"
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    async with factory() as db:
        assert [row.id for row in await _children(db)] == [child_id]


async def test_memory_owner_http_uses_original_actor_for_index(app, client, analysis_index_world):
    execution, _ = analysis_index_world
    response = await client.post(f"/api/v1/projects/{execution.project_id}/memories", json={
        "chapter_no": 1, "content": "人工新增的记忆",
    })
    assert response.status_code == 201, response.text
    async with app.state.sessionmaker() as db:
        children = await _children(db)
        assert len(children) == 1 and children[0].user_id == execution.actor_id
        assert children[0].project_id == execution.project_id


async def test_memory_enqueue_failure_does_not_report_committed_write_failed(
    app, client, analysis_index_world, monkeypatch, caplog,
):
    execution, _ = analysis_index_world

    async def fail(*_args, **_kwargs):
        raise RuntimeError("private-index-sql secret-content")

    monkeypatch.setattr(kb_index_service, "enqueue_memory", fail)
    response = await client.post(f"/api/v1/projects/{execution.project_id}/memories", json={
        "chapter_no": 1, "content": "已成功保存的记忆",
    })
    assert response.status_code == 201, response.text
    async with app.state.sessionmaker() as db:
        stored = await db.get(StoryMemory, response.json()["id"])
        assert stored.content == "已成功保存的记忆" and await _children(db) == []
    assert "private-index-sql" not in caplog.text and "secret-content" not in caplog.text


async def test_memory_token_revoked_after_commit_returns_401_and_does_not_enqueue(
    app, client, analysis_index_world, monkeypatch,
):
    execution, _ = analysis_index_world
    entered, release = asyncio.Event(), asyncio.Event()
    original = kb_index_service.enqueue_memory

    async def hold_before_enqueue(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(kb_index_service, "enqueue_memory", hold_before_enqueue)
    request = asyncio.create_task(client.post(f"/api/v1/projects/{execution.project_id}/memories", json={
        "chapter_no": 1, "content": "撤销令牌前已提交",
    }))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with app.state.sessionmaker() as db:
            user = await db.get(User, execution.actor_id)
            user.token_version += 1
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 401 and "撤销令牌前已提交" not in response.text
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count(StoryMemory.id)).where(
                StoryMemory.project_id == execution.project_id, StoryMemory.content == "撤销令牌前已提交",
            )) == 1
            assert await _children(db) == []
    finally:
        release.set()
        if not request.done():
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


async def test_editor_memory_http_does_not_enqueue_owner_index(app, client, make_client, analysis_index_world):
    execution, _ = analysis_index_world
    editor = make_client()
    await register(editor, "analysis-index-editor")
    base = f"/api/v1/projects/{execution.project_id}"
    invite = await client.post(base + "/collaborators", json={"username": "analysis-index-editor", "role": "editor"})
    assert invite.status_code == 201, invite.text
    accepted = await editor.post(f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
                                 json={"expected_revision": 1})
    assert accepted.status_code == 200, accepted.text
    response = await editor.post(base + "/memories", json={"chapter_no": 1, "content": "协作者人工记忆"})
    assert response.status_code == 201, response.text
    async with app.state.sessionmaker() as db:
        assert await _children(db) == []


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_editor_analysis_grant_never_uses_owner_embedding(app, client, make_client, fake_llm):
    from tests.test_collaboration_analysis_execution import _accepted_editor_analysis

    editor_client = make_client()
    project, _chapter, editor, member, _source_job, execution, _job = await _accepted_editor_analysis(
        app, client, editor_client, fake_llm, owner_fallback=True,
    )
    # 执行本身合法且冻结了作者向量；拒绝必须来自协作授权策略，而不是缺证。
    assert analysis_service._valid_execution(execution) and execution.grant_identity is not None
    assert execution.embedding_selection.mode == "vector"
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        await db.rollback()
    revoked = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}", params={
        "expected_revision": member["revision"],
    })
    assert revoked.status_code == 204, revoked.text
    async with app.state.sessionmaker() as db:
        with pytest.raises(analysis_service.AnalysisExecutionChanged):
            await _enqueue(app, db, execution)
        await db.rollback()
        assert await _children(db) == []
        assert await db.scalar(select(func.count(AnalysisIndexReceipt.receipt_key))) == 0


async def test_manual_wrapper_rejects_missing_or_mismatched_original_auth(app, analysis_index_world):
    execution, _ = analysis_index_world
    with pytest.raises(UnauthorizedError):
        await kb_index_service.enqueue_memory(
            app.state.sessionmaker, actor_id=execution.actor_id, project_id=execution.project_id,
            authenticated_actor=None,
        )
    async with app.state.sessionmaker() as db:
        assert await _children(db) == []
