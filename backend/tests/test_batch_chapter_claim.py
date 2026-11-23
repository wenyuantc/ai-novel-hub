"""批量章 attempt 的旧断点和旧清理不能侵入后来的抢占。"""

import copy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.providers import AIProviderError
from app.core.errors import ConflictError
from app.models import (
    AIConfig,
    AnalysisIndexReceipt,
    Chapter,
    SceneReviewAnalysisReceipt,
    SceneReviewProposal,
    Task,
)
from app.services import (
    analysis_service,
    batch_service,
    chapter_claim,
    collaboration_ai_guard,
    generation_service,
    project_ai_config,
    rich_document_review,
    task_service,
)
from app.services.task_engine import execution_identity
from app.services.task_engine.handlers.batch import BatchHandler
from tests.helpers import Gate, analysis_json, create_ai_config, run_task, task_params, wait_task, wait_until
from tests.test_batch_generate import _gen_calls, _setup, _start
from tests.test_chapter_claim_core import _world
from tests.test_rich_document_api import add_section, save_payload


async def _batch_world(app, client, make_client):
    identity, execution = await _world(app, client, make_client, batch=True)
    config = await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        model_binding = await project_ai_config.resolve_project_model(
            db, identity["actor_id"], identity["project_id"], "text", cipher=app.state.cipher,
            config_id=config["id"], for_update=True,
        )
        embedding_selection = await project_ai_config.resolve_shared_embedding(
            db, identity["actor_id"], identity["project_id"], cipher=app.state.cipher, for_update=True,
        )
        task.input = {**task.input, "request": {"project_id": identity["project_id"],
                      "start_chapter_id": chapter.id, "end_chapter_id": chapter.id},
                      "ai_config_id": config["id"], "_task_actor_token_version": 0,
                      "_project_model": project_ai_config.serialize_project_model(model_binding),
                      "_shared_embedding": project_ai_config.serialize_shared_embedding(embedding_selection)}
        task.checkpoint = batch_service.new_checkpoint({chapter.id: chapter}, [chapter.id])
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        job = await batch_service.build_batch_job(app.state.task_engine.ctx, task, execution=_running_execution(task))
    return identity, execution, task, job


def _running_execution(task):
    status, execution = task.status, task.execution_key
    task.status, task.execution_key = "queued", None
    try:
        return execution_identity.from_queued_task(task, execution)
    finally:
        task.status, task.execution_key = status, execution


async def test_old_checkpoint_cannot_replace_new_attempt_pointer(app, client, make_client):
    identity, execution, _task, old_job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        first = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        assert await chapter_claim.release_chapter_claim(db, first)
        second = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        assert first.claim_key != second.claim_key
        await db.commit()
    # 旧工作对象里的 current 仍是抢占前的 None，不得覆盖第二次 attempt。
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await batch_service._write_checkpoint(db, old_job)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert task.checkpoint["current"]["_chapter_claim"]["claim_key"] == second.claim_key
        assert chapter.generation_claim_key == second.claim_key


@pytest.mark.parametrize("field", ["queued_at", "execution_key"])
async def test_old_batch_job_cannot_write_a_replaced_execution_round(app, client, make_client, field):
    identity, _execution, _task, old_job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        if field == "queued_at":
            task.queued_at += timedelta(microseconds=1)
        else:
            task.execution_key = str(uuid4())
        await db.commit()
    old_job.checkpoint["items"][0]["error"] = "旧执行不应写入"
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await batch_service._write_checkpoint(db, old_job)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        assert task.checkpoint["items"][0]["error"] == ""


@pytest.mark.parametrize("field", ["checkpoint", "input"])
async def test_batch_snapshot_rejects_boolean_in_place_of_database_id(app, client, make_client, field):
    identity, _execution, _task, old_job = await _batch_world(app, client, make_client)
    assert identity["project_id"] == identity["chapter_id"] == 1
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        if field == "checkpoint":
            checkpoint = copy.deepcopy(task.checkpoint)
            checkpoint["items"][0]["chapter_id"] = True
            await db.execute(update(Task).where(Task.id == task.id).values(checkpoint=checkpoint))
        else:
            payload = copy.deepcopy(task.input)
            payload["request"]["project_id"] = True
            await db.execute(update(Task).where(Task.id == task.id).values(input=payload))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await batch_service._write_checkpoint(db, old_job)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        value = (task.checkpoint["items"][0]["chapter_id"] if field == "checkpoint"
                 else task.input["request"]["project_id"])
        assert value is True


async def test_old_abort_cannot_clear_new_attempt_in_same_execution(app, client, make_client):
    identity, execution, task_snapshot, job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        first = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        await db.commit()
    # 模拟 runner 获得第一个 attempt 后，同一执行体后来又持有新的 attempt。
    job.active_claim = first
    task_snapshot._batch_job = job
    async with app.state.sessionmaker() as db:
        assert await chapter_claim.release_chapter_claim(db, first)
        second = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        await db.commit()
    with pytest.raises(ConflictError):
        await BatchHandler().on_abort(app.state.task_engine.ctx, task_snapshot, execution=job.execution_identity)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert chapter.status == "generating" and chapter.generation_claim_key == second.claim_key
        assert task.checkpoint["current"]["_chapter_claim"]["claim_key"] == second.claim_key


