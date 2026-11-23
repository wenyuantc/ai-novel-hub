"""孤立向导缺原执行结果证明时不能清理、重发或丢弃原持久证据。"""

from uuid import uuid4

import pytest

from app.models import Task
from tests.helpers import create_project, register


async def _orphan(app, client, checkpoint, *, status="running", admin=False):
    user = await register(client, "wizard-orphan")
    project = await create_project(client)
    await app.state.task_engine.stop()
    execution = str(uuid4()) if status == "running" else None
    operation = str(uuid4()) if admin else None
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=project["id"], kind="wizard.characters", status=status,
                    execution_key=execution, checkpoint=checkpoint,
                    admin_cancel_operation_id=operation, input={"request": {"project_id": project["id"]}})
        db.add(task)
        await db.commit()
        return task.id, task.statistics_key, execution, operation, project["id"]


@pytest.mark.parametrize("checkpoint", [None, {"_wizard_pending": None}, {"_wizard_completion": {}}])
async def test_recovery_keeps_orphan_wizard_running_and_blocks_retry(app, client, checkpoint):
    task_id, task_key, execution, _, project_id = await _orphan(app, client, checkpoint)
    engine = app.state.task_engine
    await engine.start()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert (row.status, row.execution_key, row.checkpoint) == ("running", execution, checkpoint)
        assert engine.execution_is_uncertain(row) and engine.has_cleanup_failure(project_id)
    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": task_key})
    retried = await client.post(f"/api/v1/tasks/{task_id}/retry", params={"task_key": task_key})
    assert cancelled.status_code == 409 and retried.status_code == 409


@pytest.mark.parametrize("checkpoint", [None, {"_wizard_pending": None}, {"_wizard_completion": {}}])
async def test_admin_drain_keeps_unproven_orphan_wizard(app, client, checkpoint):
    task_id, task_key, execution, operation, project_id = await _orphan(app, client, checkpoint, admin=True)
    engine = app.state.task_engine
    await engine.admin_cancellations._drain(task_key, operation)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert (row.status, row.execution_key, row.checkpoint, row.admin_cancel_operation_id) == (
            "running", execution, checkpoint, operation,
        )
        assert engine.execution_is_uncertain(row) and engine.has_cleanup_failure(project_id)


async def test_queued_wizard_with_damaged_pending_cannot_be_cancelled(app, client):
    task_id, task_key, _, _, _ = await _orphan(app, client, {"_wizard_pending": None}, status="queued")
    response = await client.post(f"/api/v1/tasks/{task_id}/cancel", params={"task_key": task_key})
    assert response.status_code == 409
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert row.status == "queued" and row.execution_key is None and row.checkpoint == {"_wizard_pending": None}
