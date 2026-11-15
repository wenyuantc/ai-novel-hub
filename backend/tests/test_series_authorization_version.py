"""系列权限代次绑定真实邀请轮次，不依赖系列内容 revision。"""

import re

import pytest
from sqlalchemy import update

from app.models import SeriesCollaborator
from app.services import series_service
from app.services.series_authorization import authorization_version
from tests.helpers import create_project, register


async def _invite(owner, guest, base, username):
    invitation = await owner.post(f"{base}/collaborators", json={"username": username})
    assert invitation.status_code == 201, invitation.text
    member = invitation.json()
    path = f"/api/v1/collaboration/invitations/series/{member['invitation_id']}/accept"
    accepted = await guest.post(path, json={"expected_revision": member["revision"]})
    assert accepted.status_code == 200, accepted.text
    return member, accepted.json()


def test_series_authorization_version_fails_closed_for_missing_identity():
    invite = "00000000-0000-4000-8000-000000000001"
    valid = authorization_version(1, 2, invite, 1)
    assert valid != authorization_version(2, 2, invite, 1)
    assert valid != authorization_version(1, 3, invite, 1)
    for series_id, actor_id, invitation_id, revision in (
        (0, 2, invite, 1), (1, 0, invite, 1), (1, 2, None, 1),
        (1, 2, "not-a-uuid", 1), (1, 2, invite, None), (1, 2, invite, 0),
    ):
        with pytest.raises(ValueError):
            authorization_version(series_id, actor_id, invitation_id, revision)


async def test_series_authorization_version_is_stable_private_and_changes_on_reinvite(client, make_client):
    await register(client, "series-version-owner")
    guest, other = make_client(), make_client()
    user = await register(guest, "series-version-editor")
    other_user = await register(other, "series-version-other")
    created = await client.post("/api/v1/series", json={"name": "系列"})
    assert created.status_code == 201
    series = created.json()
    base = f"/api/v1/series/{series['id']}"
    assert series["authorization_version"] is None
    assert (await client.get(base)).json()["authorization_version"] is None
    assert (await client.get("/api/v1/series")).json()[0]["authorization_version"] is None

    first, member = await _invite(client, guest, base, "series-version-editor")
    detail = await guest.get(base)
    assert detail.status_code == 200, detail.text
    old = detail.json()["authorization_version"]
    assert re.fullmatch(r"[0-9a-f]{64}", old)
    assert first["invitation_id"] not in detail.text
    assert (await guest.get(base)).json()["authorization_version"] == old
    assert (await guest.get("/api/v1/series")).json()[0]["authorization_version"] == old

    _, other_member = await _invite(client, other, base, "series-version-other")
    other_version = (await other.get(base)).json()["authorization_version"]
    assert other_version != old
    assert (await guest.get(base)).json()["authorization_version"] == old
    revoked_other = await client.delete(
        f"{base}/collaborators/{other_user['id']}", params={"expected_revision": other_member["revision"]},
    )
    assert revoked_other.status_code == 204
    assert (await guest.get(base)).json()["authorization_version"] == old

    renamed = await client.patch(base, json={"name": "改名", "expected_revision": series["revision"]})
    assert renamed.status_code == 200 and renamed.json()["authorization_version"] is None
    assert (await guest.get(base)).json()["authorization_version"] == old
    book = await create_project(client)
    joined = await client.post(f"{base}/books", json={
        "project_id": book["id"], "expected_revision": renamed.json()["revision"],
    })
    assert joined.status_code == 200 and joined.json()["authorization_version"] is None
    assert (await guest.get(base)).json()["authorization_version"] == old
    left = await client.delete(f"{base}/books/{book['id']}", params={"expected_revision": joined.json()["revision"]})
    assert left.status_code == 200 and left.json()["authorization_version"] is None

    revoked = await client.delete(
        f"{base}/collaborators/{user['id']}", params={"expected_revision": member["revision"]},
    )
    assert revoked.status_code == 204
    assert (await guest.get(base)).status_code == 404
    second_invite = await client.post(f"{base}/collaborators", json={"username": "series-version-editor"})
    assert second_invite.status_code == 201
    second = second_invite.json()
    assert second["invitation_id"] != first["invitation_id"]
    assert (await guest.get(base)).status_code == 404
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/series/{second['invitation_id']}/accept",
        json={"expected_revision": second["revision"]},
    )
    assert accepted.status_code == 200
    renewed = (await guest.get(base)).json()["authorization_version"]
    assert re.fullmatch(r"[0-9a-f]{64}", renewed) and renewed != old
    assert (await guest.get("/api/v1/series")).json()[0]["authorization_version"] == renewed