async def test_old_idle_abort_cannot_finalize_while_later_attempt_is_active(app, client, make_client):
    identity, execution, task_snapshot, job = await _batch_world(app, client, make_client)
    task_snapshot._batch_job = job
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        await db.commit()
    with pytest.raises(ConflictError):
        await BatchHandler().on_abort(app.state.task_engine.ctx, task_snapshot, execution=job.execution_identity)
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert chapter.status == "generating" and chapter.generation_claim_key == claim.claim_key


async def test_old_terminal_cas_cannot_finish_new_attempt(app, client, make_client):
    identity, execution, task_snapshot, job = await _batch_world(app, client, make_client)
    task_snapshot._batch_job = job
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        await db.commit()
    _row, committed = await app.state.task_engine.complete_state(
        task_snapshot, expected_status="running", status="failed", result=None, error="旧执行体失败",
        text="", progress=0, abort_ok=True,
    )
    assert not committed
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert task.status == "running" and task.execution_key == execution
        assert chapter.generation_claim_key == claim.claim_key
        assert task.checkpoint["current"]["_chapter_claim"]["claim_key"] == claim.claim_key


async def _captured_running_attempt(app, client, make_client):
    identity, execution, task, job = await _batch_world(app, client, make_client)
    job.checkpoint["_batch_attempt"] = str(uuid4())
    async with app.state.sessionmaker() as db:
        await batch_service._write_checkpoint(db, job)
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
        job.checkpoint["current"] = {"chapter_id": claim.chapter_id, "prev_status": claim.prev_status,
                                     "_chapter_claim": pointer}
        job.persisted_checkpoint = copy.deepcopy(job.checkpoint)
        job.active_claim = claim
        await db.commit()
    task._batch_job = job
    return identity, execution, task, job, claim, batch_service.capture_abort_snapshot(task)


async def test_abort_snapshot_preserves_original_facts_and_releases_exact_claim(app, client, make_client):
    identity, _execution, _task, job, claim, snapshot = await _captured_running_attempt(app, client, make_client)
    job.persisted_checkpoint["items"][0]["error"] = "不能改变原快照"
    assert snapshot.checkpoint["items"][0]["error"] == ""
    restored = await batch_service.restore_abort_snapshot(app.state.task_engine.ctx, snapshot)
    assert restored.claim is None and restored.checkpoint["current"] is None
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, identity["task_id"]), await db.get(Chapter, identity["chapter_id"])
        assert task.checkpoint == restored.checkpoint
        assert chapter.status == claim.prev_status and chapter.generation_claim_key is None


async def test_uncertain_attempt_release_and_marker_are_one_transaction(app, client, make_client):
    identity, _execution, _task, _job, claim, snapshot = await _captured_running_attempt(app, client, make_client)
    async with app.state.sessionmaker() as db:
        checkpoint = await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=None)
        assert checkpoint["current"] is None
        assert checkpoint["_batch_outcome_unknown"]["claim_key"] == claim.claim_key
        await db.rollback()
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, identity["chapter_id"])
        task = await db.get(Task, identity["task_id"])
        assert chapter.generation_claim_key == claim.claim_key
        assert task.checkpoint == snapshot.checkpoint

    async with app.state.sessionmaker() as db:
        checkpoint = await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=None)
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="interrupted", execution_key=None,
        ))
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await batch_service.confirm_uncertain_attempt(
            db, snapshot, status="interrupted", expected_operation_id=None,
        )
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert task.checkpoint == checkpoint and chapter.generation_claim_key is None
        assert "_batch_outcome_unknown" not in BatchHandler().checkpoint_out(task)
        with pytest.raises(ConflictError):
            await batch_service.revalidate(db, app.state.task_engine.ctx, task)


@pytest.mark.parametrize("change", ["attempt", "admin_intent"])
async def test_uncertain_attempt_does_not_release_replaced_claim_or_intent(app, client, make_client, change):
    identity, execution, _task, _job, claim, snapshot = await _captured_running_attempt(app, client, make_client)
    async with app.state.sessionmaker() as db:
        if change == "attempt":
            assert await chapter_claim.release_chapter_claim(db, claim)
            task = await db.get(Task, identity["task_id"])
            checkpoint = copy.deepcopy(task.checkpoint)
            checkpoint["_batch_attempt"] = str(uuid4())
            task.checkpoint = checkpoint
            await db.flush()
            claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        else:
            task = await db.get(Task, identity["task_id"])
            task.admin_cancel_operation_id = str(uuid4())
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=None)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        chapter = await db.get(Chapter, identity["chapter_id"])
        assert task.status == "running" and "_batch_outcome_unknown" not in task.checkpoint
        assert chapter.generation_claim_key == claim.claim_key


