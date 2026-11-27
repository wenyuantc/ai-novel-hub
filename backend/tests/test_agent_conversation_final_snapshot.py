"""会话详情最后一次读取仍须绑定原消息、会话与任务。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import AgentChange, AgentConversation, AgentMessage, ProjectCollaborator, Task, User
from app.services import personal_projection
from tests.helpers import create_project, register
from tests.schema_helpers import isolated_worker_database
from tests.test_agent_action_identity import _setup
from tests.test_agent_entries_e4id import _invite


@pytest.fixture(params=("sqlite", "postgresql"))
async def settings_overrides(monkeypatch, request):
    if request.param == "sqlite":
        yield {}
    else:
        async with isolated_worker_database(monkeypatch, required=True) as overrides:
            yield overrides


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


@pytest.mark.parametrize("role", ["owner", "editor"])
@pytest.mark.parametrize("advance", ["progress", "status", "checkpoint", "step_cursor", "preview", "finish", "revoke"])
async def test_conversation_detail_during_turn_update(app, client, make_client, monkeypatch, role, advance):
    owner = await register(client, "snapshot-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent/conversations"
    actor, reader, grant = owner, client, {}
    if role == "editor":
        reader = make_client()
        actor = await register(reader, "snapshot-editor")
        member = await _invite(client, reader, project["id"], "snapshot-editor")
        grant = {"_collaboration_grant": {k: member[k] for k in ("invitation_id", "revision")}}
    cid = (await reader.post(base, json={})).json()["id"]
    async with app.state.sessionmaker.begin() as db:
        task = Task(user_id=actor["id"], project_id=project["id"], kind="agent.turn", status="paused",
                    resource_key=f"agent:{cid}", input={"request": {"conversation_id": cid}, **grant})
        db.add(task)
        await db.flush()
        tid, key = task.id, task.statistics_key
        db.add(AgentChange(conversation_id=cid, project_id=project["id"], turn_task_id=tid, turn_task_key=key,
                           tool_call_id="call", tool_name="test", kind="small", status="proposed", seq=1))

    if advance == "preview":
        initial = (await reader.get(f"{base}/{cid}")).json()["changes"][0]
    project_payload = personal_projection.project_agent_payload
    advanced = False

    async def advance_before_projection(db, user_id, project_id, payload, proofs):
        nonlocal advanced
        if not advanced:
            advanced = True
            async with app.state.sessionmaker.begin() as writer:
                task = await writer.get(Task, tid)
                if advance == "progress":
                    task.progress = 42
                elif advance == "status":
                    task.status = "succeeded"
                elif advance == "step_cursor":
                    task.step_cursor += 1
                elif advance == "preview":
                    change = await writer.scalar(select(AgentChange).where(AgentChange.turn_task_id == tid))
                    change.preview = {"title": "已刷新", "guards": {"revision": "private-proof"}}
                elif advance == "finish":
                    task.status = "succeeded"
                    (await writer.get(AgentConversation, cid)).last_message_at = datetime.now(UTC)
                    writer.add(AgentMessage(conversation_id=cid, turn_task_id=tid, turn_task_key=key,
                                            role="assistant", content="已完成"))
                    change = await writer.scalar(select(AgentChange).where(AgentChange.turn_task_id == tid))
                    change.status = "executed"
                else:
                    task.checkpoint = {"step": 2, "_external_snapshot_mac": "private-proof"}
                    if advance == "revoke":
                        if role == "editor":
                            member = await writer.scalar(select(ProjectCollaborator).where(
                                ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id,
                            ))
                            member.status = "revoked"
                        else:
                            (await writer.get(User, user_id)).token_version += 1
        return await project_payload(db, user_id, project_id, payload, proofs)

    monkeypatch.setattr(personal_projection, "project_agent_payload", advance_before_projection)
    response = await reader.get(f"{base}/{cid}")
    assert advanced
    if advance == "revoke":
        assert response.status_code == (404 if role == "editor" else 401), response.text
        assert "private-proof" not in response.text
        return
    assert response.status_code == 200, response.text
    result = response.json()
    assert "private-proof" not in response.text
    if advance in ("status", "finish"):
        assert result["active_task"] is None
        if advance == "finish":
            assert [item["content"] for item in result["messages"]] == ["已完成"]
            assert [item["status"] for item in result["changes"]] == ["executed"]
    else:
        assert result["active_task"]["task_key"] == key
        if advance == "checkpoint":
            assert result["active_task"]["checkpoint"] == {"step": 2}
        if advance == "preview":
            current = result["changes"][0]
            assert current["change_key"] == initial["change_key"]
            assert current["view_version"] != initial["view_version"]
            assert current["preview"] == {"title": "已刷新"}
            stale = await reader.post(
                f"/api/v1/projects/{project['id']}/agent/changes/{current['id']}/reject",
                json={"change_key": initial["change_key"], "view_version": initial["view_version"]},
            )
            assert stale.status_code == 409, stale.text
        if advance == "step_cursor":
            assert result["active_task"]["step_cursor"] == 1


async def test_turn_keeps_one_snapshot_when_writer_commits_during_reread(app, client, monkeypatch):
    base, cid, tid, _ = await _setup(app, client, monkeypatch)
    # WAL 允许写者在读快照仍存续时提交，确定性地覆盖重读期间的第二次更新。
    if app.state.engine.dialect.name == "sqlite":
        async with app.state.engine.connect() as connection:
            assert (await connection.exec_driver_sql("PRAGMA journal_mode=WAL")).scalar().lower() == "wal"
    async with app.state.sessionmaker.begin() as writer:
        (await writer.get(Task, tid)).resource_key = f"agent:{cid}"

    project_payload = personal_projection.project_agent_payload
    reads = 0

    async def advance(db, user_id, project_id, payload, proofs):
        nonlocal reads
        reads += 1
        async with app.state.sessionmaker.begin() as writer:
            task = await writer.get(Task, tid)
            task.step_cursor = reads
            writer.add(AgentMessage(conversation_id=cid, turn_task_id=tid, turn_task_key=task.statistics_key,
                                    role="assistant", content=f"步骤 {reads}"))
        return await project_payload(db, user_id, project_id, payload, proofs)

    monkeypatch.setattr(personal_projection, "project_agent_payload", advance)
    response = await client.get(f"{base}/conversations/{cid}")
    assert response.status_code == 200 and reads == 2, response.text
    assert response.json()["active_task"]["step_cursor"] == 1
    assert [m["content"] for m in response.json()["messages"]] == ["步骤 1"]
    monkeypatch.setattr(personal_projection, "project_agent_payload", project_payload)
    latest = (await client.get(f"{base}/conversations/{cid}")).json()
    assert latest["active_task"]["step_cursor"] == 2
    assert [m["content"] for m in latest["messages"]] == ["步骤 1", "步骤 2"]


@pytest.mark.parametrize("replacement", ["conversation", "task", "message", "token", "resource"])
async def test_reread_revalidates_original_before_using_new_snapshot(app, client, monkeypatch, replacement):
    base, cid, tid, _ = await _setup(app, client, monkeypatch)
    async with app.state.sessionmaker.begin() as db:
        (await db.get(Task, tid)).resource_key = f"agent:{cid}"
        message = AgentMessage(conversation_id=cid, role="user", content="原历史")
        db.add(message)
        await db.flush()
        mid = message.id
    project_payload = personal_projection.project_agent_payload

    async def race(db, user_id, project_id, payload, proofs):
        async with app.state.sessionmaker.begin() as writer:
            (await writer.get(Task, tid)).step_cursor += 1
        try:
            return await project_payload(db, user_id, project_id, payload, proofs)
        except personal_projection.AgentDetailAdvanced:
            async with app.state.sessionmaker.begin() as writer:
                if replacement == "conversation":
                    (await writer.get(AgentConversation, cid)).instance_key = str(uuid4())
                elif replacement == "task":
                    (await writer.get(Task, tid)).statistics_key = str(uuid4())
                elif replacement == "message":
                    (await writer.get(AgentMessage, mid)).content = "替代历史"
                elif replacement == "resource":
                    (await writer.get(Task, tid)).resource_key = f"agent:{cid + 1}"
                else:
                    (await writer.get(User, user_id)).token_version += 1
            raise

    monkeypatch.setattr(personal_projection, "project_agent_payload", race)
    response = await client.get(f"{base}/conversations/{cid}")
    assert response.status_code == (401 if replacement == "token" else 404), response.text
    assert "原历史" not in response.text and "替代历史" not in response.text
