"""项目清理只能重放有原任务轮次与资源证据的失败，不遍历猜测。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.errors import ConflictError, UnauthorizedError
from app.models import Chapter, Project, Task, User
from app.services import chapter_claim, task_service, task_statistics_service
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.registry import get_handler
from tests.helpers import create_project, register
from tests.test_chapter_claim_wiring import _queued


async def _terminal_tasks(app, client):
    await app.state.task_engine.stop()
    user = await register(client, "abort-identity-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=project["id"], status="draft")
        old = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="cancelled")
        other = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="cancelled")
        db.add_all([chapter, old, other])
        await task_statistics_service.record_created(db, old)
        await task_statistics_service.record_created(db, other)
        await db.commit()
        return project["id"], chapter.id, old, other


async def test_unproven_old_failure_does_not_replay_any_terminal_handler(app, client, monkeypatch):
    project_id, chapter_id, old, other = await _terminal_tasks(app, client)
    engine = app.state.task_engine
    called = []

    async def abort(_ctx, task):
        called.append(task.id)

    monkeypatch.setattr(get_handler(old.kind), "on_abort", abort)
    engine._abort_failures.add(project_id)  # 没有具体原身份，不能猜是 old 或 other。
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await task_service.stop_project_tasks(db, engine, project_id)
        assert (await db.get(Chapter, chapter_id)).status == "draft"
    assert not called and old.id != other.id


async def test_proven_failure_retries_only_the_same_task_round(app, client, monkeypatch):
    project_id, _, old, other = await _terminal_tasks(app, client)
    engine = app.state.task_engine
    called = []

    async def abort(_ctx, task):
        called.append(task.id)
        if len(called) == 1:
            raise RuntimeError("第一次收尾失败")

    monkeypatch.setattr(get_handler(old.kind), "on_abort", abort)
    with pytest.raises(RuntimeError):
        await task_service._abort_loaded_task(engine, old)
    async with app.state.sessionmaker() as db:
        await task_service.stop_project_tasks(db, engine, project_id)
    assert called == [old.id, old.id] and other.id not in called


async def test_old_failure_cannot_replay_after_same_task_changes_round(app, client, monkeypatch):
    project_id, _, old, _ = await _terminal_tasks(app, client)
    engine = app.state.task_engine
    called = []

    async def abort(_ctx, task):
        called.append(task.id)
        if len(called) == 1:
            raise RuntimeError("收尾失败")

    monkeypatch.setattr(get_handler(old.kind), "on_abort", abort)
    with pytest.raises(RuntimeError):
        await task_service._abort_loaded_task(engine, old)
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, old.id)
        current.queued_at += timedelta(seconds=1)
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await task_service.stop_project_tasks(db, engine, project_id)
    assert called == [old.id]


async def test_old_failed_cleanup_never_releases_new_batch_claim(app, client, monkeypatch):
    chapter_id, task_id, key = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with maker() as db:
        task = await db.get(Task, task_id)
        queued_at = task.queued_at
    captured = []

    async def run(*_args, **kwargs):
        captured.append(kwargs["claim"])

    monkeypatch.setattr(engine, "_run", run)
    await engine.lease.register()
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, key)
    old_claim = captured[0]
    async with maker() as db:
        original = await db.get(Task, task_id)
    handler = get_handler(original.kind)
    real_abort = handler.on_abort
    calls = []

    async def abort(ctx, task, *, claim=None, terminal_status=None):
        calls.append(claim)
        if len(calls) == 1:
            raise RuntimeError("原执行体收尾失败")
        await real_abort(ctx, task, claim=claim, terminal_status=terminal_status)

    monkeypatch.setattr(handler, "on_abort", abort)
    with pytest.raises(RuntimeError):
        await task_service._abort_loaded_task(engine, original)
    async with maker() as db:
        assert await chapter_claim.release_chapter_claim(db, old_claim)
        old = await db.get(Task, task_id)
        old.status = "failed"
        batch = Task(user_id=original.user_id, project_id=original.project_id, kind="chapter.batch", status="running",
                     resource_key=f"batch:{original.project_id}", execution_key=str(uuid4()),
                     input={"request": {"project_id": original.project_id}, "chapter_ids": [chapter_id]},
                     checkpoint={"phase": "generate", "current": None, "items": []})
        db.add(batch)
        await db.flush()
        current = await chapter_claim.claim_batch_chapter(
            db, task_id=batch.id, task_key=batch.statistics_key, actor_id=batch.user_id, project_id=batch.project_id,
            chapter_id=chapter_id, queued_at=batch.queued_at, execution_key=batch.execution_key, grant_identity=None,
        )
        await db.commit()
    async with maker() as db:
        with pytest.raises(ConflictError):
            await task_service.stop_project_tasks(db, engine, original.project_id)
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.status == "generating" and chapter.generation_claim_key == current.claim_key
    # 已证明的原 claim 可安全重试；第二次仍是旧值，不能触碰新 batch 的占用。
    assert calls == [old_claim, old_claim]


@pytest.mark.parametrize("evidence", ["snapshot", "unproven"])
async def test_purge_rejects_cleanup_evidence_even_if_old_flag_missing(app, client, recycle_admin, evidence):
    project_id, _, old, _ = await _terminal_tasks(app, client)
    deleted = await client.delete(f"/api/v1/projects/{project_id}")
    assert deleted.status_code == 204, deleted.text
    listing = await recycle_admin.get("/api/v1/admin/recycle-bin/projects", params={"page_size": 100})
    assert listing.status_code == 200, listing.text
    item = next(row for row in listing.json()["items"] if row["id"] == project_id)
    engine = app.state.task_engine
    if evidence == "snapshot":
        snapshot = engine.capture_abort_snapshot(old)
        assert snapshot is not None
        engine._failed_cleanups[old.id] = snapshot
    else:
        engine._unproven_abort_failures.add(project_id)
    engine._abort_failures.discard(project_id)
    response = await recycle_admin.post(f"/api/v1/admin/recycle-bin/projects/{project_id}/purge", json={
        "deleted_at": item["deleted_at"], "title": item["title"], "confirm": True,
    })
    assert response.status_code == 409 and str(old.statistics_key) not in response.text
    assert engine.has_cleanup_failure(project_id)


@pytest.mark.parametrize("checkpoint", [None, {"_analysis_pending": None}, {"_analysis_completion": {"bad": True}}])
async def test_recovery_never_replays_unproven_running_analysis(app, client, checkpoint):
    project_id, _, old, _ = await _terminal_tasks(app, client)
    engine = app.state.task_engine
    execution = str(uuid4())
    async with app.state.sessionmaker() as db:
        task = Task(user_id=old.user_id, project_id=project_id, kind="chapter.analyze", status="running",
                    execution_key=execution, checkpoint=checkpoint)
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        task_id = task.id
    await engine.start()
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task_id)
        assert current.status == "running" and current.execution_key == execution
        assert current.checkpoint == checkpoint and engine.has_cleanup_failure(project_id)
        visible = task_service.to_out(current, engine=engine)
        assert visible.stage == "结果待核对" and "尚未确认" in visible.error


@pytest.mark.parametrize(("kind", "marker", "status", "blocked"), [
    ("chapter.analyze", "_analysis_pending", "failed", True),
    ("chapter.analyze", "_analysis_completion", "failed", True),
    ("chapter.generate", "_chapter_claim", "failed", True),
    ("chapter.batch", "_batch_completion", "interrupted", True),
    ("chapter.analyze", "_analysis_pending", "succeeded", True),
    ("chapter.analyze", "_analysis_completion", "succeeded", True),
    ("agent.turn", "_external_execution", "failed", True),
])
async def test_unresolved_task_receipt_is_not_deleted_or_bulk_pruned(app, client, kind, marker, status, blocked):
    _, _, old, _ = await _terminal_tasks(app, client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, old.id)
        task.kind, task.status, task.checkpoint = kind, status, {marker: None}
        await db.commit()
    async with app.state.sessionmaker() as db:
        if blocked:
            with pytest.raises(ConflictError):
                await task_service.delete_task(db, old.user_id, old.id, expected_task_key=old.statistics_key)
            await db.rollback()
            await task_service.clear_finished(db, old.user_id)
            assert await db.get(Task, old.id) is not None
        else:
            await task_service.delete_task(db, old.user_id, old.id, expected_task_key=old.statistics_key)
            assert await db.get(Task, old.id) is None


@pytest.mark.parametrize("change", ["none", "deleted", "task_key", "disabled", "token", "other_owner"])
async def test_cancel_response_during_deletion_requires_original_owner_token_and_task(app, client, make_client, change):
    project_id, _, old, _ = await _terminal_tasks(app, client)
    other_user = None
    if change == "other_owner":
        other_user = await register(make_client(), "other-task-owner")
    out = task_service.to_out(old).model_copy(update={"checkpoint": {"secret": "private"}})
    async with app.state.sessionmaker() as db:
        user = await db.get(User, old.user_id)
        version = user.token_version
        project = await db.get(Project, project_id)
        project.deletion_started_at = datetime.now(UTC)
        project.deletion_token = str(uuid4())
        if change == "deleted":
            project.deleted_at = datetime.now(UTC)
            project.deletion_started_at = None
            project.deletion_token = None
        elif change == "task_key":
            (await db.get(Task, old.id)).statistics_key = str(uuid4())
        elif change == "disabled":
            user.is_active = False
        elif change == "token":
            user.token_version += 1
        elif change == "other_owner":
            project.user_id = other_user["id"]
        await db.commit()
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(old.user_id, version)
        if change in ("disabled", "token"):
            with pytest.raises(UnauthorizedError):
                await task_service._author_cancelled_during_deletion(
                    db, old.user_id, out, task_key=old.statistics_key, queued_at=old.queued_at,
                )
            return
        result = await task_service._author_cancelled_during_deletion(
            db, old.user_id, out, task_key=old.statistics_key, queued_at=old.queued_at,
        )
        if change == "none":
            assert result is not None and result.task_key == old.statistics_key and result.checkpoint is None
        else:
            assert result is None