@pytest.mark.parametrize("change", [
    "marker", "cursor", "queued_at", "task_key", "input", "old_claim", "rekeyed_claim",
])
async def test_uncertain_attempt_confirmation_requires_original_committed_facts(app, client, make_client, change):
    identity, _execution, _task, _job, claim, snapshot = await _captured_running_attempt(app, client, make_client)
    async with app.state.sessionmaker() as db:
        await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=None)
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="interrupted", execution_key=None,
        ))
        await db.commit()
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, identity["task_id"]), await db.get(Chapter, identity["chapter_id"])
        if change == "marker":
            checkpoint = copy.deepcopy(task.checkpoint)
            checkpoint["_batch_outcome_unknown"]["claim_key"] = str(uuid4())
            task.checkpoint = checkpoint
        elif change == "cursor":
            task.step_cursor += 1
        elif change == "queued_at":
            task.queued_at += timedelta(microseconds=1)
        elif change == "task_key":
            task.statistics_key = str(uuid4())
        elif change == "input":
            task.input = {**task.input, "unexpected": "changed"}
        else:
            chapter.status = "generating"
            chapter.generation_claim_key = claim.claim_key
            chapter.generation_claim_task_key = str(uuid4()) if change == "rekeyed_claim" else claim.task_key
            chapter.generation_claim_prev_status = claim.prev_status
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert not await batch_service.confirm_uncertain_attempt(
            db, snapshot, status="interrupted", expected_operation_id=None,
        )


async def test_uncertain_attempt_confirmation_binds_original_admin_intent(app, client, make_client):
    identity, _execution, _task, _job, _claim, snapshot = await _captured_running_attempt(app, client, make_client)
    operation_id = str(uuid4())
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            admin_cancel_operation_id=operation_id,
        ))
        await db.commit()
    async with app.state.sessionmaker() as db:
        checkpoint = await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=operation_id)
        assert checkpoint["_batch_outcome_unknown"]["operation_id"] == operation_id
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="interrupted", execution_key=None, admin_cancel_operation_id=None,
        ))
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await batch_service.confirm_uncertain_attempt(
            db, snapshot, status="interrupted", expected_operation_id=operation_id,
        )
        assert not await batch_service.confirm_uncertain_attempt(
            db, snapshot, status="interrupted", expected_operation_id=str(uuid4()),
        )


async def test_uncertain_confirmation_allows_later_independent_chapter_claim(app, client, make_client):
    identity, _execution, _task, _job, claim, snapshot = await _captured_running_attempt(app, client, make_client)
    async with app.state.sessionmaker() as db:
        await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=None)
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="interrupted", execution_key=None,
        ))
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, identity["chapter_id"])
        replacement = Task(
            user_id=identity["actor_id"], project_id=identity["project_id"], kind="chapter.batch", status="running",
            resource_key=f"batch:{identity['project_id']}", execution_key=str(uuid4()), input=snapshot.input,
            checkpoint=batch_service.new_checkpoint({chapter.id: chapter}, [chapter.id]),
        )
        db.add(replacement)
        await db.flush()
        later = await chapter_claim.claim_batch_chapter(
            db, task_id=replacement.id, task_key=replacement.statistics_key, actor_id=identity["actor_id"],
            project_id=identity["project_id"], chapter_id=chapter.id, queued_at=replacement.queued_at,
            execution_key=replacement.execution_key, grant_identity=None,
        )
        assert later.claim_key != claim.claim_key
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await batch_service.confirm_uncertain_attempt(
            db, snapshot, status="interrupted", expected_operation_id=None,
        )


@pytest.mark.parametrize("corrupt", [False, True])
async def test_uncertain_batch_http_retry_is_blocked_and_marker_is_private(app, client, make_client, fake_llm, corrupt):
    identity, _execution, _task, _job, _claim, snapshot = await _captured_running_attempt(app, client, make_client)
    async with app.state.sessionmaker() as db:
        checkpoint = await batch_service.release_uncertain_attempt(db, snapshot, expected_operation_id=None)
        if corrupt:
            checkpoint["_batch_outcome_unknown"] = None
        await db.execute(update(Task).where(Task.id == identity["task_id"]).values(
            status="interrupted", execution_key=None, checkpoint=checkpoint,
        ))
        await db.commit()
    params = {"task_key": identity["task_key"]}
    response = await client.post(f"/api/v1/tasks/{identity['task_id']}/retry", params=params)
    assert response.status_code == 409 and not fake_llm.calls
    response = await client.get(f"/api/v1/tasks/{identity['task_id']}", params=params)
    assert response.status_code == 200 and "_batch_outcome_unknown" not in response.text


