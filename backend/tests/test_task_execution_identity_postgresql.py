"""PostgreSQL 两连接验证领取后不能以替换行/归属/类型续签执行体。"""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, update

from app.models import Task
from app.services.task_engine.registry import get_handler
from tests.helpers import create_project, register
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps  # noqa: F401


@pytest.mark.parametrize("change", ["task", "actor", "project", "kind", "execution", "input", "checkpoint"])
async def test_claimed_execution_rejects_replacement_before_build(postgres_collaboration_apps, monkeypatch, change):  # noqa: F811
    app, _ = postgres_collaboration_apps
    engine, maker = app.state.task_engine, app.state.sessionmaker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        owner = await register(client, f"identity-owner-{change}")
        project = await create_project(client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as other_client:
        other = await register(other_client, f"identity-other-{change}")
        other_project = await create_project(other_client)
    async with maker() as db:
        task = Task(user_id=owner["id"], project_id=project["id"], kind="wizard.world", status="queued",
                    input={"request": {"project_id": project["id"]}, "_task_actor_token_version": 0,
                           "_wizard_actor_token_version": 0})
        db.add(task)
        await db.commit()
        task_id, task_key, queued_at = task.id, task.statistics_key, task.queued_at
    entered = []

    async def forbidden_build(*_args, **_kwargs):
        entered.append(True)
        raise AssertionError("不得采用替换后的任务构建执行体")

    monkeypatch.setattr(get_handler("wizard.world"), "build_job", forbidden_build)
    actual_run = engine._run

    async def replace_before_run(task_id, hub, execution, **kwargs):
        async with maker() as db:
            if change == "task":
                await db.execute(delete(Task).where(Task.id == task_id))
                db.add(Task(id=task_id, user_id=owner["id"], project_id=project["id"], kind="wizard.world",
                            status="running", execution_key=str(uuid4()), input={"request": {}}))
            else:
                values = {
                    "actor": {"user_id": other["id"]}, "project": {"project_id": other_project["id"]},
                    "kind": {"kind": "wizard.characters"}, "execution": {"execution_key": str(uuid4())},
                    "input": {"input": {"request": {"project_id": project["id"]}}},
                    "checkpoint": {"checkpoint": {"replacement": True}},
                }[change]
                await db.execute(update(Task).where(Task.id == task_id).values(**values))
            await db.commit()
        await actual_run(task_id, hub, execution, **kwargs)

    monkeypatch.setattr(engine, "_run", replace_before_run)
    engine._running_rounds[task_id] = queued_at
    engine._stopping = False
    await engine._claim_and_run(task_id, task_key)
    assert not entered
