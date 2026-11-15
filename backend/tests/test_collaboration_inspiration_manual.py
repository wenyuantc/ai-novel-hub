"""本人灵感草稿应用到共享作品的人工权限边界。"""

import pytest
from sqlalchemy import event, select

from app.core.errors import ForbiddenError
from app.models import AdminAuditLog, Chapter
from app.schemas.inspiration import ApplyRequest
from app.services import inspiration_service
from tests.helpers import create_project, register
from tests.test_collaboration_story_access import _invite


async def _personal_draft(client, idea):
    response = await client.post("/api/v1/inspiration/sessions", json={"idea": idea})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    response = await client.patch(
        f"/api/v1/inspiration/sessions/{session_id}/steps/theme",
        json={"action": "decide", "value": "共同创作"},
    )
    assert response.status_code == 200, response.text
    return session_id


async def test_personal_inspiration_preview_and_apply_follow_project_capabilities(
    app, client, make_client, system_admin,
):
    await register(client, "inspiration-owner")
    editor, viewer, stranger = make_client(), make_client(), make_client()
    await register(editor, "inspiration-editor")
    await register(viewer, "inspiration-viewer")
    await register(stranger, "inspiration-stranger")
    project = await create_project(client, title="共享作品", theme="原主题")
    await _invite(client, editor, project["id"], "inspiration-editor", "editor")
    await _invite(client, viewer, project["id"], "inspiration-viewer", "viewer")
    editor_session = await _personal_draft(editor, "编辑者自己的想法")
    viewer_session = await _personal_draft(viewer, "查看者自己的想法")
    stranger_session = await _personal_draft(stranger, "陌生人的想法")
    admin_session = await _personal_draft(system_admin, "管理员自己的想法")
    url = "/api/v1/inspiration/sessions"
    for actor, session_id in ((editor, editor_session), (viewer, viewer_session), (system_admin, admin_session)):
        preview = await actor.get(f"{url}/{session_id}/apply-preview", params={"project_id": project["id"]})
        assert preview.status_code == 200, preview.text
        theme = next(item for item in preview.json() if item["field"] == "theme")
        assert theme["current"] == "原主题" and theme["proposed"] == "共同创作"
    assert (await stranger.get(
        f"{url}/{stranger_session}/apply-preview", params={"project_id": project["id"]},
    )).status_code == 404
    denied = await viewer.post(f"{url}/{viewer_session}/apply", json={"project_id": project["id"], "fields": ["theme"]})
    assert denied.status_code == 403, denied.text
    assert (await stranger.post(
        f"{url}/{stranger_session}/apply", json={"project_id": project["id"], "fields": ["theme"]},
    )).status_code == 404
    assert (await viewer.post(
        f"{url}/{editor_session}/apply", json={"project_id": project["id"], "fields": ["theme"]},
    )).status_code == 404
    for actor, session_id in ((editor, editor_session), (system_admin, admin_session)):
        saved = await actor.post(f"{url}/{session_id}/apply", json={"project_id": project["id"], "fields": ["theme"]})
        assert saved.status_code == 200, saved.text
        assert saved.json()["theme"] == "共同创作"
    assert (await client.get(f"/api/v1/projects/{project['id']}")).json()["theme"] == "共同创作"
    async with app.state.sessionmaker() as db:
        audit = await db.scalar(select(AdminAuditLog).where(
            AdminAuditLog.project_id == project["id"], AdminAuditLog.action == "project.update",
            AdminAuditLog.result == "success",
        ))
        assert audit is not None
        assert "管理员自己的想法" not in str(audit.summary)


async def test_denied_inspiration_apply_does_not_flush_unrelated_pending(app, client, make_client):
    await register(client, "inspiration-flush-owner")
    viewer = make_client()
    user = await register(viewer, "inspiration-flush-viewer")
    project = await create_project(client)
    await _invite(client, viewer, project["id"], "inspiration-flush-viewer", "viewer")
    session_id = await _personal_draft(viewer, "不应刷新其他写入")
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="授权失败时不得刷新")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(ForbiddenError):
            await inspiration_service.apply(
                db, user["id"], session_id, ApplyRequest(project_id=project["id"], fields=["theme"]),
            )
        assert not flushes and pending.id is None and pending in db.new
        await db.rollback()