async def test_idle_abort_snapshots_reject_new_worker_in_same_execution(app, client, make_client):
    _, execution_key, task, job = await _batch_world(app, client, make_client)
    engine = app.state.task_engine
    task._batch_job = job
    snapshot = engine.capture_abort_snapshot(task)
    assert snapshot is not None and snapshot.claim.claim is None

    # 同一轮的输入、断点和业务执行键全部保留，仅接管者发生变化。
    new_worker = await engine.lease.register()
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task.id)
        current.worker_key = new_worker
        await db.commit()
    with pytest.raises(ConflictError):
        await batch_service.restore_abort_snapshot(engine.ctx, snapshot.claim)
    for proof in ({"abort_snapshot": snapshot}, {"batch_snapshot": snapshot.claim}):
        _, committed = await engine.complete_state(
            current, expected_status="running", status="interrupted", result=None, error="",
            text="", progress=0, abort_ok=True, **proof,
        )
        assert not committed
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task.id)
        assert (current.status, current.execution_key, current.worker_key) == ("running", execution_key, new_worker)


async def test_old_abort_snapshot_rejects_new_attempt_even_after_release(app, client, make_client):
    identity, execution, _task, _job, first, snapshot = await _captured_running_attempt(app, client, make_client)
    async with app.state.sessionmaker() as db:
        assert await chapter_claim.release_chapter_claim(db, first)
        task = await db.get(Task, identity["task_id"])
        checkpoint = copy.deepcopy(task.checkpoint)
        checkpoint["_batch_attempt"] = str(uuid4())
        task.checkpoint = checkpoint
        await db.flush()
        second = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        assert await chapter_claim.release_chapter_claim(db, second)
        await db.commit()
    with pytest.raises(ConflictError):
        await batch_service.restore_abort_snapshot(app.state.task_engine.ctx, snapshot)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        assert task.status == "running" and task.checkpoint["current"] is None
        assert task.checkpoint["_batch_attempt"] != snapshot.checkpoint["_batch_attempt"]


@pytest.mark.parametrize("awaiting_review", [False, True])
async def test_queued_batch_snapshot_never_discards_unreviewed_proposal(app, client, make_client, awaiting_review):
    identity, _execution, _task, _job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        task.status, task.execution_key = "queued", None
        checkpoint = copy.deepcopy(task.checkpoint)
        if awaiting_review:
            checkpoint["phase"] = "awaiting_review"
            checkpoint["rich_review"] = {"phase": "awaiting_review"}
        task.checkpoint = checkpoint
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        if awaiting_review:
            with pytest.raises(ConflictError):
                batch_service.capture_abort_snapshot(task)
        else:
            snapshot = batch_service.capture_abort_snapshot(task)
            restored = await batch_service.restore_abort_snapshot(app.state.task_engine.ctx, snapshot)
            assert restored.claim is None and restored.execution_key is None


async def test_real_batch_retry_advances_attempt_epoch(app, client, fake_llm, monkeypatch):
    _project, base, (chapter,), _ = await _setup(client, [""])
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))
    original = chapter_claim.claim_batch_chapter
    attempts = []

    async def capture_attempt(db, **identity):
        claim = await original(db, **identity)
        checkpoint = await db.scalar(select(Task.checkpoint).where(Task.id == claim.task_id))
        attempts.append((claim.claim_key, checkpoint.get("_batch_attempt")))
        return claim

    monkeypatch.setattr(chapter_claim, "claim_batch_chapter", capture_attempt)
    gate = Gate()
    fake_llm.push(AIProviderError("第一次失败"))
    fake_llm.push([gate, "第二次成功"])
    started = await _start(client, base, chapter, chapter)
    assert started.status_code == 202, started.text
    try:
        await gate.wait_reached()
        assert len(attempts) == 2
        assert attempts[0][0] != attempts[1][0]
        assert attempts[0][1] and attempts[1][1] and attempts[0][1] != attempts[1][1]
    finally:
        gate.release()
        await wait_task(client, started.json(), within=15)


async def test_batch_acceptance_freezes_text_and_embedding_choices(app, client):
    project, base, (chapter,), _ = await _setup(client, [""])
    await app.state.task_engine.stop()
    response = await _start(client, base, chapter, chapter)
    assert response.status_code == 202, response.text
    task_ref = response.json()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_ref["id"])
        text_binding, embedding_binding = task.input.get("_project_model"), task.input.get("_shared_embedding")
        assert text_binding["actor_id"] == task.user_id and text_binding["project_id"] == project["id"]
        assert text_binding["config_id"] == task.input["ai_config_id"]
        assert embedding_binding["actor_id"] == task.user_id and embedding_binding["project_id"] == project["id"]
        assert embedding_binding["mode"] == "keyword" and embedding_binding["reason"] == "embedding_config_missing"
    assert "_project_model" not in response.text and "_shared_embedding" not in response.text


