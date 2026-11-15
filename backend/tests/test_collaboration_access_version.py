"""前端响应作用域必须区分权限相同但邀请轮次不同的协作授权。"""

from tests.helpers import create_project, register


async def test_access_version_tracks_invitation_round_and_is_actor_scoped(client, make_client, system_admin):
    await register(client, "scope-owner")
    guest = make_client()
    user = await register(guest, "scope-guest")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    members = f"{base}/collaborators"

    assert project["access"]["authorization_version"] is None
    assert (await system_admin.get(base)).json()["access"]["authorization_version"] is None
    first = (await client.post(members, json={"username": "scope-guest", "role": "editor"})).json()
    invite_url = f"/api/v1/collaboration/invitations/project/{first['invitation_id']}/accept"
    assert (await guest.post(invite_url, json={"expected_revision": first["revision"]})).status_code == 200

    initial = (await guest.get(base)).json()["access"]["authorization_version"]
    shelf = (await guest.get("/api/v1/projects")).json()[0]["access"]["authorization_version"]
    assert isinstance(initial, str) and initial and initial == shelf
    assert first["invitation_id"] not in initial

    changed = await client.patch(f"{members}/{user['id']}", json={
        "expected_revision": 2, "allow_owner_ai": True,
    })
    assert changed.status_code == 200
    changed_version = (await guest.get(base)).json()["access"]["authorization_version"]
    assert changed_version and changed_version != initial

    assert (await client.delete(f"{members}/{user['id']}", params={"expected_revision": 3})).status_code == 204
    assert (await guest.get(base)).status_code == 404
    second = (await client.post(members, json={
        "username": "scope-guest", "role": "editor", "allow_owner_ai": True,
    })).json()
    assert second["invitation_id"] != first["invitation_id"]
    invite_url = f"/api/v1/collaboration/invitations/project/{second['invitation_id']}/accept"
    assert (await guest.post(invite_url, json={"expected_revision": second["revision"]})).status_code == 200
    renewed = (await guest.get(base)).json()["access"]["authorization_version"]
    assert renewed and renewed not in {initial, changed_version}
    assert second["invitation_id"] not in renewed
