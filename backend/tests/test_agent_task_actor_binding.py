"""Agent 入队固定原登录代次，重试不得用后来的凭据替换。"""

import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.models import AgentMessage, Task, User
from app.services import task_service
from app.services.authorization_service import AuthenticatedActor
from tests.test_agent_runtime import setup_agent


async def _queued_agent(app, client):
    project, _base, url = await setup_agent(client)
    await app.state.task_engine.stop()
    response = await client.post(url + "/messages", json={"content": "原任务"})
    assert response.status_code == 202, response.text
    return project, url, response.json()["task"]


async def test_agent_creation_binds_authenticated_token_and_hides_it(app, client):
    _project, url, ref = await _queued_agent(app, client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, ref["id"])
        user = await db.get(User, task.user_id)
        assert task.input["_agent_actor_token_version"] == user.token_version
        assert type(task.input["_agent_actor_token_version"]) is int
    detail = await client.get(url)
    assert detail.status_code == 200 and "_agent_actor_token_version" not in detail.text


@pytest.mark.parametrize("cause", ["missing_actor", "forged", "nested_forged"])
async def test_agent_admission_rejects_missing_or_client_supplied_token(app, client, cause):
    project, _base, url = await setup_agent(client)
    await app.state.task_engine.stop()
    conversation_id = int(url.rsplit("/", 1)[1])
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User))
        if cause != "missing_actor":
            db.info["authenticated_actor"] = AuthenticatedActor(user.id, user.token_version)
        params = {"project_id": project["id"], "conversation_id": conversation_id, "content": "不得入队"}
        if cause == "forged":
            params["_agent_actor_token_version"] = 999
        elif cause == "nested_forged":
            params["page_context"] = {"_agent_actor_token_version": 999}
        with pytest.raises(ConflictError):
            await task_service.create_task(db, app.state.task_engine, user.id, "agent.turn", params)
        assert await db.scalar(select(func.count()).select_from(AgentMessage)) == 0


@pytest.mark.parametrize("value", [None, True, -1, "0", 200])
async def test_agent_retry_rejects_unproven_or_replaced_original_token(app, client, value):
    _project, _url, ref = await _queued_agent(app, client)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, ref["id"])
        task.status = "failed"
        payload = dict(task.input)
        if value is None:
            payload.pop("_agent_actor_token_version", None)
        else:
            payload["_agent_actor_token_version"] = value
        task.input = payload
        await db.commit()
        actor_id = task.user_id
    async with app.state.sessionmaker() as db:
        user = await db.get(User, actor_id)
        db.info["authenticated_actor"] = AuthenticatedActor(user.id, user.token_version)
        with pytest.raises(ConflictError):
            await task_service.retry_task(db, app.state.task_engine, actor_id, ref["id"],
                                          expected_task_key=ref["task_key"])
        assert await db.scalar(select(func.count()).select_from(Task)) == 1
