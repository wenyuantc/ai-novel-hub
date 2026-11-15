"""分析状态、历史与任务接口对本人任务采用相同的授权轮次。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.models import ChapterAnalysis, ProjectCollaborator, Task
from tests.helpers import create_project, register, task_params
from tests.rbac_helpers import assign_roles, create_role
from tests.test_collaboration_planning_access import _collaborators


@pytest.mark.parametrize("change", ["downgrade", "revision", "reinvite"])
async def test_analysis_task_visibility_tracks_current_grant(app, client, make_client, system_admin, change):
    base, viewer, editor, _, users = await _collaborators(client, make_client)
    await app.state.task_engine.stop()
    owner_id = (await client.get("/api/v1/auth/me")).json()["id"]
    project_id = int(base.rsplit("/", 1)[-1])
    chapter_response = await client.post(f"{base}/chapters", json={"title": "活动分析", "chapter_no": 1})
    assert chapter_response.status_code == 201, chapter_response.text
    chapter_id = chapter_response.json()["id"]
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == users["editor"]["id"],
        ))
        old_invitation, old_revision = member.invitation_id, member.revision
        grant = {"invitation_id": old_invitation, "revision": old_revision}
        tasks = [
            Task(user_id=owner_id, project_id=project_id, chapter_id=chapter_id,
                 kind="chapter.analyze", status="running"),
            Task(user_id=users["editor"]["id"], project_id=project_id, chapter_id=chapter_id,
                 kind="chapter.analyze", status="queued", input={"_collaboration_grant": grant}),
            Task(user_id=users["editor"]["id"], project_id=project_id, chapter_id=chapter_id,
                 kind="chapter.analyze", status="succeeded"),
        ]
        db.add_all(tasks)
        await db.flush()
        analyses = [ChapterAnalysis(project_id=project_id, chapter_id=chapter_id, chapter_no=1,
                                   task_id=task.id, content_hash="0" * 64) for task in tasks]
        db.add_all(analyses)
        await db.flush()
        owner_task, active_task, finished_task = [task.id for task in tasks]
        _, active_analysis, finished_analysis = [analysis.id for analysis in analyses]
        await db.commit()
    listing = await editor.get("/api/v1/tasks")
    assert listing.status_code == 200, listing.text
    task_refs = {task["id"]: task for task in listing.json()}
    assert {active_task, finished_task} <= task_refs.keys()

    async def check_view(actor, *, active_visible):
        for path in (f"{base}/analysis/statuses", f"/api/v1/chapters/{chapter_id}/analysis-status"):
            response = await actor.get(path)
            assert response.status_code == 200, response.text
            state = response.json()[0] if path.endswith("/statuses") else response.json()
            assert (state["status"] == "analyzing") is active_visible
            assert state["active_task_id"] == (active_task if active_visible else None)
        history = await actor.get(f"/api/v1/chapters/{chapter_id}/analyses")
        assert history.status_code == 200, history.text
        indexed = {row["id"]: row["task_id"] for row in history.json()}
        assert indexed[active_analysis] == (active_task if active_visible else None)
        assert indexed[finished_analysis] == finished_task
        for analysis_id, task_id in ((active_analysis, active_task if active_visible else None),
                                     (finished_analysis, finished_task)):
            response = await actor.get(f"/api/v1/analyses/{analysis_id}")
            assert response.status_code == 200 and response.json()["task_id"] == task_id, response.text
        active = await actor.get(f"/api/v1/tasks/{active_task}", params=task_params(task_refs[active_task]))
        assert active.status_code == (200 if active_visible else 404)
        finished = await actor.get(f"/api/v1/tasks/{finished_task}", params=task_params(task_refs[finished_task]))
        assert finished.status_code == 200

    await check_view(editor, active_visible=True)
    viewer_state = await viewer.get(f"/api/v1/chapters/{chapter_id}/analysis-status")
    assert viewer_state.status_code == 200 and viewer_state.json()["status"] != "analyzing"
    owner_state = await client.get(f"/api/v1/chapters/{chapter_id}/analysis-status")
    assert owner_state.status_code == 200 and owner_state.json()["active_task_id"] == owner_task

    member_path = f"{base}/collaborators/{users['editor']['id']}"
    if change == "downgrade":
        # 管理员与协作者能力并集不可绕过私人任务的活动授权。
        role = await create_role(system_admin, "分析任务管理", ["projects.read", "projects.edit"])
        await assign_roles(system_admin, users["editor"]["id"], [role["id"]])
        response = await client.patch(member_path, json={"expected_revision": old_revision, "role": "viewer"})
        assert response.status_code == 200, response.text
    elif change == "revision":
        response = await client.patch(member_path, json={"expected_revision": old_revision, "allow_owner_ai": True})
        assert response.status_code == 200, response.text
    else:
        assert (await client.delete(member_path, params={"expected_revision": old_revision})).status_code == 204
        hidden = await editor.get(f"/api/v1/tasks/{finished_task}", params=task_params(task_refs[finished_task]))
        assert hidden.status_code == 404
        invitation = await client.post(f"{base}/collaborators", json={
            "username": "planning-editor", "role": "editor",
        })
        assert invitation.status_code == 201, invitation.text
        new = invitation.json()
        assert new["invitation_id"] != old_invitation
        accepted = await editor.post(
            f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept",
            json={"expected_revision": new["revision"]},
        )
        assert accepted.status_code == 200, accepted.text

    await check_view(editor, active_visible=False)
    owner_state = await client.get(f"/api/v1/chapters/{chapter_id}/analysis-status")
    assert owner_state.status_code == 200 and owner_state.json()["active_task_id"] == owner_task


@pytest.mark.parametrize("endpoint,transition", [
    ("project-status", "revision"),
    ("chapter-status", "reinvite"),
    ("history", "downgrade"),
    ("detail", "finished_downgrade"),
    ("chapter-status", "finished"),
    ("project-status", "replace"),
    ("detail", "replace"),
    ("project-status", "queued"),
    ("history", "queued"),
    ("chapter-status", "renew_grant"),
    ("detail", "renew_grant"),
    ("project-status", "revoke"),
    ("history", "revoke"),
    ("history", "revoke_no_ref"),
    ("detail", "token"),
])
async def test_analysis_rechecks_after_concurrent_grant_change(
    app, client, make_client, monkeypatch, endpoint, transition,
):
    from app.models import User
    from app.services import analysis_service, rich_document_ai, writeback_service

    base, _, editor, _, users = await _collaborators(client, make_client)
    await app.state.task_engine.stop()
    project_id = int(base.rsplit("/", 1)[-1])
    chapter = await client.post(f"{base}/chapters", json={"title": "并发分析", "chapter_no": 1})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == users["editor"]["id"],
        ))
        old_invitation, revision = member.invitation_id, member.revision
        task = Task(user_id=users["editor"]["id"], project_id=project_id, chapter_id=chapter_id,
                    kind="chapter.analyze", status="queued", input={"_collaboration_grant": {
                        "invitation_id": old_invitation, "revision": revision,
                    }})
        db.add(task)
        await db.flush()
        analysis = ChapterAnalysis(project_id=project_id, chapter_id=chapter_id, chapter_no=1,
                                   task_id=task.id, content_hash="0" * 64)
        db.add(analysis)
        await db.flush()
        task_id, analysis_id = task.id, analysis.id
        if transition == "revoke_no_ref":
            await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=None))
        await db.commit()

    reached, release = asyncio.Event(), asyncio.Event()
    if endpoint in {"project-status", "chapter-status"}:
        target, attr = rich_document_ai, "analysis_fingerprints"
    elif endpoint == "history":
        target, attr = analysis_service, "_visible_task_ids"
    else:
        target, attr = writeback_service, "list_changes"
    original = getattr(target, attr)

    async def blocked(*args, **kwargs):
        result = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(target, attr, blocked)
    path = {
        "project-status": f"{base}/analysis/statuses",
        "chapter-status": f"/api/v1/chapters/{chapter_id}/analysis-status",
        "history": f"/api/v1/chapters/{chapter_id}/analyses",
        "detail": f"/api/v1/analyses/{analysis_id}",
    }[endpoint]
    request = asyncio.create_task(editor.get(path))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        member_path = f"{base}/collaborators/{users['editor']['id']}"
        if transition in {"reinvite", "revoke", "revoke_no_ref"}:
            assert (await client.delete(member_path, params={"expected_revision": revision})).status_code == 204
            if transition == "reinvite":
                invite = await client.post(f"{base}/collaborators", json={
                    "username": "planning-editor", "role": "editor",
                })
                assert invite.status_code == 201, invite.text
                new = invite.json()
                assert new["invitation_id"] != old_invitation
                accepted = await editor.post(
                    f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept",
                    json={"expected_revision": new["revision"]},
                )
                assert accepted.status_code == 200, accepted.text
        else:
            if transition in {"finished", "finished_downgrade"}:
                async with app.state.sessionmaker() as db:
                    task = await db.get(Task, task_id)
                    task.status = "succeeded"
                    await db.commit()
            elif transition == "replace":
                async with app.state.sessionmaker() as db:
                    task = await db.get(Task, task_id)
                    old_key = task.statistics_key
                    await db.delete(task)
                    await db.flush()
                    replacement = Task(id=task_id, user_id=users["editor"]["id"], project_id=project_id,
                                       chapter_id=chapter_id, kind="chapter.analyze", status="queued",
                                       input={"_collaboration_grant": {
                                           "invitation_id": old_invitation, "revision": revision,
                                       }})
                    db.add(replacement)
                    await db.flush()
                    assert replacement.statistics_key != old_key
                    await db.commit()
            elif transition == "queued":
                async with app.state.sessionmaker() as db:
                    task = await db.get(Task, task_id)
                    task.queued_at += timedelta(seconds=1)
                    await db.commit()
            elif transition == "token":
                async with app.state.sessionmaker() as db:
                    await db.execute(update(User).where(User.id == users["editor"]["id"]).values(
                        token_version=User.token_version + 1,
                    ))
                    await db.commit()
            if transition not in {"finished", "replace", "queued", "token"}:
                body = {"expected_revision": revision}
                body.update({"allow_owner_ai": True} if transition in {"revision", "renew_grant"}
                            else {"role": "viewer"})
                changed = await client.patch(member_path, json=body)
                assert changed.status_code == 200, changed.text
                if transition == "renew_grant":
                    async with app.state.sessionmaker() as db:
                        task = await db.get(Task, task_id)
                        task.input = {"_collaboration_grant": {
                            "invitation_id": old_invitation, "revision": changed.json()["revision"],
                        }}
                        await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        if transition in {"revoke", "revoke_no_ref", "token"}:
            assert response.status_code == (401 if transition == "token" else 404), response.text
            return
        assert response.status_code == 200, response.text
        if endpoint in {"project-status", "chapter-status"}:
            state = response.json()[0] if endpoint == "project-status" else response.json()
            assert state["status"] != "analyzing" and state["active_task_id"] is None
        elif endpoint == "history":
            assert response.json()[0]["task_id"] is None
        else:
            assert response.json()["task_id"] is None
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_author_legacy_task_without_uuid_hides_reference_but_keeps_analysis(app, client):
    owner = await register(client, "legacy-analysis-owner")
    await app.state.task_engine.stop()
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapters = []
    for number in (1, 2):
        response = await client.post(f"{base}/chapters", json={"title": f"章节 {number}", "chapter_no": number})
        assert response.status_code == 201, response.text
        chapters.append(response.json()["id"])
    async with app.state.sessionmaker() as db:
        tasks = [Task(user_id=owner["id"], project_id=project["id"], chapter_id=chapter_id,
                      kind="chapter.analyze", status="queued") for chapter_id in chapters]
        db.add_all(tasks)
        await db.flush()
        legacy_id, normal_id = [task.id for task in tasks]
        assert tasks[1].statistics_key is not None
        await db.execute(update(Task).where(Task.id == legacy_id).values(statistics_key=None))
        analyses = [ChapterAnalysis(project_id=project["id"], chapter_id=chapter_id, chapter_no=index,
                                   task_id=task.id, content_hash="0" * 64, summary="保留的分析正文")
                    for index, (chapter_id, task) in enumerate(zip(chapters, tasks, strict=True), start=1)]
        db.add_all(analyses)
        await db.flush()
        legacy_analysis, normal_analysis = [analysis.id for analysis in analyses]
        await db.commit()

    states = await client.get(f"{base}/analysis/statuses")
    assert states.status_code == 200, states.text
    state = {row["chapter_id"]: row for row in states.json()}
    assert state[chapters[0]]["status"] != "analyzing" and state[chapters[0]]["active_task_id"] is None
    assert state[chapters[1]]["status"] == "analyzing" and state[chapters[1]]["active_task_id"] == normal_id
    for analysis_id, expected in ((legacy_analysis, None), (normal_analysis, normal_id)):
        response = await client.get(f"/api/v1/analyses/{analysis_id}")
        assert response.status_code == 200 and response.json()["summary"] == "保留的分析正文", response.text
        assert response.json()["task_id"] == expected
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, legacy_id)
        assert task is not None and task.status == "queued" and task.statistics_key is None
        assert (await db.get(ChapterAnalysis, legacy_analysis)).task_id == legacy_id