async def test_batch_does_not_send_if_accepted_model_changes(app, client, fake_llm):
    _project, base, (chapter,), _ = await _setup(client, [""])
    await app.state.task_engine.stop()
    response = await _start(client, base, chapter, chapter)
    assert response.status_code == 202, response.text
    task_ref = response.json()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_ref["id"])
        config = await db.get(AIConfig, task.input["ai_config_id"])
        config.model = "changed-after-acceptance"
        await db.commit()
    fake_llm.push("不应发送或保存")
    await app.state.task_engine.start()
    failed = await wait_task(client, task_ref, within=15, statuses=("failed",))
    assert failed["status"] == "failed" and fake_llm.calls == []
    assert (await client.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == ""


async def test_batch_does_not_send_if_model_id_is_reused_with_identical_config(app, client, fake_llm):
    _project, base, (chapter,), _ = await _setup(client, [""])
    await app.state.task_engine.stop()
    response = await _start(client, base, chapter, chapter)
    assert response.status_code == 202, response.text
    task_ref = response.json()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_ref["id"])
        config = await db.get(AIConfig, task.input["ai_config_id"])
        original_key = config.instance_key
        original_fingerprint = project_ai_config._fingerprint(config)
        values = {column.key: getattr(config, column.key) for column in AIConfig.__mapper__.column_attrs
                  if column.key not in {"id", "instance_key", "created_at", "updated_at"}}
        config_id = config.id
        await db.delete(config)
        await db.flush()
        replacement = AIConfig(id=config_id, instance_key=str(uuid4()), **values)
        db.add(replacement)
        await db.flush()
        assert replacement.instance_key != original_key
        assert project_ai_config._fingerprint(replacement) == original_fingerprint
        await db.commit()
    fake_llm.push("不得发送到替换模型")
    await app.state.task_engine.start()
    failed = await wait_task(client, task_ref, within=15, statuses=("failed",))
    assert failed["status"] == "failed" and fake_llm.calls == []
    assert (await client.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == ""


async def test_batch_keeps_accepted_keyword_choice_after_embedding_added(app, client, fake_llm):
    _project, base, (chapter,), _ = await _setup(client, [""])
    await app.state.task_engine.stop()
    response = await _start(client, base, chapter, chapter)
    assert response.status_code == 202, response.text
    await create_ai_config(client, model_type="embedding", model="new-embedding")
    fake_llm.push("关键词模式下的章节正文")
    await app.state.task_engine.start()
    complete = await wait_task(client, response.json(), within=15, statuses=("succeeded", "failed"))
    assert complete["status"] == "succeeded" and len(_gen_calls(fake_llm)) == 1
    assert fake_llm.embed_calls == []


@pytest.mark.parametrize("change", ["actor", "queued_at", "execution", "input", "checkpoint", "token"])
async def test_batch_job_rejects_changed_claimed_task_identity(app, client, make_client, change):
    _identity, _execution, task, _job = await _batch_world(app, client, make_client)
    original = _running_execution(task)
    if change == "actor":
        other = replace(original, actor_id=original.actor_id + 100)
    elif change == "queued_at":
        other = replace(original, queued_at=original.queued_at + timedelta(microseconds=1))
    elif change == "execution":
        other = replace(original, execution_key=str(uuid4()))
    elif change == "input":
        other = replace(original, input_json="{}")
    elif change == "checkpoint":
        other = replace(original, checkpoint_json="{}")
    else:
        other = replace(original, actor_token_version=None)
    with pytest.raises(ConflictError):
        await BatchHandler().build_job(app.state.task_engine.ctx, task, execution=other)


async def test_batch_generation_captures_attempt_in_claim_transaction(app, client, make_client, monkeypatch):
    identity, _execution, _task, job = await _batch_world(app, client, make_client)
    job.checkpoint["items"][0]["target_words"] = 1000

    class Captured(Exception):
        pass

    async def build(db, **kwargs):
        attempt = kwargs.get("batch_attempt")
        assert type(attempt) is collaboration_ai_guard.BatchAttemptIdentity
        task = await db.get(Task, identity["task_id"])
        assert attempt.claim == job.active_claim and attempt.cursor == task.step_cursor == 0
        assert attempt.claim.chapter_id == identity["chapter_id"]
        assert attempt.attempt_key == task.checkpoint["_batch_attempt"]
        assert task.checkpoint["current"]["_chapter_claim"]["claim_key"] == attempt.claim.claim_key
        raise Captured

    monkeypatch.setattr(generation_service, "build_chapter_job", build)
    with pytest.raises(Captured):
        async for _ in batch_service._generate_chapter(app.state.task_engine.ctx, job, 0, job.checkpoint["items"][0]):
            pass


async def test_batch_generation_rejects_mismatched_attempt_cursor(app, client, make_client):
    identity, _execution, _task, job = await _batch_world(app, client, make_client)
    job.cursor = 1
    job.checkpoint["items"][0]["target_words"] = 1000
    with pytest.raises(ConflictError):
        async for _ in batch_service._generate_chapter(app.state.task_engine.ctx, job, 0, job.checkpoint["items"][0]):
            pass
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, identity["task_id"]), await db.get(Chapter, identity["chapter_id"])
        assert task.step_cursor == 0 and task.checkpoint["current"] is None
        assert chapter.generation_claim_key is None


