"""人工记忆写入不得以提交后的新邀请或新令牌续签旧响应。"""

import pytest
from sqlalchemy import func, select

from app.models import KbDocument, StoryMemory, Task, User
from app.services import kb_service, memory_service
from tests.helpers import create_ai_config, create_project, register


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("transition", ["revoke", "downgrade", "reinvite", "token"])
async def test_committed_manual_memory_rechecks_original_identity(
    app, client, make_client, monkeypatch, operation, transition,
):
    await app.state.task_engine.stop()
    await register(client, "memory-owner")
    editor = make_client()
    editor_user = await register(editor, "memory-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    old = await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "初始记忆"})
    assert old.status_code == 201, old.text
    await create_ai_config(client, model_type="embedding", model="owner-embedding")
    await create_ai_config(editor, model_type="embedding", model="editor-embedding")
    invited = await client.post(f"{base}/collaborators", json={"username": "memory-editor", "role": "editor"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    original = getattr(memory_service, f"{operation}_memory")
    transitioned = False

    async def change_after_commit(*args, **kwargs):
        nonlocal transitioned
        result = await original(*args, **kwargs)
        assert not transitioned
        transitioned = True
        if transition in {"revoke", "reinvite"}:
            changed = await client.delete(f"{base}/collaborators/{editor_user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert changed.status_code == 204, changed.text
        if transition == "downgrade":
            changed = await client.patch(f"{base}/collaborators/{editor_user['id']}", json={
                "expected_revision": member["revision"], "role": "viewer",
            })
            assert changed.status_code == 200, changed.text
        if transition == "reinvite":
            invited_again = await client.post(f"{base}/collaborators", json={
                "username": "memory-editor", "role": "editor",
            })
            assert invited_again.status_code == 201, invited_again.text
            renewed = invited_again.json()
            assert renewed["invitation_id"] != member["invitation_id"]
            accepted_again = await editor.post(
                f"/api/v1/collaboration/invitations/project/{renewed['invitation_id']}/accept",
                json={"expected_revision": renewed["revision"]},
            )
            assert accepted_again.status_code == 200, accepted_again.text
        if transition == "token":
            async with app.state.sessionmaker() as db:
                user = await db.get(User, editor_user["id"])
                user.token_version += 1
                await db.commit()
        return result

    monkeypatch.setattr(memory_service, f"{operation}_memory", change_after_commit)
    content = "原轮次提交的记忆"
    response = await (
        editor.post(f"{base}/memories", json={"chapter_no": 1, "content": content})
        if operation == "create" else editor.patch(f"{base}/memories/{old.json()['id']}", json={"content": content})
    )
    assert transitioned
    assert response.status_code == {"revoke": 404, "downgrade": 403, "reinvite": 403, "token": 401}[transition]
    assert content not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(StoryMemory.id)).where(StoryMemory.content == content)) == 1
        assert await db.scalar(select(func.count(Task.id))) == 0


@pytest.mark.parametrize("operation", ["create", "update", "upload"])
@pytest.mark.parametrize("transition", ["revoke", "downgrade", "reinvite", "token"])
async def test_manual_write_cannot_switch_identity_between_capture_and_lock(
    app, client, make_client, monkeypatch, operation, transition,
):
    await app.state.task_engine.stop()
    await register(client, "before-owner")
    editor = make_client()
    editor_user = await register(editor, "before-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    initial = await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "原有记忆"})
    assert initial.status_code == 201, initial.text
    invited = await client.post(f"{base}/collaborators", json={"username": "before-editor", "role": "editor"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    original = kb_service.capture_content_write_actor
    transitioned = False

    async def change_before_lock(db, user_id, project_id):
        nonlocal transitioned
        result = await original(db, user_id, project_id)
        assert not transitioned
        transitioned = True
        if transition in {"revoke", "reinvite"}:
            changed = await client.delete(f"{base}/collaborators/{editor_user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert changed.status_code == 204, changed.text
        if transition == "downgrade":
            changed = await client.patch(f"{base}/collaborators/{editor_user['id']}", json={
                "expected_revision": member["revision"], "role": "viewer",
            })
            assert changed.status_code == 200, changed.text
        if transition == "reinvite":
            again = await client.post(f"{base}/collaborators", json={"username": "before-editor", "role": "editor"})
            assert again.status_code == 201, again.text
            renewed = again.json()
            assert renewed["invitation_id"] != member["invitation_id"]
            reaccepted = await editor.post(
                f"/api/v1/collaboration/invitations/project/{renewed['invitation_id']}/accept",
                json={"expected_revision": renewed["revision"]},
            )
            assert reaccepted.status_code == 200, reaccepted.text
        if transition == "token":
            async with app.state.sessionmaker() as other:
                user = await other.get(User, editor_user["id"])
                user.token_version += 1
                await other.commit()
        return result

    monkeypatch.setattr(kb_service, "capture_content_write_actor", change_before_lock)
    if operation == "create":
        response = await editor.post(f"{base}/memories", json={"chapter_no": 1, "content": "禁止写入"})
    elif operation == "update":
        response = await editor.patch(f"{base}/memories/{initial.json()['id']}", json={"content": "禁止写入"})
    else:
        response = await editor.post(f"{base}/kb/documents", files={"file": ("禁止写入.txt", b"no write")})
    assert transitioned
    assert response.status_code == {"revoke": 404, "downgrade": 403, "reinvite": 403, "token": 401}[transition]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(StoryMemory.id))) == 1
        assert (await db.get(StoryMemory, initial.json()["id"])).content == "原有记忆"
        assert await db.scalar(select(func.count(KbDocument.id))) == 0
        assert await db.scalar(select(func.count(Task.id))) == 0
