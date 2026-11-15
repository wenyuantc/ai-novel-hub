"""协作邀请、版本与身份按真实 HTTP 入口验证。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError
from app.models import CollaborationAuditEvent, Project, ProjectCollaborator, SeriesCollaborator, User
from app.services import collaboration_service, project_access
from tests.cover_fixtures import PNG
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role, set_quota


async def test_project_invitation_revision_lifecycle_and_access(app, client, make_client):
    owner = await register(client, "collab-owner")
    guest_client = make_client()
    guest = await register(guest_client, "collab-guest")
    project = await create_project(client)
    project_url = f"/api/v1/projects/{project['id']}"
    members_url = f"{project_url}/collaborators"

    invited = await client.post(members_url, json={"username": "collab-guest", "role": "viewer"})
    assert invited.status_code == 201, invited.text
    row = invited.json()
    assert row["status"] == "pending" and row["revision"] == 1 and row["allow_owner_ai"] is False
    assert (await guest_client.get(project_url)).status_code == 404
    pending = (await guest_client.get("/api/v1/collaboration/invitations")).json()
    assert len(pending) == 1 and pending[0]["invitation_id"] == row["invitation_id"]
    path = f"/api/v1/collaboration/invitations/project/{row['invitation_id']}"
    accepted = await guest_client.post(f"{path}/accept", json={"expected_revision": 1})
    assert accepted.status_code == 200 and accepted.json()["revision"] == 2
    view = await guest_client.get(project_url)
    assert view.status_code == 200, view.text
    assert view.json()["access"]["role"] == "viewer" and not view.json()["access"]["can_edit_content"]
    assert view.json()["reading"] is None
    assert [item["id"] for item in (await guest_client.get("/api/v1/projects")).json()] == [project["id"]]
    assert (await guest_client.patch(project_url, json={"title": "无权"})).status_code == 403

    stale = await client.patch(f"{members_url}/{guest['id']}", json={"role": "editor", "expected_revision": 1})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_invitation"
    updated = await client.patch(f"{members_url}/{guest['id']}", json={"role": "editor", "expected_revision": 2})
    assert updated.status_code == 200 and updated.json()["revision"] == 3
    edited = await guest_client.patch(project_url, json={"title": "协作修改"})
    assert edited.status_code == 200 and edited.json()["access"]["can_edit_content"]
    assert (await guest_client.patch(project_url, json={"default_style_id": None})).status_code == 403
    revoked = await client.delete(f"{members_url}/{guest['id']}", params={"expected_revision": 3})
    assert revoked.status_code == 204
    assert (await guest_client.get(project_url)).status_code == 404
    assert (await guest_client.get("/api/v1/projects")).json() == []
    again = await client.post(members_url, json={"username": "collab-guest", "role": "viewer"})
    assert again.status_code == 201 and again.json()["invitation_id"] != row["invitation_id"]
    assert again.json()["revision"] == 5
    late = await guest_client.post(f"{path}/accept", json={"expected_revision": 2})
    assert late.status_code == 404
    assert (await guest_client.get(project_url)).status_code == 404
    async with app.state.sessionmaker() as db:
        audit = list(await db.scalars(select(CollaborationAuditEvent).order_by(CollaborationAuditEvent.id)))
        assert any(e.action == "invite" and e.actor_user_id == owner["id"] and e.result == "success" for e in audit)
        assert any(e.action == "modify" and e.result == "failure" and e.error_code == "stale_invitation" for e in audit)


async def test_invitation_safe_failures_series_and_admin_overlap(app, client, make_client):
    await register(client, "collab-owner")
    guest_client = make_client()
    guest = await register(guest_client, "collab-admin")
    disabled_client = make_client()
    disabled = await register(disabled_client, "collab-disabled")
    project = await create_project(client)
    project_url = f"/api/v1/projects/{project['id']}"
    async with app.state.sessionmaker() as db:
        (await db.get(User, guest["id"])).is_admin = True
        (await db.get(User, disabled["id"])).is_active = False
        await db.commit()
    for name in ("collab-disabled", "nobody-matches"):
        response = await client.post(f"{project_url}/collaborators", json={"username": name, "role": "viewer"})
        assert response.status_code == 400 and response.json()["code"] == "invitee_unavailable"
    invited = await client.post(f"{project_url}/collaborators", json={"username": "collab-admin", "role": "viewer"})
    assert invited.status_code == 201, invited.text
    row = invited.json()
    path = f"/api/v1/collaboration/invitations/project/{row['invitation_id']}/accept"
    assert (await guest_client.post(path, json={"expected_revision": row["revision"]})).status_code == 200
    access = (await guest_client.get(project_url)).json()["access"]
    assert access["role"] == "viewer" and access["can_edit_content"] and not access["can_use_ai"]
    assert (await guest_client.patch(project_url, json={"title": "管理员手工修改"})).status_code == 200
    assert (await guest_client.get(f"{project_url}/backup")).status_code == 404

    series = await client.post("/api/v1/series", json={"name": "系列"})
    series_url = f"/api/v1/series/{series.json()['id']}"
    invite = await client.post(f"{series_url}/collaborators", json={"username": "collab-admin"})
    assert invite.status_code == 201 and invite.json()["role"] == "editor"
    path = f"/api/v1/collaboration/invitations/series/{invite.json()['invitation_id']}/decline"
    assert (await guest_client.post(path, json={"expected_revision": 1})).status_code == 200


async def test_pending_changes_invalidate_old_revision_and_private_style_is_redacted(app, client, make_client):
    await register(client, "private-owner")
    guest_client = make_client()
    guest = await register(guest_client, "private-guest")
    project = (await client.post("/api/v1/projects", json={"title": "私人风格作品", "style": "私人风格"})).json()
    base = f"/api/v1/projects/{project['id']}"
    assert project["default_style_id"] is not None and project["access"]["role"] == "owner"
    chapter = await client.post(f"{base}/chapters", json={"chapter_no": 1, "title": "第一章", "content": "正文"})
    assert chapter.status_code == 201, chapter.text
    progress = {"chapter_id": chapter.json()["id"], "ratio": 0.65}
    assert (await client.put(f"{base}/reading-progress", json=progress)).status_code == 204
    assert (await client.get(base)).json()["reading"]["ratio"] == 0.65
    invited = (await client.post(f"{base}/collaborators", json={
        "username": "private-guest", "role": "viewer",
    })).json()
    changed = await client.patch(f"{base}/collaborators/{guest['id']}", json={
        "expected_revision": 1, "role": "editor", "allow_owner_ai": True,
    })
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    accept = f"/api/v1/collaboration/invitations/project/{invited['invitation_id']}/accept"
    stale = await guest_client.post(accept, json={"expected_revision": 1})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_invitation"
    assert (await guest_client.post(accept, json={"expected_revision": 2})).status_code == 200
    view = (await guest_client.get(base)).json()
    assert view["default_style_id"] is None and view["default_style"] is None and view["reading"] is None
    assert view["access"]["role"] == "editor" and view["access"]["can_use_ai"]
    assert view["access"]["allow_owner_ai"] is True
    shelf = (await guest_client.get("/api/v1/projects")).json()[0]
    assert shelf["default_style_id"] is None and shelf["default_style"] is None and shelf["reading"] is None
    assert (await guest_client.put(f"{base}/reading-progress", json=progress)).status_code == 404
    async with app.state.sessionmaker() as db:
        record = await db.get(Project, project["id"])
        record.deletion_started_at = datetime.now(UTC)
        record.deletion_token = str(uuid4())
        await db.commit()
    assert (await guest_client.get("/api/v1/projects")).json() == []
    deleting = await guest_client.get(base)
    assert deleting.status_code == 409 and deleting.json()["code"] == "project_deleting"


async def test_nonmember_unknown_invitation_same_404_and_failure_audit(app, client, make_client):
    await register(client, "invite-owner")
    guest_client = make_client()
    await register(guest_client, "invite-guest")
    stranger = make_client()
    stranger_user = await register(stranger, "invite-stranger")
    project = await create_project(client)
    invited = (await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "invite-guest", "role": "viewer",
    })).json()
    prefix = "/api/v1/collaboration/invitations/project"
    other = await stranger.post(f"{prefix}/{invited['invitation_id']}/accept", json={"expected_revision": 1})
    missing = await stranger.post(f"{prefix}/{uuid4()}/accept", json={"expected_revision": 1})
    assert other.status_code == missing.status_code == 404 and other.json() == missing.json()
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.actor_user_id == stranger_user["id"], CollaborationAuditEvent.result == "failure",
        )))
        assert len(rows) == 2 and all(row.resource_id == 0 and row.error_code == "not_found" for row in rows)


async def test_series_invitation_does_not_grant_books_and_decline_can_be_reinvited(client, make_client):
    await register(client, "series-owner")
    guest = make_client()
    guest_user = await register(guest, "series-guest")
    project = await create_project(client)
    created = await client.post("/api/v1/series", json={"name": "系列"})
    assert created.status_code == 201, created.text
    base = f"/api/v1/series/{created.json()['id']}"
    joined = await client.post(f"{base}/books", json={
        "project_id": project["id"], "expected_revision": created.json()["revision"],
    })
    assert joined.status_code == 200, joined.text
    first = await client.post(f"{base}/collaborators", json={"username": "series-guest"})
    assert first.status_code == 201
    old = first.json()["invitation_id"]
    path = f"/api/v1/collaboration/invitations/series/{old}"
    assert (await guest.post(f"{path}/decline", json={"expected_revision": 1})).status_code == 200
    second = await client.post(f"{base}/collaborators", json={"username": "series-guest"})
    assert second.status_code == 201 and second.json()["revision"] == 3
    assert second.json()["invitation_id"] != old
    assert (await guest.post(f"{path}/accept", json={"expected_revision": 1})).status_code == 404
    current = f"/api/v1/collaboration/invitations/series/{second.json()['invitation_id']}"
    assert (await guest.post(f"{current}/accept", json={"expected_revision": 3})).status_code == 200
    assert (await guest.get(f"/api/v1/projects/{project['id']}")).status_code == 404
    assert (await guest.get("/api/v1/projects")).json() == []
    assert (await guest.get(f"{base}/collaborators")).status_code == 404
    revoked = await client.delete(f"{base}/collaborators/{guest_user['id']}", params={"expected_revision": 4})
    assert revoked.status_code == 204

    project_base = f"/api/v1/projects/{project['id']}"
    book_invite = await client.post(f"{project_base}/collaborators", json={
        "username": "series-guest", "role": "viewer",
    })
    assert book_invite.status_code == 201, book_invite.text
    book_path = f"/api/v1/collaboration/invitations/project/{book_invite.json()['invitation_id']}"
    assert (await guest.post(f"{book_path}/accept", json={"expected_revision": 1})).status_code == 200
    assert (await guest.get(project_base)).json()["series"] is None
    assert (await guest.get("/api/v1/projects")).json()[0]["series"] is None
    rejoined = await client.post(f"{base}/collaborators", json={"username": "series-guest"})
    assert rejoined.status_code == 201 and rejoined.json()["revision"] == 6
    series_path = f"/api/v1/collaboration/invitations/series/{rejoined.json()['invitation_id']}"
    assert (await guest.post(f"{series_path}/accept", json={"expected_revision": 6})).status_code == 200
    assert (await guest.get(project_base)).json()["series"]["id"] == created.json()["id"]
    assert (await guest.get("/api/v1/projects")).json()[0]["series"]["id"] == created.json()["id"]


async def test_http_project_responses_use_the_actual_actor(client, make_client, system_admin):
    owner = await register(client, "projection-owner")
    guest_client = make_client()
    guest = await register(guest_client, "projection-guest")

    created = await client.post("/api/v1/projects", json={"title": "原作"})
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["owner"]["id"] == owner["id"]
    assert project["access"]["role"] == "owner" and project["access"]["can_manage_collaborators"]
    base = f"/api/v1/projects/{project['id']}"
    owner_detail = await client.get(base)
    assert owner_detail.status_code == 200 and owner_detail.json()["access"]["role"] == "owner"

    invited = await client.post(f"{base}/collaborators", json={"username": "projection-guest", "role": "editor"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accept = f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept"
    accepted = await guest_client.post(accept, json={"expected_revision": invitation["revision"]})
    assert accepted.status_code == 200, accepted.text
    detail = await guest_client.get(base)
    assert detail.status_code == 200, detail.text
    assert detail.json()["owner"]["id"] == owner["id"]
    assert detail.json()["access"]["role"] == "editor" and detail.json()["access"]["is_collaborator"]
    assert not detail.json()["access"]["can_manage_collaborators"]
    updated = await guest_client.patch(base, json={"title": "编辑后的原作"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["access"]["role"] == "editor" and updated.json()["access"]["can_edit_content"]
    assert not updated.json()["access"]["can_backup"]

    # 管理员通过既有人工旁路编辑别人的封面，响应应反映管理员而非原作者。
    cover = await system_admin.put(f"{base}/cover", files={"file": ("cover.png", PNG, "image/png")})
    assert cover.status_code == 200, cover.text
    assert cover.json()["owner"]["id"] == owner["id"] and cover.json()["access"]["role"] == "admin"
    assert cover.json()["access"]["can_edit_content"] and not cover.json()["access"]["can_use_ai"]
    removed = await system_admin.delete(f"{base}/cover")
    assert removed.status_code == 200, removed.text
    assert removed.json()["access"]["role"] == "admin" and not removed.json()["access"]["can_backup"]

    # 备份导入者与原作者不同，导入响应必须以导入者为作者。
    backup = await client.get(f"{base}/backup")
    assert backup.status_code == 200, backup.text
    imported = await guest_client.post("/api/v1/projects/import", files={
        "file": ("backup.json", backup.content, "application/json"),
    })
    assert imported.status_code == 201, imported.text
    restored = imported.json()["project"]
    assert restored["owner"]["id"] == guest["id"] and restored["access"]["role"] == "owner"
    assert restored["access"]["can_manage_collaborators"] and restored["access"]["can_backup"]

    session = await guest_client.post("/api/v1/inspiration/sessions", json={"idea": "一个新的故事"})
    assert session.status_code == 201, session.text
    converted = await guest_client.post(
        f"/api/v1/inspiration/sessions/{session.json()['id']}/convert", json={"title_override": "灵感作品"},
    )
    assert converted.status_code == 201, converted.text
    assert converted.json()["owner"]["id"] == guest["id"] and converted.json()["access"]["role"] == "owner"
    assert converted.json()["access"]["can_manage_collaborators"]


async def test_shared_shelf_project_does_not_use_guest_project_quota(client, make_client, system_admin):
    await register(client, "quota-owner")
    guest_client = make_client()
    guest = await register(guest_client, "quota-guest")
    await set_quota(system_admin, guest["id"], {"project_count": {"mode": "limited", "value": 1}})
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    invited = await client.post(f"{base}/collaborators", json={"username": "quota-guest", "role": "viewer"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await guest_client.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    quota_url = f"/api/v1/admin/users/{guest['id']}/quotas"
    assert (await system_admin.get(quota_url)).json()["project_count"]["used"] == 0
    own = await guest_client.post("/api/v1/projects", json={"title": "自己创建的作品"})
    assert own.status_code == 201, own.text
    shelf = await guest_client.get("/api/v1/projects")
    assert shelf.status_code == 200, shelf.text
    access_by_id = {item["id"]: item["access"]["role"] for item in shelf.json()}
    assert access_by_id == {project["id"]: "viewer", own.json()["id"]: "owner"}
    assert (await system_admin.get(quota_url)).json()["project_count"]["used"] == 1
    blocked = await guest_client.post("/api/v1/projects", json={"title": "超出配额"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "quota_exceeded"


async def test_self_invite_disabled_actor_and_recycle_restore(app, client, make_client, system_admin):
    await register(client, "lifecycle-owner")
    guest_client = make_client()
    guest = await register(guest_client, "lifecycle-guest")
    pending_client = make_client()
    await register(pending_client, "lifecycle-pending")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    members = f"{base}/collaborators"

    self_invite = await client.post(members, json={"username": "lifecycle-owner", "role": "viewer"})
    assert self_invite.status_code == 400 and self_invite.json()["code"] == "self_invite"
    invited = await client.post(members, json={"username": "lifecycle-guest", "role": "editor"})
    assert invited.status_code == 201, invited.text
    accept_path = f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept"
    assert (await guest_client.post(accept_path, json={"expected_revision": 1})).status_code == 200
    pending = await client.post(members, json={"username": "lifecycle-pending", "role": "viewer"})
    assert pending.status_code == 201, pending.text
    pending_path = f"/api/v1/collaboration/invitations/project/{pending.json()['invitation_id']}/accept"
    assert (await pending_client.get("/api/v1/collaboration/invitations")).json()[0]["revision"] == 1

    assert (await client.delete(base)).status_code == 204
    assert (await guest_client.get(base)).status_code == 404
    assert (await guest_client.get("/api/v1/projects")).json() == []
    assert (await client.get(members)).status_code == 404
    assert (await pending_client.get("/api/v1/collaboration/invitations")).json() == []
    assert (await pending_client.post(pending_path, json={"expected_revision": 1})).status_code == 404

    recycle_url = "/api/v1/admin/recycle-bin/projects"
    trash = await system_admin.get(recycle_url)
    assert trash.status_code == 200, trash.text
    recycled = next(item for item in trash.json()["items"] if item["id"] == project["id"])
    restored = await system_admin.post(
        f"{recycle_url}/{project['id']}/restore", json={"deleted_at": recycled["deleted_at"], "confirm": True},
    )
    assert restored.status_code == 204, restored.text
    assert (await guest_client.get(base)).json()["access"]["role"] == "editor"
    assert (await pending_client.get("/api/v1/collaboration/invitations")).json()[0]["revision"] == 1
    assert (await pending_client.post(pending_path, json={"expected_revision": 1})).status_code == 200

    async with app.state.sessionmaker() as db:
        (await db.get(User, guest["id"])).is_active = False
        await db.commit()
        failures = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.result == "failure", CollaborationAuditEvent.resource_id == project["id"],
        )))
        assert {event.error_code for event in failures} >= {"self_invite", "not_found"}
    assert (await guest_client.get(base)).status_code == 401
    assert (await guest_client.patch(base, json={"title": "停用后写入"})).status_code == 401


async def test_rbac_and_collaboration_access_have_independent_sources(client, make_client, system_admin):
    await register(client, "overlap-owner")
    guest_client = make_client()
    guest = await register(guest_client, "overlap-guest")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    invited = await client.post(f"{base}/collaborators", json={"username": "overlap-guest", "role": "viewer"})
    assert invited.status_code == 201, invited.text
    accept = f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept"
    assert (await guest_client.post(accept, json={"expected_revision": 1})).status_code == 200
    role = await create_role(system_admin, "人工编辑者", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, guest["id"], [role["id"]])
    overlap = (await guest_client.get(base)).json()["access"]
    assert overlap["role"] == "viewer" and overlap["can_edit_content"] and not overlap["can_use_ai"]

    await assign_roles(system_admin, guest["id"], [])
    viewer = (await guest_client.get(base)).json()["access"]
    assert viewer["role"] == "viewer" and viewer["can_read"] and viewer["can_export"]
    assert not viewer["can_edit_content"] and not viewer["can_use_ai"]
    assert (await guest_client.patch(base, json={"title": "只读用户"})).status_code == 403

    await assign_roles(system_admin, guest["id"], [role["id"]])
    revoked = await client.delete(f"{base}/collaborators/{guest['id']}", params={"expected_revision": 2})
    assert revoked.status_code == 204, revoked.text
    admin = (await guest_client.get(base)).json()["access"]
    assert admin["role"] == "admin" and admin["can_read"] and admin["can_edit_content"]
    assert not admin["is_collaborator"] and not admin["can_export"] and not admin["can_use_ai"]
    assert not admin["can_backup"] and (await guest_client.get("/api/v1/projects")).json() == []
    assert (await guest_client.patch(base, json={"title": "仍可人工维护"})).status_code == 200


async def test_business_integrity_errors_are_not_misreported_as_duplicate(app, client, make_client, monkeypatch):
    await register(client, "integrity-owner")
    guest_client = make_client()
    await register(guest_client, "integrity-guest")
    project = await create_project(client)

    async def invalid_constraint(*args, **kwargs):
        raise IntegrityError("insert", {}, Exception("CHECK constraint failed: role"))

    monkeypatch.setattr(collaboration_service, "_lock_parent", invalid_constraint)
    response = await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "integrity-guest", "role": "viewer",
    })
    assert response.status_code == 500 and response.json()["detail"] == "服务器内部错误"
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(ProjectCollaborator))).all() == []
        failure = await db.scalar(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.result == "failure", CollaborationAuditEvent.resource_id == project["id"],
        ))
        assert failure is not None and failure.error_code == "system_error"


async def test_collaboration_rejects_out_of_range_database_ids_and_revisions(client, make_client):
    await register(client, "bounds-owner")
    guest_client = make_client()
    guest = await register(guest_client, "bounds-guest")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/collaborators"
    invited = await client.post(base, json={"username": "bounds-guest", "role": "viewer"})
    assert invited.status_code == 201, invited.text
    accept = f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept"
    oversized = 2_147_483_648
    assert (await client.get(f"/api/v1/projects/{oversized}/collaborators")).status_code == 422
    assert (await client.get(f"/api/v1/series/{oversized}/collaborators")).status_code == 422
    assert (await client.delete(f"{base}/{oversized}", params={"expected_revision": 1})).status_code == 422
    assert (await client.delete(f"{base}/{guest['id']}", params={"expected_revision": oversized})).status_code == 422
    assert (await client.patch(f"{base}/{guest['id']}", json={
        "expected_revision": oversized, "role": "editor",
    })).status_code == 422
    for revision in (oversized, True, "1"):
        assert (await guest_client.post(accept, json={"expected_revision": revision})).status_code == 422


@pytest.mark.parametrize("scope", ["project", "series"])
async def test_sqlite_duplicate_membership_constraint_is_identified(app, client, make_client, scope):
    owner = await register(client, f"constraint-{scope}-owner")
    guest = await register(make_client(), f"constraint-{scope}-guest")
    other = await register(make_client(), f"constraint-{scope}-other")
    if scope == "project":
        resource = await create_project(client)
        member_model = ProjectCollaborator
    else:
        response = await client.post("/api/v1/series", json={"name": "约束测试"})
        assert response.status_code == 201, response.text
        resource, member_model = response.json(), SeriesCollaborator
    payload = {"username": f"constraint-{scope}-guest"}
    if scope == "project":
        payload["role"] = "viewer"
    prefix = "projects" if scope == "project" else "series"
    response = await client.post(f"/api/v1/{prefix}/{resource['id']}/collaborators", json=payload)
    assert response.status_code == 201, response.text
    invitation_id = response.json()["invitation_id"]

    async with app.state.sessionmaker() as db:
        values = {f"{scope}_id": resource["id"], "user_id": guest["id"], "role": "editor",
                  "invitation_id": str(uuid4()), "invited_by_user_id": owner["id"]}
        db.add(member_model(**values))
        with pytest.raises(IntegrityError) as duplicate:
            await db.flush()
        assert collaboration_service._membership_conflict(duplicate.value, scope)
        await db.rollback()
        values.update(user_id=other["id"], invitation_id=invitation_id)
        db.add(member_model(**values))
        with pytest.raises(IntegrityError) as wrong_unique:
            await db.flush()
        assert not collaboration_service._membership_conflict(wrong_unique.value, scope)
        await db.rollback()


async def test_readonly_admin_keeps_existing_write_and_recycle_errors(app, client, make_client, system_admin):
    owner_client = make_client()
    await register(owner_client, "readonly-owner")
    reader = await register(client, "readonly-admin")
    project = await create_project(owner_client)
    role = await create_role(system_admin, "只读协作验证", ["projects.read"])
    await assign_roles(system_admin, reader["id"], [role["id"]])
    response = await client.patch(f"/api/v1/projects/{project['id']}", json={"title": "越权"})
    assert response.status_code == 403 and response.json()["code"] == "permission_denied"
    async with app.state.sessionmaker() as db:
        with pytest.raises(ForbiddenError) as error:
            await project_access.require_project(db, reader["id"], project["id"], "project.recycle", for_update=True)
        assert error.value.extra["code"] == "permission_denied"
        assert error.value.extra["required_permission"] == "projects.recycle"
        await db.rollback()


@pytest.mark.parametrize("busy", [False, True])
async def test_audit_constraint_failure_rolls_back_and_is_not_reported_as_duplicate(
    app, client, make_client, monkeypatch, busy,
):
    await register(client, "failure-owner")
    guest = await register(make_client(), "failure-guest")
    project = await create_project(client)

    async def fail_audit(*args, **kwargs):
        if busy:
            class SqliteBusy(Exception):
                sqlite_errorcode = 5

            raise OperationalError("audit", {}, SqliteBusy("database is locked"))
        raise IntegrityError("audit", {}, RuntimeError("injected audit storage failure"))

    monkeypatch.setattr(collaboration_service, "add_success", fail_audit)
    response = await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "failure-guest", "role": "editor",
    })
    assert response.status_code == 500 and response.json()["detail"] == "服务器内部错误"
    assert "already_collaborator" not in response.json()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ProjectCollaborator.id).where(ProjectCollaborator.user_id == guest["id"])) is None
        event = await db.scalar(select(CollaborationAuditEvent).where(CollaborationAuditEvent.action == "invite"))
        assert event.result == "failure" and event.error_code == "system_error"


async def test_lost_commit_confirmation_checks_operation_id_without_false_failure(
    app, client, make_client, monkeypatch,
):
    await register(client, "unknown-commit-owner")
    guest = await register(make_client(), "unknown-commit-guest")
    project = await create_project(client)
    original_add = collaboration_service.add_success
    original_commit = AsyncSession.commit

    async def mark(db, snapshot):
        result = await original_add(db, snapshot)
        db.info["simulate_collaboration_commit_loss"] = True
        return result

    async def lost(db):
        await original_commit(db)
        if db.info.pop("simulate_collaboration_commit_loss", False):
            raise ConnectionError("提交已成功但确认丢失")

    monkeypatch.setattr(collaboration_service, "add_success", mark)
    monkeypatch.setattr(AsyncSession, "commit", lost)
    result = await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "unknown-commit-guest", "role": "viewer",
    })
    assert result.status_code == 409 and result.json()["code"] == "operation_confirmed"
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == guest["id"]))
        assert member.status == "pending"
        events = list(await db.scalars(select(CollaborationAuditEvent).where(
            CollaborationAuditEvent.target_user_id == guest["id"], CollaborationAuditEvent.action == "invite",
        )))
        assert len(events) == 1 and events[0].result == "success"


async def test_sqlite_old_snapshot_is_rolled_back_before_rechecking_grant(app, client, make_client):
    await register(client, "sqlite-owner")
    guest_client = make_client()
    guest = await register(guest_client, "sqlite-guest")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    row = (await client.post(f"{base}/collaborators", json={"username": "sqlite-guest", "role": "editor"})).json()
    assert (await guest_client.post(f"/api/v1/collaboration/invitations/project/{row['invitation_id']}/accept", json={
        "expected_revision": 1,
    })).status_code == 200
    async with app.state.engine.connect() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.commit()
    async with app.state.sessionmaker() as stale_db:
        await stale_db.execute(text("BEGIN"))
        assert (await stale_db.scalar(select(ProjectCollaborator.status).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == guest["id"],
        ))) == "accepted"
        revoked = await client.delete(f"{base}/collaborators/{guest['id']}", params={"expected_revision": 2})
        assert revoked.status_code == 204
        with pytest.raises(ConflictError) as error:
            await project_access.require_project(stale_db, guest["id"], project["id"], "content.edit", for_update=True)
        assert error.value.extra["code"] == "collaboration_busy"
        assert (await stale_db.scalar(select(ProjectCollaborator.status).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == guest["id"],
        ))) == "revoked"