async def test_batch_does_not_publish_delta_without_current_claim(app, client, make_client):
    identity, _execution, _task, job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        with pytest.raises(ConflictError):
            await BatchHandler().verify_event(
                db, task, job=job, execution=job.execution_identity, current_execution=job.task_execution_identity,
                event="delta", data={"text": "没有所属章节的正文"},
            )
        assert job.verified_text == ""


async def test_batch_task_output_does_not_expose_claim_or_completion_receipt(app, client, make_client):
    identity, execution, _task, _job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        task = await db.get(Task, identity["task_id"])
        checkpoint = copy.deepcopy(task.checkpoint)
        attempt = str(uuid4())
        checkpoint["_batch_attempt"] = attempt
        checkpoint["_batch_completion"] = {"execution_key": execution, "claim_key": claim.claim_key}
        task.checkpoint = checkpoint
        await db.commit()
        task = await db.scalar(select(Task).where(Task.id == identity["task_id"]))
        output = task_service.out_json(task)
        assert claim.claim_key not in str(output)
        assert execution not in str(output)
        assert attempt not in str(output)


async def test_forged_completion_receipt_cannot_resume_as_completed(app, client, make_client):
    identity, _execution, _task, _job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        checkpoint = copy.deepcopy(task.checkpoint)
        checkpoint["phase"] = "analyze"
        checkpoint["items"][0]["state"] = "done"
        checkpoint["_batch_completion"] = {"schema": 1, "version_id": 999999}
        task.checkpoint = checkpoint
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        with pytest.raises(ConflictError):
            await batch_service.build_batch_job(app.state.task_engine.ctx, task, execution=_running_execution(task))


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_legacy_analysis_without_send_receipt_does_not_replay(app, client, make_client, fake_llm):
    identity, _execution, _task, _job = await _batch_world(app, client, make_client)
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        checkpoint = copy.deepcopy(task.checkpoint)
        checkpoint["phase"] = "analyze"
        checkpoint["items"][0]["state"] = "done"
        task.checkpoint = checkpoint
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        job = await batch_service.build_batch_job(app.state.task_engine.ctx, task, execution=_running_execution(task))
    fake_llm.push(analysis_json())
    item = job.checkpoint["items"][0]
    async for _event in batch_service._analyze_chapter(app.state.task_engine.ctx, job, 0, item):
        pass
    assert not fake_llm.calls
    assert item["analysis"] == "failed" and "未自动重复" in item["warning"]


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("vector", [False, True])
async def test_batch_reports_index_fallback_after_successful_analysis(app, client, fake_llm, vector):
    _project, base, (chapter,), _ = await _setup(client, [""])
    if vector:
        await create_ai_config(client, model_type="embedding", model="embedding-model")
    fake_llm.push("已提交的章节正文")
    fake_llm.push(analysis_json())
    task, _ = await run_task(client, await _start(client, base, chapter, chapter))
    assert task["status"] == "succeeded"
    item = task["checkpoint"]["items"][0]
    assert item["analysis"] == "ok"
    async with app.state.sessionmaker() as db:
        children = list(await db.scalars(select(Task).where(Task.kind == "kb.index").order_by(Task.id)))
        receipts = list(await db.scalars(select(AnalysisIndexReceipt)))
        batch = await db.scalar(select(Task).where(Task.id == task["id"]))
    if not vector:
        assert analysis_service.ANALYSIS_KEYWORD_WARNING in item["warning"]
        assert children == [] and receipts == []
        return
    # 批量分析阶段按原 batch 执行的收据受理派生索引：不再提示「暂未生成」，子任务归原 actor。
    assert analysis_service.ANALYSIS_INDEX_PENDING_WARNING not in (item.get("warning") or "")
    assert len(children) == 1 and len(receipts) == 1
    child, receipt = children[0], receipts[0]
    assert (child.user_id, child.project_id) == (batch.user_id, batch.project_id)
    assert child.input["_task_actor_token_version"] == batch.input["_task_actor_token_version"]
    assert child.input["_shared_embedding"] == batch.input["_shared_embedding"]
    assert (receipt.source_kind, receipt.outcome, receipt.child_task_key) == ("batch", "queued", child.statistics_key)
    assert (receipt.parent_task_key, receipt.actor_id) == (batch.statistics_key, batch.user_id)
    assert receipt.parent_queued_at is not None and receipt.parent_execution_key is not None


