"""事件发布与持久刷写必须处于原执行身份的同一门禁中。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.providers import AIProviderError
from app.models import Task, User
from app.services import task_service
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.task_engine.engine import RECEIPT_FINALIZED_NOTE
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.hub import TaskHub
from tests.helpers import create_project, register, wait_until
from tests.test_tasks import _generate, _setup
from tests.worker_helpers import fence_worker


async def _running(app, client, *, with_worker=False):
    await app.state.task_engine.stop()
    worker_key = await app.state.task_engine.lease.register() if with_worker else None
    owner = await register(client, "event-guard-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        user = await db.get(User, owner["id"])
        task = Task(user_id=user.id, project_id=project["id"], kind="wizard.world", status="queued", step_cursor=0,
                    input={"request": {"project_id": project["id"]},
                           "_task_actor_token_version": user.token_version,
                           "_wizard_actor_token_version": user.token_version})
        db.add(task)
        await db.flush()
        execution = from_queued_task(task, str(uuid4()), worker_key=worker_key)
        task.status, task.execution_key = "running", execution.execution_key
        task.worker_key = worker_key
        await db.commit()
        return task, execution


class _Handler:
    execution_identity_aware = True

    def __init__(self, mode):
        self.mode = mode

    async def run(self, job, ctx):
        yield "delta", {"text": "原模型私密正文"}
        yield "done", {"answer": "原模型结果"}

    def progress(self, task, text):
        return 1

    async def verify_event(self, db, task, *, job, execution, current_execution, event, data):
        if self.mode == "commit":
            await db.commit()
        elif self.mode == "rollback":
            await db.rollback()
        elif self.mode == "dirty":
            task.title = "不应提交"
        elif self.mode == "raise":
            raise RuntimeError("不得泄露的异常")


@pytest.mark.parametrize("mode", ["commit", "rollback", "dirty", "raise"])
async def test_event_hook_cannot_release_or_modify_transaction_before_publish(app, client, mode):
    task, execution = await _running(app, client)
    hub = TaskHub(task.id, statistics_key=task.statistics_key, queued_at=task.queued_at,
                  execution_key=execution.execution_key, status="running")
    _snapshot, subscriber = hub.subscribe()
    with pytest.raises(VisibilityPolicyError):
        await app.state.task_engine._consume(task, _Handler(mode), hub, object(), execution=execution)
    assert hub.text == "" and hub.result is None and subscriber.queue.empty()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task.id)).title != "不应提交"


async def test_event_hook_authorizes_each_frame_in_order(app, client):
    task, execution = await _running(app, client)
    hub = TaskHub(task.id, statistics_key=task.statistics_key, queued_at=task.queued_at,
                  execution_key=execution.execution_key, status="running")
    _snapshot, subscriber = hub.subscribe()
    assert await app.state.task_engine._consume(task, _Handler("ok"), hub, object(), execution=execution) == ""
    frames = []
    while not subscriber.queue.empty():
        frames.append(subscriber.queue.get_nowait())
    assert frames[0] == ("delta", {"text": "原模型私密正文"})
    assert hub.result == {"answer": "原模型结果"}


async def test_event_rejects_revoked_actor_before_publication(app, client):
    task, execution = await _running(app, client)
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User).where(User.id == execution.actor_id))
        user.token_version += 1
        await db.commit()
    hub = TaskHub(task.id, statistics_key=task.statistics_key, queued_at=task.queued_at,
                  execution_key=execution.execution_key, status="running")
    _snapshot, subscriber = hub.subscribe()
    with pytest.raises(VisibilityPolicyError):
        await app.state.task_engine._consume(task, _Handler("ok"), hub, object(), execution=execution)
    assert hub.text == "" and subscriber.queue.empty()


@pytest.mark.parametrize("lost", ["local", "fenced", "adopted"])
async def test_lost_worker_cannot_publish_flush_or_clear_success(app, second_app, client, lost):
    task, execution = await _running(app, client, with_worker=True)
    engine, maker = app.state.task_engine, app.state.sessionmaker
    hub = TaskHub(task.id, statistics_key=task.statistics_key, queued_at=task.queued_at,
                  execution_key=execution.execution_key, status="running")
    handler = _Handler("ok")
    async with maker() as db:
        assert (await engine._event_task(db, execution, execution, "delta")).id == task.id
    if lost == "local":
        engine.lease.invalidate()
    elif lost == "fenced":
        await fence_worker(engine, mirror=False)
    else:
        next_owner = await second_app.state.task_engine.lease.register()
        async with maker.begin() as db:
            await db.execute(update(Task).where(Task.id == task.id).values(worker_key=next_owner))

    _, subscriber = hub.subscribe()
    with pytest.raises(VisibilityPolicyError):
        await engine._consume(task, handler, hub, object(), execution=execution)
    assert subscriber.queue.empty() and not hub.text
    hub.append("迟到的执行者")
    with pytest.raises(VisibilityPolicyError):
        await engine._flush(hub, handler=handler, job=object(), execution=execution)
    async with maker.begin() as db:
        row = await db.get(Task, task.id)
        assert not row.output_text
        row.status = "succeeded"
    assert not await engine._mark_followup_warning(task, "迟到的警告")
    await engine.clear_finished_execution(task)
    async with maker() as db:
        row = await db.get(Task, task.id)
        assert row.execution_key == execution.execution_key and not row.error


@pytest.mark.parametrize("outcome", ["success", "failure", "receipt"])
async def test_fenced_finalizer_cannot_prune_previous_history(app, client, fake_llm, monkeypatch, outcome):
    engine, maker = app.state.task_engine, app.state.sessionmaker
    project, _, (chapter,) = await _setup(client)
    async with maker.begin() as db:
        history = Task(user_id=await db.scalar(select(User.id)), project_id=project["id"],
                       kind="outline.continue", status="interrupted")
        db.add(history)
        await db.flush()
        history_id = history.id

    if outcome == "receipt":
        # 正文和收据已提交，普通终态的 CAS 未命中，由引擎走按原收据收尾的入口。
        original = engine.complete_state

        async def require_receipt(task, **kwargs):
            if kwargs["status"] == "succeeded":
                return task, False
            return await original(task, **kwargs)

        monkeypatch.setattr(engine, "complete_state", require_receipt)

    prune = task_service.prune_finished
    fenced = asyncio.Event()

    async def fence_before_prune(db, user_id, **kwargs):
        if not fenced.is_set():
            await fence_worker(engine, mirror=False)
            monkeypatch.setattr(task_service, "MAX_FINISHED_PER_USER", 0)
            fenced.set()
        await prune(db, user_id, **kwargs)

    monkeypatch.setattr(task_service, "prune_finished", fence_before_prune)
    fake_llm.push(AIProviderError("模型失败") if outcome == "failure" else "完整正文")
    response = await _generate(client, chapter)
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    await asyncio.wait_for(fenced.wait(), 8)
    await wait_until(lambda: task_id not in engine._running)
    async with maker() as db:
        task = await db.get(Task, task_id)
        assert task.status == ("failed" if outcome == "failure" else "succeeded")
        if outcome == "receipt":
            assert task.error == RECEIPT_FINALIZED_NOTE and task.execution_key is None
        assert await db.get(Task, history_id) is not None
