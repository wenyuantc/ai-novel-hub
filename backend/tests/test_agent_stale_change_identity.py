"""红灯证据：旧客户端持有的整数 ID 尚不能证明原提案或原任务轮次。"""

import pytest

from app.models import AgentChange, Task
from app.services.agent import service
from tests.helpers import create_project, register


@pytest.mark.parametrize("action", ["change", "turn", "stream"])
async def test_old_client_id_does_not_adopt_recreated_agent_target(app, client, monkeypatch, action):
    owner = await register(client, f"stale-client-{action}")
    project = await create_project(client)
    project_id = project["id"]
    base = f"/api/v1/projects/{project_id}/agent"
    conversation = (await client.post(f"{base}/conversations", json={})).json()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project_id, kind="agent.turn", status="paused",
                    input={"request": {"conversation_id": conversation["id"]}})
        db.add(task)
        await db.flush()
        original = AgentChange(conversation_id=conversation["id"], project_id=project_id, turn_task_id=task.id,
                               turn_task_key=task.statistics_key,
                               tool_call_id="same-call", tool_name="test", kind="small", status="proposed",
                               arguments={"text": "旧提案"})
        db.add(original)
        await db.commit()
        old_id, created_at, task_id, task_key = original.id, original.created_at, task.id, task.statistics_key
    detail = await client.get(f"{base}/conversations/{conversation['id']}")
    assert detail.status_code == 200, detail.text
    old_view = detail.json()["changes"][0]
    async with app.state.sessionmaker() as db:
        original = await db.get(AgentChange, old_id)
        task = await db.get(Task, task_id)
        if action == "change":
            await db.delete(original)
            await db.flush()
            db.add(AgentChange(id=old_id, conversation_id=conversation["id"], project_id=project_id,
                               turn_task_id=task_id, turn_task_key=task_key,
                               tool_call_id="same-call", tool_name="test", kind="small",
                               status="proposed", created_at=created_at, arguments={"text": "新提案"}))
        else:
            await db.delete(original)
            await db.delete(task)
            await db.flush()
            replacement = Task(id=task_id, user_id=owner["id"], project_id=project_id, kind="agent.turn",
                               status="succeeded" if action == "stream" else "paused", output_text="新任务正文",
                               input={"request": {"conversation_id": conversation["id"]}})
            db.add(replacement)
            await db.flush()
            db.add(AgentChange(id=old_id, conversation_id=conversation["id"], project_id=project_id,
                               turn_task_id=task_id, turn_task_key=replacement.statistics_key,
                               tool_call_id="same-call", tool_name="test", kind="small",
                               status="proposed", created_at=created_at, arguments={"text": "新提案"}))
        await db.commit()

    async def skip_resume(*_args):
        return None

    monkeypatch.setattr(service, "maybe_resume", skip_resume)
    if action == "change":
        response = await client.post(f"{base}/changes/{old_id}/reject", json={
            "change_key": old_view["change_key"], "view_version": old_view["view_version"],
        })
    elif action == "turn":
        response = await client.post(f"{base}/turns/{task_id}/reject-all", json={
            "task_key": task_key, "changes": [{"id": old_id, "change_key": old_view["change_key"],
                                                "view_version": old_view["view_version"]}],
        })
    else:
        response = await client.get(f"/api/v1/tasks/{task_id}/stream", params={"task_key": task_key})
    assert response.status_code in (404, 409, 422, 428), response.text
    async with app.state.sessionmaker() as db:
        replacement = await db.get(AgentChange, old_id)
        assert replacement.status == "proposed"