async def test_old_analysis_failure_cannot_mark_replaced_receipt_failed(app, client, make_client):
    identity, _execution, _task, job = await _batch_world(app, client, make_client)
    ref = {"run_key": str(uuid4()), "task_key": identity["task_key"],
           "chapter_id": identity["chapter_id"], "content_hash": "a" * 64}
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        checkpoint = copy.deepcopy(task.checkpoint)
        checkpoint["phase"] = "analyze"
        checkpoint["rich_analysis"] = ref
        task.checkpoint = checkpoint
        db.add(SceneReviewAnalysisReceipt(**ref, status="running"))
        await db.commit()
    job.checkpoint = copy.deepcopy(checkpoint)
    job.persisted_checkpoint = copy.deepcopy(checkpoint)
    # 同一整数任务和收据键仍在，但收据内容已由另一连接更换。
    async with app.state.sessionmaker() as db:
        receipt = await db.get(SceneReviewAnalysisReceipt, ref["run_key"])
        receipt.content_hash = "b" * 64
        await db.commit()
    with pytest.raises(ConflictError):
        await batch_service._fail_review_analysis(app.state.task_engine.ctx, job, ref)
    async with app.state.sessionmaker() as db:
        receipt = await db.get(SceneReviewAnalysisReceipt, ref["run_key"])
        assert receipt.content_hash == "b" * 64 and receipt.status == "running"


