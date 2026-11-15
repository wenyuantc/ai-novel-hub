"""规划预览仅做本地读取，按真实 actor 和同作品来源授权。"""

import pytest
from sqlalchemy import event, inspect, update

from app.models import Chapter, ChapterPlan, Outline, Project, ProjectCollaborator, User
from app.models.planning import PlanningUnit
from app.services import planning_context_service
from tests.helpers import register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_collaboration_setting_context import _seed


def _path(project_id):
    return f"/api/v1/projects/{project_id}/planning/units/context-preview"


async def _extra_sources(app, values, other):
    project, chapter = values[3], values[4]
    async with app.state.sessionmaker() as db:
        own_outline = Outline(project_id=project["id"], title="本书大纲")
        foreign_outline = Outline(project_id=other["id"], title="他书大纲")
        db.add_all([own_outline, foreign_outline])
        await db.flush()
        own_plan = ChapterPlan(project_id=project["id"], outline_id=own_outline.id, title="本书细纲", seq=1)
        foreign_plan = ChapterPlan(project_id=other["id"], outline_id=foreign_outline.id, title="他书细纲", seq=1)
        own_detached = PlanningUnit(project_id=project["id"], last_source_label="本书待关联")
        foreign_detached = PlanningUnit(project_id=other["id"], last_source_label="他书待关联")
        db.add_all([own_plan, foreign_plan, own_detached, foreign_detached])
        await db.commit()
        return {
            "own_plan": own_plan.id, "foreign_plan": foreign_plan.id, "foreign_outline": foreign_outline.id,
            "own_detached": own_detached.id, "foreign_detached": foreign_detached.id,
            "chapter": chapter["id"],
        }


async def test_local_planning_preview_role_matrix_source_isolation_and_no_outbound(
    app, client, make_client, system_admin, fake_llm,
):
    values, other, _, _ = await _seed(app, client, make_client)
    _, viewer_user, editor_user, project, chapter, scene, _, _, viewer, editor, stranger = values
    sources = await _extra_sources(app, values, other)
    foreign_chapter = await client.post(f"/api/v1/projects/{other['id']}/chapters", json={"title": "他书章"})
    assert foreign_chapter.status_code == 201
    foreign_scene_page = await client.get(
        f"/api/v1/projects/{other['id']}/chapters/{foreign_chapter.json()['id']}/scenes",
    )
    foreign_scene = foreign_scene_page.json()["items"][0]["id"]
    manager = make_client()
    manager_user = await register(manager, "planning-preview-manager")
    role = await create_role(system_admin, "规划预览只读", ["projects.read"])
    await assign_roles(system_admin, manager_user["id"], [role["id"]])
    url = _path(project["id"])
    items = [("chapter", chapter["id"]), ("scene", scene["id"]), ("plan", sources["own_plan"]),
             ("detached", sources["own_detached"])]
    statements = []

    def collect(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", collect)
    try:
        for kind, source_id in items:
            params = {"source_kind": kind, "source_id": source_id}
            expected = None
            for actor in (client, viewer, editor, manager, system_admin):
                response = await actor.get(url, params=params)
                assert response.status_code == 200, (kind, response.text)
                if expected is None:
                    expected = response.json()
                assert response.json() == expected
                assert "作者私人风格" not in response.text
                assert "另一书私有设定" not in response.text
            assert (await stranger.get(url, params=params)).status_code == 404
            assert (await make_client().get(url, params=params)).status_code == 401
        foreign_sources = (("chapter", foreign_chapter.json()["id"]), ("scene", foreign_scene),
                           ("plan", sources["foreign_plan"]), ("detached", sources["foreign_detached"]))
        for kind, source_id in foreign_sources:
            response = await viewer.get(url, params={"source_kind": kind, "source_id": source_id})
            assert response.status_code == 404, response.text
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", collect)
    assert not [s for s in statements if s.lstrip().startswith(("insert", "update", "delete"))]
    assert not any("retrieval_chunks" in s or "kb_chunks" in s for s in statements)
    assert not fake_llm.calls and not fake_llm.embed_calls

    # 管理与协作重叠时沿用同一人工读取能力，不额外开放 AI。
    await assign_roles(system_admin, viewer_user["id"], [role["id"]])
    assert (await viewer.get(url, params={"source_kind": "chapter", "source_id": chapter["id"]})).status_code == 200
    assert (await editor.get(url, params={"source_kind": "scene", "source_id": scene["id"]})).status_code == 200
    assert editor_user["id"] != viewer_user["id"]


@pytest.mark.parametrize("kind", ["chapter", "scene"])
async def test_planning_preview_does_not_return_corrupt_foreign_plan_id(app, client, make_client, kind):
    values, other, ids, _ = await _seed(app, client, make_client)
    project, chapter, scene = values[3:6]
    sources = await _extra_sources(app, values, other)
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, chapter["id"])
        row.plan_id, row.outline_id = sources["foreign_plan"], sources["foreign_outline"]
        unit = await db.get(PlanningUnit, ids["chapter_unit"])
        unit.plan_id = sources["foreign_plan"]
        await db.commit()
    response = await client.get(_path(project["id"]), params={
        "source_kind": kind, "source_id": chapter["id"] if kind == "chapter" else scene["id"],
    })
    assert response.status_code == 200, response.text
    assert response.json()["plan_id"] is None
    assert "他书细纲" not in response.text and "他书大纲" not in response.text