async def test_series_authorization_version_uses_member_revision(app, client, make_client):
    await register(client, "series-version-revision-owner")
    guest = make_client()
    user = await register(guest, "series-version-revision-editor")
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    base = f"/api/v1/series/{series['id']}"
    invitation, _ = await _invite(client, guest, base, "series-version-revision-editor")
    before = (await guest.get(base)).json()["authorization_version"]
    async with app.state.sessionmaker() as db:
        await db.execute(update(SeriesCollaborator).where(
            SeriesCollaborator.series_id == series["id"], SeriesCollaborator.user_id == user["id"],
        ).values(revision=SeriesCollaborator.revision + 1))
        await db.commit()
    detail = await guest.get(base)
    assert detail.status_code == 200
    after = detail.json()["authorization_version"]
    assert after != before and invitation["invitation_id"] not in detail.text
    assert (await guest.get("/api/v1/series")).json()[0]["authorization_version"] == after


async def test_project_series_projection_uses_same_authorization_version(client, make_client):
    await register(client, "series-version-book-owner")
    guest = make_client()
    user = await register(guest, "series-version-book-editor")
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    base = f"/api/v1/series/{series['id']}"
    book = await create_project(client)
    project_url = f"/api/v1/projects/{book['id']}"
    joined = await client.post(f"{base}/books", json={"project_id": book['id'], "expected_revision": 0})
    assert joined.status_code == 200
    assert (await client.get(project_url)).json()["series"]["authorization_version"] is None
    project_invite = await client.post(f"{project_url}/collaborators", json={
        "username": "series-version-book-editor", "role": "viewer",
    })
    assert project_invite.status_code == 201
    project_member = project_invite.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{project_member['invitation_id']}/accept",
        json={"expected_revision": project_member['revision']},
    )
    assert accepted.status_code == 200
    assert (await guest.get(project_url)).json()["series"] is None
    _, member = await _invite(client, guest, base, "series-version-book-editor")
    version = (await guest.get(base)).json()["authorization_version"]
    assert (await guest.get(project_url)).json()["series"]["authorization_version"] == version
    assert (await guest.get("/api/v1/projects")).json()[0]["series"]["authorization_version"] == version
    revoked = await client.delete(
        f"{base}/collaborators/{user['id']}", params={"expected_revision": member['revision']},
    )
    assert revoked.status_code == 204
    assert (await guest.get(project_url)).json()["series"] is None
    assert (await guest.get("/api/v1/projects")).json()[0]["series"] is None
    _, _ = await _invite(client, guest, base, "series-version-book-editor")
    renewed = (await guest.get(base)).json()["authorization_version"]
    assert renewed != version
    assert (await guest.get(project_url)).json()["series"]["authorization_version"] == renewed
    assert (await guest.get("/api/v1/projects")).json()[0]["series"]["authorization_version"] == renewed


async def test_series_detail_rejects_authorization_change_while_reading(app, client, make_client, monkeypatch):
    await register(client, "series-version-race-owner")
    guest = make_client()
    user = await register(guest, "series-version-race-editor")
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    base = f"/api/v1/series/{series['id']}"
    await _invite(client, guest, base, "series-version-race-editor")
    original = series_service._visible_project_ids

    async def change_revision(db, actor, item, ids):
        result = await original(db, actor, item, ids)
        await db.execute(update(SeriesCollaborator).where(
            SeriesCollaborator.series_id == item.id, SeriesCollaborator.user_id == user["id"],
        ).values(revision=SeriesCollaborator.revision + 1))
        return result

    monkeypatch.setattr(series_service, "_visible_project_ids", change_revision)
    result = await guest.get(base)
    assert result.status_code == 409 and result.json()["code"] == "stale_series_access"