async def test_committed_chapter_resumes_analysis_without_regeneration(app, client, fake_llm, monkeypatch):
    _project, base, (chapter,), _ = await _setup(client, [""], protagonist=True)
    fake_llm.push("已经提交的正文")
    original = batch_service._save
    interrupted = False

    async def fail_after_body_commit(ctx, job):
        nonlocal interrupted
        if not interrupted and job.checkpoint.get("phase") == "analyze":
            interrupted = True
            raise RuntimeError("生成已提交后中断")
        await original(ctx, job)

    monkeypatch.setattr(batch_service, "_save", fail_after_body_commit)
    started = await _start(client, base, chapter, chapter, include_written=True)
    assert started.status_code == 202, started.text
    task_ref = started.json()
    failed = await wait_task(client, task_ref, within=15, statuses=("failed",))
    assert interrupted and len(_gen_calls(fake_llm)) == 1
    assert (await client.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == "已经提交的正文"
    assert failed["checkpoint"]["phase"] == "analyze"
    before = (await client.get(f"{base}/chapters/{chapter['id']}/versions")).json()
    response = await client.post(f"/api/v1/tasks/{task_ref['id']}/retry", params=task_params(task_ref))
    retried, _ = await run_task(client, response)
    assert retried["status"] == "succeeded"
    assert len(_gen_calls(fake_llm)) == 1
    assert (await client.get(f"{base}/chapters/{chapter['id']}/versions")).json() == before


async def test_recovery_of_committed_chapter_does_not_repeat_generation(app, client, fake_llm, monkeypatch):
    _project, base, (chapter,), _ = await _setup(client, [""], protagonist=True)
    fake_llm.push("重启前已提交的正文")
    original = batch_service._save
    uncertain = False

    async def lose_confirmation(ctx, job):
        nonlocal uncertain
        if not uncertain and job.checkpoint.get("phase") == "analyze":
            uncertain = True
            raise generation_service.ChapterCommitUncertain("测试提交结果未知")
        await original(ctx, job)

    monkeypatch.setattr(batch_service, "_save", lose_confirmation)
    started = await _start(client, base, chapter, chapter, include_written=True)
    assert started.status_code == 202, started.text
    ref = started.json()
    engine = app.state.task_engine
    await wait_until(lambda: uncertain and ref["id"] not in engine._running, within=15)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, ref["id"])
        assert task.status == "running" and task.execution_key is not None
        assert task.checkpoint["phase"] == "analyze" and "_batch_completion" in task.checkpoint
    await engine.stop()
    await engine.start()
    recovered = await wait_task(client, ref, within=15, statuses=("interrupted",))
    assert recovered["checkpoint"]["phase"] == "analyze"
    response = await client.post(f"/api/v1/tasks/{ref['id']}/retry", params=task_params(ref))
    retried, _ = await run_task(client, response)
    assert retried["status"] == "succeeded"
    assert len(_gen_calls(fake_llm)) == 1
    assert (await client.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == "重启前已提交的正文"


async def test_recovery_with_corrupt_receipt_keeps_running_barrier(app, client, make_client):
    identity, execution, _task, _job = await _batch_world(app, client, make_client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        checkpoint = copy.deepcopy(task.checkpoint)
        checkpoint["phase"] = "analyze"
        checkpoint["_batch_completion"] = {"schema": 1, "version_id": 999999}
        task.checkpoint = checkpoint
        await db.commit()
    await app.state.task_engine.start()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        assert task.status == "running" and task.execution_key == execution


async def test_recovery_of_batch_review_keeps_pending_without_model_replay(app, client, fake_llm):
    _project, base, (chapter,), _ = await _setup(client, ["原来的章节"])
    scene = (await client.get(f"{base}/chapters/{chapter['id']}/scenes")).json()["items"][0]
    document_url = f"{base}/chapters/{chapter['id']}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(document_url)).json())
    add_section(body)
    source = await client.put(document_url, json=body)
    assert source.status_code == 200, source.text
    fake_llm.push("新生成的候选")
    task, events = await run_task(client, await _start(client, base, chapter, chapter, include_written=True))
    assert task["status"] == "paused"
    identity = events[-1][1]["result"]
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        row = await db.get(Task, task["id"])
        row.status, row.execution_key, row.result = "running", payload["execution_receipt"]["execution_key"], None
        await db.commit()
    await app.state.task_engine.start()
    recovered = await wait_task(client, task, within=15, statuses=("paused",))
    assert recovered["rich_review"]["token"] == identity["token"]
    assert (await client.get(document_url)).json() == source.json()
    assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("analysis_result", ["completed", "failed"])
async def test_batch_analysis_is_not_resent_after_checkpoint_failure(
    app, client, fake_llm, monkeypatch, analysis_result,
):
    _project, base, (chapter,), _ = await _setup(client, [""], protagonist=True)
    fake_llm.push("已经保存的章节")
    fake_llm.push(analysis_json() if analysis_result == "completed" else AIProviderError("分析提供方不可用"))
    original = batch_service._save
    failed_once = False

    async def fail_after_analysis(ctx, job):
        nonlocal failed_once
        if not failed_once and job.cursor == 1 and job.checkpoint.get("phase") == "generate":
            failed_once = True
            raise RuntimeError("分析已提交后断点写入失败")
        await original(ctx, job)

    monkeypatch.setattr(batch_service, "_save", fail_after_analysis)
    started = await _start(client, base, chapter, chapter, include_written=True)
    assert started.status_code == 202, started.text
    ref = started.json()
    failed = await wait_task(client, ref, within=15, statuses=("failed",))
    assert failed_once and failed["checkpoint"]["phase"] == "analyze"
    assert len(fake_llm.calls) == 2
    fake_llm.push(analysis_json())  # 如错误重发，则本次调用可完成且调用数会暴露问题。
    response = await client.post(f"/api/v1/tasks/{ref['id']}/retry", params=task_params(ref))
    retried, _ = await run_task(client, response)
    assert retried["status"] == "succeeded"
    assert len(fake_llm.calls) == 2


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("simulate_crash", [False, True])
async def test_inflight_batch_analysis_is_not_automatically_resent(app, client, fake_llm, simulate_crash):
    _project, base, (chapter,), _ = await _setup(client, [""], protagonist=True)
    gate = Gate()
    fake_llm.push("已保存的正文")
    fake_llm.push([gate, analysis_json()])
    started = await _start(client, base, chapter, chapter, include_written=True)
    assert started.status_code == 202, started.text
    ref = started.json()
    try:
        await gate.wait_reached(within=15)
        async with app.state.sessionmaker() as db:
            original_execution = (await db.get(Task, ref["id"])).execution_key
        await app.state.task_engine.stop()
    finally:
        gate.release()
    if simulate_crash:
        # 模拟进程排空后的持久事实：分析已出站、结果未知，旧 Task 仍标记 running。
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, ref["id"])
            task.status, task.execution_key = "running", original_execution
            await db.commit()
    await app.state.task_engine.start()
    await wait_task(client, ref, within=15, statuses=("interrupted",))
    response = await client.post(f"/api/v1/tasks/{ref['id']}/retry", params=task_params(ref))
    retried, _ = await run_task(client, response)
    assert retried["status"] == "succeeded"
    assert len(fake_llm.calls) == 2
    assert retried["checkpoint"]["items"][0]["analysis"] == "failed"
    assert "未自动重复" in retried["checkpoint"]["items"][0]["warning"]


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_recovered_unstarted_batch_analysis_sends_once(app, client, fake_llm, monkeypatch):
    _project, base, (chapter,), _ = await _setup(client, [""], protagonist=True)
    fake_llm.push("正文已提交")
    fake_llm.push(analysis_json())
    original = batch_service._save
    uncertain = False

    async def stop_before_analysis(ctx, job):
        nonlocal uncertain
        if not uncertain and job.checkpoint.get("phase") == "analyze":
            uncertain = True
            raise generation_service.ChapterCommitUncertain("提交后状态待核对")
        await original(ctx, job)

    monkeypatch.setattr(batch_service, "_save", stop_before_analysis)
    started = await _start(client, base, chapter, chapter, include_written=True)
    assert started.status_code == 202, started.text
    ref = started.json()
    engine = app.state.task_engine
    await wait_until(lambda: uncertain and ref["id"] not in engine._running, within=15)
    await engine.stop()
    await engine.start()
    await wait_task(client, ref, within=15, statuses=("interrupted",))
    response = await client.post(f"/api/v1/tasks/{ref['id']}/retry", params=task_params(ref))
    retried, _ = await run_task(client, response)
    assert retried["status"] == "succeeded"
    assert len(fake_llm.calls) == 2 and retried["checkpoint"]["items"][0]["analysis"] == "ok"
