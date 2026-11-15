"""事件发布与持久刷写必须处于原执行身份的同一门禁中。"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Task, User
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.hub import TaskHub
from tests.helpers import create_project, register


async def _running(app, client):
    await app.state.task_engine.stop()
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
        execution = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", execution.execution_key
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
