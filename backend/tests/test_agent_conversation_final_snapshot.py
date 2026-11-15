"""会话详情最后一次读取仍须绑定原消息、会话与任务。"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import AgentConversation, AgentMessage, Task, User
from app.services import personal_projection
from tests.helpers import register
from tests.test_agent_action_identity import _setup


@pytest.mark.parametrize("replacement", ["token", "conversation", "message", "task"])
async def test_conversation_detail_rechecks_entire_final_snapshot(app, client, make_client, monkeypatch, replacement):
    base, conversation_id, task_id, _ = await _setup(app, client, monkeypatch)
    owner = (await client.get("/api/v1/auth/me")).json()
    other = await register(make_client(), f"detail-{replacement}")
    async with app.state.sessionmaker() as db:
        (await db.get(Task, task_id)).resource_key = f"agent:{conversation_id}"
        message = AgentMessage(conversation_id=conversation_id, role="user", content="旧消息私人内容")
        db.add(message)
        await db.commit()
        message_id = message.id

    original = personal_projection.project_agent_payload
    swapped = False

    async def replace_before_final(db, user_id, project_id, payload, proofs):
        nonlocal swapped
        if not swapped and isinstance(payload, dict) and "conversation" in payload:
            swapped = True
            async with app.state.sessionmaker() as writer:
                if replacement == "token":
                    (await writer.get(User, owner["id"])).token_version += 1
                elif replacement == "conversation":
                    (await writer.get(AgentConversation, conversation_id)).user_id = other["id"]
                elif replacement == "message":
                    (await writer.get(AgentMessage, message_id)).content = "替换后的消息"
                else:
                    (await writer.get(Task, task_id)).statistics_key = str(uuid4())
                await writer.commit()
        return await original(db, user_id, project_id, payload, proofs)

    monkeypatch.setattr(personal_projection, "project_agent_payload", replace_before_final)
    response = await client.get(f"{base}/conversations/{conversation_id}")
    assert swapped and response.status_code == (401 if replacement == "token" else 404), response.text
    assert "旧消息私人内容" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AgentMessage.id).where(AgentMessage.id == message_id)) == message_id


async def test_conversation_active_task_keeps_internal_checkpoint_private(app, client, monkeypatch):
    base, conversation_id, task_id, _ = await _setup(app, client, monkeypatch)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task.resource_key = f"agent:{conversation_id}"
        task.checkpoint = {"visible": "进度", "_external_snapshot_mac": "private-mac"}
        await db.commit()
    response = await client.get(f"{base}/conversations/{conversation_id}")
    assert response.status_code == 200, response.text
    assert response.json()["active_task"]["checkpoint"] == {"visible": "进度"}
    assert "private-mac" not in response.text
