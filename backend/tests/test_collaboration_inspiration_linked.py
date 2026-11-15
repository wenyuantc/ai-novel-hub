"""由共享作品创建的本人灵感会话必须绑定原邀请，生成仍受 AI 能力限制。"""

from sqlalchemy import delete

from app.models import InspirationOrigin, InspirationSession, Project
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_story_access import _invite


async def test_linked_inspiration_is_personal_and_old_invitation_never_recovers(client, make_client, fake_llm):
    await register(client, "linked-owner")
    editor, viewer, stranger = make_client(), make_client(), make_client()
    editor_user = await register(editor, "linked-editor")
    await register(viewer, "linked-viewer")
    await register(stranger, "linked-stranger")
    project = await create_project(client, title="原作品标题", premise="原作品设定")
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, project["id"], "linked-editor", "editor")
    await _invite(client, viewer, project["id"], "linked-viewer", "viewer")
    await create_ai_config(viewer)

    start = f"/api/v1/inspiration/sessions/from-project/{project['id']}"
    viewer_response = await viewer.post(start)
    assert viewer_response.status_code == 200, viewer_response.text
    viewer_id = viewer_response.json()["id"]
    denied = await viewer.post(f"/api/v1/inspiration/sessions/{viewer_id}/steps/title/generate", json={})
    assert denied.status_code == 403 and fake_llm.calls == []
    assert (await stranger.post(start)).status_code == 404

    response = await editor.post(start)
    assert response.status_code == 200, response.text
    old_id = response.json()["id"]
    assert response.json()["project_id"] == project["id"]
    assert response.json()["state"]["idea"] == "原作品设定"
    assert "invitation" not in response.text and "grant" not in response.text
    assert (await viewer.get(f"/api/v1/inspiration/sessions/{old_id}")).status_code == 404

    removed = await client.delete(f"{base}/collaborators/{editor_user['id']}", params={"expected_revision": 2})
    assert removed.status_code == 204, removed.text
    assert (await editor.get(f"/api/v1/inspiration/sessions/{old_id}")).status_code == 404
    assert all(row["id"] != old_id for row in (await editor.get("/api/v1/inspiration/sessions")).json())
    changed = await editor.patch(f"/api/v1/inspiration/sessions/{old_id}", json={"idea": "旧授权不能续签"})
    assert changed.status_code == 404
    generated = await editor.post(f"/api/v1/inspiration/sessions/{old_id}/steps/title/generate", json={})
    assert generated.status_code == 404
    assert fake_llm.calls == []
    await _invite(client, editor, project["id"], "linked-editor", "editor")
    assert (await editor.get(f"/api/v1/inspiration/sessions/{old_id}")).status_code == 404
    renewed = await editor.post(start)
    assert renewed.status_code == 200 and renewed.json()["id"] != old_id


async def test_linked_session_keeps_read_access_when_same_invitation_is_downgraded(client, make_client, fake_llm):
    await register(client, "linked-role-owner")
    editor = make_client()
    user = await register(editor, "linked-role-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, project["id"], "linked-role-editor", "editor")
    created = await editor.post(f"/api/v1/inspiration/sessions/from-project/{project['id']}")
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    changed = await client.patch(
        f"{base}/collaborators/{user['id']}", json={"expected_revision": 2, "role": "viewer"},
    )
    assert changed.status_code == 200, changed.text
    assert (await editor.get(f"/api/v1/inspiration/sessions/{session_id}")).status_code == 200
    denied = await editor.post(f"/api/v1/inspiration/sessions/{session_id}/steps/title/generate", json={})
    assert denied.status_code == 403 and fake_llm.calls == []


async def test_purged_project_origin_stays_hidden_but_owner_can_delete_personal_session(app, client, make_client):
    await register(client, "linked-purge-owner")
    editor = make_client()
    await register(editor, "linked-purge-editor")
    project = await create_project(client, premise="被彻删作品的私有正文")
    await _invite(client, editor, project["id"], "linked-purge-editor", "editor")
    created = await editor.post(f"/api/v1/inspiration/sessions/from-project/{project['id']}")
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    async with app.state.sessionmaker() as db:
        await db.execute(delete(Project).where(Project.id == project["id"]))
        await db.commit()
        assert (await db.get(InspirationSession, session_id)).project_id is None
        assert (await db.get(InspirationOrigin, session_id)).project_id == project["id"]
    assert (await editor.get(f"/api/v1/inspiration/sessions/{session_id}")).status_code == 404
    assert all(row["id"] != session_id for row in (await editor.get("/api/v1/inspiration/sessions")).json())
    assert (await editor.delete(f"/api/v1/inspiration/sessions/{session_id}")).status_code == 204
    async with app.state.sessionmaker() as db:
        assert await db.get(InspirationOrigin, session_id) is None
