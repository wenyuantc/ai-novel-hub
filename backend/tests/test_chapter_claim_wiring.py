"""单章任务受理、领取和取消必须携带同一章节占用身份。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Task
from app.services import chapter_claim, generation_service, task_statistics_service
from app.services.task_engine.handlers import chapter as chapter_handler
from tests.helpers import create_project, register
from tests.test_tasks import _generate, _setup


async def _queued(app, client):
    await app.state.task_engine.stop()
    _, _, (chapter,) = await _setup(client)
    response = await _generate(client, chapter)
    assert response.status_code == 202, response.text
    return chapter["id"], response.json()["id"], response.json()["task_key"]


async def test_accept_binds_chapter_to_queued_task_in_same_transaction(app, client):
    chapter_id, task_id, _ = await _queued(app, client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        chapter = await db.get(Chapter, chapter_id)
        claim = task.input["_chapter_claim"]
        assert task.status == "queued" and task.execution_key is None and task.queued_at
        assert chapter.status == "generating"
        assert chapter.generation_claim_key == claim["claim_key"]
        assert chapter.generation_claim_task_key == task.statistics_key
        assert chapter.generation_claim_prev_status == claim["prev_status"]


async def test_accept_claim_failure_rolls_back_task_and_chapter(app, client, monkeypatch):
    await app.state.task_engine.stop()
    _, _, (chapter,) = await _setup(client)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("抢占失败")

    monkeypatch.setattr(chapter_claim, "claim_queued_chapter", fail)
    with pytest.raises(RuntimeError, match="抢占失败"):
        await _generate(client, chapter)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Task.id).where(Task.chapter_id == chapter["id"])) is None
        row = await db.get(Chapter, chapter["id"])
        assert row.status == "draft" and row.generation_claim_key is None


async def test_queued_cancel_releases_original_claim_atomically(app, client):
    chapter_id, task_id, task_key = await _queued(app, client)
    response = await client.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": task_key})
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "cancelled" and task.execution_key is None
        assert "_chapter_claim" not in task.input
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_queued_cancel_failed_release_rolls_back_terminal_state(app, client, monkeypatch):
    chapter_id, task_id, task_key = await _queued(app, client)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("释放失败")

    monkeypatch.setattr(chapter_claim, "release_chapter_claim", fail)
    with pytest.raises(RuntimeError, match="释放失败"):
        await client.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": task_key})
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "queued" and task.execution_key is None
        assert chapter.status == "generating" and chapter.generation_claim_key


async def test_queued_cancel_commit_confirmation_loss_uses_same_claim(app, client, monkeypatch):
    chapter_id, task_id, task_key = await _queued(app, client)
    original_commit, lost = AsyncSession.commit, False

    async def lose_after_commit(db):
        nonlocal lost
        if not lost and await db.scalar(select(Task.status).where(Task.id == task_id)) == "cancelled":
            lost = True
            await original_commit(db)
            raise RuntimeError("取消提交确认丢失")
        await original_commit(db)

    monkeypatch.setattr(AsyncSession, "commit", lose_after_commit)
    response = await client.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": task_key})
    assert lost and response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "cancelled" and "_chapter_claim" not in task.input
        assert chapter.status == "draft" and chapter.generation_claim_key is None


async def test_claim_failure_rolls_back_task_running_transition(app, client, monkeypatch):
    chapter_id, task_id, _ = await _queued(app, client)
    engine = app.state.task_engine
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task_key, queued_at = task.statistics_key, task.queued_at
        claim_key = (await db.get(Chapter, chapter_id)).generation_claim_key

    async def fail(*_args, **_kwargs):
        raise RuntimeError("激活失败")

    monkeypatch.setattr(chapter_claim, "activate_queued_chapter", fail)
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    with pytest.raises(RuntimeError, match="激活失败"):
        await engine._claim_and_run(task_id, task_key)
    async with app.state.sessionmaker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "queued" and task.execution_key is None
        assert chapter.status == "generating" and chapter.generation_claim_key == claim_key


async def test_claim_passes_original_running_identity_to_execution(app, client, monkeypatch):
    chapter_id, task_id, _ = await _queued(app, client)
    engine = app.state.task_engine
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task_key, queued_at = task.statistics_key, task.queued_at
        claim_key = (await db.get(Chapter, chapter_id)).generation_claim_key
    started = []

    async def run(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(engine, "_run", run)
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    assert len(started) == 1
    claim = started[0][1]["claim"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        assert claim.phase == "running" and claim.task_key == task_key
        assert claim.claim_key == claim_key and claim.execution_key == task.execution_key
        assert task.status == "running" and claim.execution_key is not None


async def test_unknown_claim_commit_requires_original_chapter_identity(app, client, monkeypatch):
    chapter_id, task_id, _ = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with maker() as db:
        task = await db.get(Task, task_id)
        task_key, queued_at = task.statistics_key, task.queued_at
    original_commit = AsyncSession.commit
    lost, started = False, []

    async def lose_after_replacement(db):
        nonlocal lost
        if not lost and await db.scalar(select(Task.status).where(Task.id == task_id)) == "running":
            lost = True
            await original_commit(db)
            async with maker() as other:
                await other.execute(update(Chapter).where(Chapter.id == chapter_id).values(
                    generation_claim_key=str(uuid4()),
                ))
                await original_commit(other)
            raise RuntimeError("领取提交确认丢失")
        await original_commit(db)

    async def run(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(AsyncSession, "commit", lose_after_replacement)
    monkeypatch.setattr(engine, "_run", run)
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    assert lost and not started


async def test_unknown_claim_commit_accepts_only_own_persisted_claim(app, client, monkeypatch):
    chapter_id, task_id, _ = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with maker() as db:
        task = await db.get(Task, task_id)
        task_key, queued_at = task.statistics_key, task.queued_at
        claim_key = (await db.get(Chapter, chapter_id)).generation_claim_key
    original_commit = AsyncSession.commit
    lost, started = False, []

    async def lose_after_commit(db):
        nonlocal lost
        if not lost and await db.scalar(select(Task.status).where(Task.id == task_id)) == "running":
            lost = True
            await original_commit(db)
            raise RuntimeError("领取提交确认丢失")
        await original_commit(db)

    async def run(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(AsyncSession, "commit", lose_after_commit)
    monkeypatch.setattr(engine, "_run", run)
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    assert lost and len(started) == 1
    claim = started[0][1]["claim"]
    assert claim.task_key == task_key and claim.claim_key == claim_key and claim.execution_key


async def test_old_queued_cleanup_cannot_release_new_chapter_claim(app, client):
    chapter_id, first_id, task_key = await _queued(app, client)
    async with app.state.sessionmaker() as db:
        first = await db.get(Task, first_id)
        old = chapter_handler.capture_queued_claim(first)
    response = await client.post(f"/api/v1/tasks/{first_id}/cancel", params={"task_key": task_key})
    assert response.status_code == 200, response.text
    response = await _generate(client, {"id": chapter_id})
    assert response.status_code == 202, response.text
    second_id = response.json()["id"]
    async with app.state.sessionmaker() as db:
        second, chapter = await db.get(Task, second_id), await db.get(Chapter, chapter_id)
        current = chapter.generation_claim_key
        assert current and current != old.claim_key
        assert not await chapter_claim.release_chapter_claim(db, old)
        await db.commit()
        assert chapter.status == "generating" and chapter.generation_claim_key == current
        assert second.input["_chapter_claim"]["claim_key"] == current


async def test_replaced_queued_claim_fails_old_task_without_touching_new_batch(app, client):
    chapter_id, task_id, _ = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with maker() as db:
        old = await db.get(Task, task_id)
        task_key, queued_at, project_id, actor_id = old.statistics_key, old.queued_at, old.project_id, old.user_id
    reset = await client.patch(f"/api/v1/projects/{project_id}/chapters/{chapter_id}", json={"status": "draft"})
    assert reset.status_code == 200, reset.text
    async with maker() as db:
        batch = Task(user_id=actor_id, project_id=project_id, kind="chapter.batch", status="running",
                     resource_key=f"batch:{project_id}", execution_key=str(uuid4()),
                     input={"request": {"project_id": project_id}, "chapter_ids": [chapter_id]},
                     checkpoint={"phase": "generate", "current": None, "items": []})
        db.add(batch)
        await db.flush()
        current = await chapter_claim.claim_batch_chapter(
            db, task_id=batch.id, task_key=batch.statistics_key, actor_id=actor_id, project_id=project_id,
            chapter_id=chapter_id, queued_at=batch.queued_at, grant_identity=None, execution_key=batch.execution_key,
        )
        await db.commit()
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    async with maker() as db:
        old, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert old.status == "failed" and old.execution_key is None
        assert chapter.status == "generating" and chapter.generation_claim_key == current.claim_key


async def test_unclaimed_old_queued_task_cannot_run_or_release_chapter(app, client, monkeypatch):
    chapter_id, task_id, _ = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with maker() as db:
        task = await db.get(Task, task_id)
        task_key, queued_at = task.statistics_key, task.queued_at
        task.input = {key: value for key, value in task.input.items() if key != "_chapter_claim"}
        await db.commit()
    started = []

    async def run(*args, **kwargs):
        started.append((args, kwargs))

    monkeypatch.setattr(engine, "_run", run)
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    response = await client.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": task_key})
    assert response.status_code == 409 and not started
    async with maker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "queued" and task.execution_key is None
        assert chapter.status == "generating" and chapter.generation_claim_key


async def test_uncertain_save_retains_running_execution_and_claim(app, client, monkeypatch):
    chapter_id, task_id, _ = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with maker() as db:
        task = await db.get(Task, task_id)
        task_key, queued_at = task.statistics_key, task.queued_at
    handler = chapter_handler.ChapterHandler()

    async def build(*_args, **_kwargs):
        return object()

    async def run(*_args):
        # 替身 job 没有执行身份，不能先发布需 verify_event 的事件；直接模拟保存提交结果未知。
        if False:  # pragma: no cover - 保持异步生成器形态
            yield "stage", {"stage": "保存中"}
        raise generation_service.ChapterCommitUncertain("不泄露内部状态")

    from app.services.task_engine import engine as engine_module

    monkeypatch.setattr(handler, "build_job", build)
    monkeypatch.setattr(handler, "run", run)
    monkeypatch.setattr(engine_module, "get_handler", lambda _kind: handler)
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    async with maker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "running" and task.execution_key
        assert chapter.status == "generating" and chapter.generation_claim_key
        assert task.input["_chapter_claim"]["claim_key"] == chapter.generation_claim_key
        assert task.project_id in engine._abort_failures


async def test_recovery_retains_even_malformed_completion_receipt(app, client):
    chapter_id, task_id, _ = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    execution = str(uuid4())
    async with maker() as db:
        task = await db.get(Task, task_id)
        task.status, task.execution_key = "running", execution
        task.checkpoint = {"_chapter_claim": {"damaged": True}}
        await db.commit()
    await engine.recover()
    async with maker() as db:
        task, chapter = await db.get(Task, task_id), await db.get(Chapter, chapter_id)
        assert task.status == "running" and task.execution_key == execution
        assert chapter.status == "generating" and chapter.generation_claim_key
        assert task.project_id in engine._abort_failures


@pytest.mark.parametrize("valid", [True, False])
async def test_finish_only_clears_matching_admin_chapter_intent(app, client, monkeypatch, valid):
    _, task_id, task_key = await _queued(app, client)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with maker() as db:
        queued = await db.get(Task, task_id)
        queued_at = queued.queued_at
    captured = []

    async def run(*_args, **kwargs):
        captured.append(kwargs["claim"])

    monkeypatch.setattr(engine, "_run", run)
    engine._stopping = False
    engine._running_rounds[task_id] = queued_at
    await engine._claim_and_run(task_id, task_key)
    claim = captured[0]
    async with maker() as db:
        original = await db.get(Task, task_id)
    from app.services.task_engine.cancellation import _CHAPTER_INTENT, chapter_cancel_intent

    operation = str(uuid4())
    intent = chapter_cancel_intent(original, operation)
    assert intent is not None
    stored = dict(intent)
    if not valid:
        stored["claim_key"] = str(uuid4())
    async with maker() as db:
        current = await db.get(Task, task_id)
        current.admin_cancel_operation_id = operation
        current.input = {**current.input, _CHAPTER_INTENT: stored}
        await db.commit()
    if valid:
        async with maker() as db:
            assert await chapter_claim.release_chapter_claim(db, claim)
            await db.commit()
    row, committed = await engine.complete_state(
        original, expected_status="running", status="cancelled", result=None, error="任务已取消",
        text="", progress=0, abort_ok=True,
    )
    assert committed is valid
    async with maker() as db:
        current = await db.get(Task, task_id)
        if valid:
            assert row.status == "cancelled" and current.admin_cancel_operation_id is None
            assert _CHAPTER_INTENT not in current.input
        else:
            assert current.status == "running" and current.admin_cancel_operation_id == operation
            assert current.input[_CHAPTER_INTENT] == stored


@pytest.mark.parametrize("replacement", ["attempt", "cursor", "json_type", "none"])
async def test_batch_terminal_cas_uses_original_checkpoint_and_cursor(app, client, replacement):
    await app.state.task_engine.stop()
    user = await register(client, "batch-terminal-owner")
    project = await create_project(client)
    checkpoint = {"phase": "generate", "current": None, "items": [{"chapter_id": 1, "attempt": 1}]}
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=project["id"], kind="chapter.batch", status="running",
                    resource_key=f"batch:{project['id']}", execution_key=str(uuid4()),
                    checkpoint=deepcopy(checkpoint), step_cursor=0)
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
    task._batch_job = SimpleNamespace(persisted_checkpoint=deepcopy(checkpoint), persisted_cursor=0)
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task.id)
        if replacement == "attempt":
            current.checkpoint = {**checkpoint, "current": {"claim_key": str(uuid4())}}
        elif replacement == "cursor":
            current.step_cursor = 1
        elif replacement == "json_type":
            # ORM 的 Python 相等比较把 True 当成 1；用 SQL 明确写入不同的 JSON 类型。
            await db.execute(update(Task).where(Task.id == task.id).values(
                checkpoint={**checkpoint, "items": [{"chapter_id": 1, "attempt": True}]},
            ))
        await db.commit()
    row, committed = await app.state.task_engine.complete_state(
        task, expected_status="running", status="failed", result=None, error="安全结束", text="", progress=0,
        abort_ok=True,
    )
    assert committed is (replacement == "none")
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task.id)
        assert current.status == ("failed" if replacement == "none" else "running")
        if replacement == "attempt":
            assert current.checkpoint["current"]["claim_key"]
        assert row.id == task.id