async def test_planning_preview_preserves_valid_plan_and_does_not_flush_pending_objects(app, client, make_client):
    values, other, _, _ = await _seed(app, client, make_client)
    project, chapter = values[3], values[4]
    sources = await _extra_sources(app, values, other)
    async with app.state.sessionmaker() as db:
        plan = await db.get(ChapterPlan, sources["own_plan"])
        row = await db.get(Chapter, chapter["id"])
        row.plan_id, row.outline_id, plan.chapter_id = plan.id, plan.outline_id, row.id
        await db.commit()
        project_row = await db.get(Project, project["id"])
        row.title = "尚未提交的标题"
        result = await planning_context_service.context_preview(db, project_row, "chapter", row.id)
        assert result.plan_id == plan.id
        assert row.title == "尚未提交的标题" and inspect(row).attrs.title.history.has_changes()
        await db.rollback()
    response = await values[8].get(_path(project["id"]), params={
        "source_kind": "chapter", "source_id": chapter["id"],
    })
    assert response.status_code == 200 and response.json()["plan_id"] == sources["own_plan"]


@pytest.mark.parametrize("field", [
    "planning_revision", "fields_revision", "ai_visibility_revision", "content_revision",
])
async def test_planning_preview_rechecks_after_render_before_response(app, client, make_client, monkeypatch, field):
    values, _, _, _ = await _seed(app, client, make_client)
    project, chapter, scene, viewer = values[3], values[4], values[5], values[8]
    original = planning_context_service.load_planning_context

    async def changed(db, project, *args, **kwargs):
        result = await original(db, project, *args, **kwargs)
        model, identity = (Chapter, chapter["id"]) if field == "content_revision" else (Project, project.id)
        column = getattr(model, field)
        await db.execute(update(model).where(model.id == identity).values({field: column + 1}))
        return result

    monkeypatch.setattr(planning_context_service, "load_planning_context", changed)
    response = await viewer.get(_path(project["id"]), params={"source_kind": "scene", "source_id": scene["id"]})
    assert response.status_code == 409, response.text
    assert response.json()["code"] in {"planning_context_changed", "stale_revision", "ai_visibility_changed",
                                        "scene_context_changed"}


@pytest.mark.parametrize("change,expected", [("revoke", 404), ("revision", 409), ("token", 401)])
async def test_planning_preview_rechecks_actor_after_render(app, client, make_client, monkeypatch, change, expected):
    values, _, _, _ = await _seed(app, client, make_client)
    viewer_user, project, chapter, viewer = values[1], values[3], values[4], values[8]
    original = planning_context_service.load_planning_context

    async def changed(db, project, *args, **kwargs):
        result = await original(db, project, *args, **kwargs)
        if change == "token":
            await db.execute(update(User).where(User.id == viewer_user["id"]).values(
                token_version=User.token_version + 1,
            ))
        else:
            fields = {"status": "revoked"} if change == "revoke" else {"revision": ProjectCollaborator.revision + 1}
            await db.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project.id, ProjectCollaborator.user_id == viewer_user["id"],
            ).values(**fields))
        return result

    monkeypatch.setattr(planning_context_service, "load_planning_context", changed)
    response = await viewer.get(_path(project["id"]), params={"source_kind": "chapter", "source_id": chapter["id"]})
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("reinvite", [False, True])
async def test_planning_preview_rechecks_after_real_revoke_and_reinvite(
    app, client, make_client, monkeypatch, reinvite,
):
    values, _, _, _ = await _seed(app, client, make_client)
    viewer_user, project, chapter, base, viewer = values[1], values[3], values[4], values[6], values[8]
    original = planning_context_service.load_planning_context

    async def changed(db, project, *args, **kwargs):
        result = await original(db, project, *args, **kwargs)
        revoked = await client.delete(f"{base}/collaborators/{viewer_user['id']}", params={"expected_revision": 2})
        assert revoked.status_code == 204, revoked.text
        if reinvite:
            invited = await client.post(f"{base}/collaborators", json={"username": "content-viewer", "role": "viewer"})
            assert invited.status_code == 201, invited.text
            path = f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept"
            accepted = await viewer.post(path, json={"expected_revision": invited.json()["revision"]})
            assert accepted.status_code == 200, accepted.text
        return result

    monkeypatch.setattr(planning_context_service, "load_planning_context", changed)
    response = await viewer.get(_path(project["id"]), params={"source_kind": "chapter", "source_id": chapter["id"]})
    assert response.status_code == (409 if reinvite else 404), response.text


async def test_admin_read_still_applies_if_collaborator_grant_changes(
    app, client, make_client, system_admin, monkeypatch,
):
    values, _, _, _ = await _seed(app, client, make_client)
    viewer_user, project, chapter, viewer = values[1], values[3], values[4], values[8]
    role = await create_role(system_admin, "协作管理员预览", ["projects.read"])
    await assign_roles(system_admin, viewer_user["id"], [role["id"]])
    original = planning_context_service.load_planning_context

    async def revoked(db, project, *args, **kwargs):
        result = await original(db, project, *args, **kwargs)
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project.id, ProjectCollaborator.user_id == viewer_user["id"],
        ).values(status="revoked"))
        return result

    monkeypatch.setattr(planning_context_service, "load_planning_context", revoked)
    response = await viewer.get(_path(project["id"]), params={"source_kind": "chapter", "source_id": chapter["id"]})
    assert response.status_code == 200, response.text
