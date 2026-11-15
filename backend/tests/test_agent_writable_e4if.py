"""E4i-f 后端（裁决 23）：会话 `writable` 由服务端计算，列表 / 详情 / 新建 / 改名一致，不输出邀请 UUID。

writable 为真当且仅当：作者本人的会话；或 accepted editor（`ai.use`）且会话属于当前邀请轮次。
viewer、管理员兼 viewer、撤权后重邀的旧会话为假；409 `permission_denied` 仍是写入口兜底。
详情的 writable 进入最终投影核验快照，读取与核验之间授权变化则失败关闭（404）。
"""

from types import SimpleNamespace

import pytest

from app.services.agent import access as agent_access
from tests.helpers import create_ai_config, create_project, register

ACTORS = [
    "author", "grant_editor", "nogrant_editor", "model_toggled", "viewer", "admin_viewer", "admin_editor",
    "reinvited",
]


async def _invite(owner, member, project_id, username, *, allow_owner_ai=False):
    invitation = await owner.post(f"/api/v1/projects/{project_id}/collaborators", json={
        "username": username, "role": "editor", "allow_owner_ai": allow_owner_ai,
    })
    assert invitation.status_code == 201, invitation.text
    accepted = await member.post(
        f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
        json={"expected_revision": invitation.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def _patch_member(owner, project_id, user_id, revision, **changes):
    response = await owner.patch(f"/api/v1/projects/{project_id}/collaborators/{user_id}",
                                 json={"expected_revision": revision, **changes})
    assert response.status_code == 200, response.text
    return response.json()


async def _world(client, make_client, system_admin, actor):
    owner = await register(client, "e4if-owner")
    await create_ai_config(client, api_key="sk-owner-e4if-0001")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    member = None
    if actor == "author":
        actor_client, user = client, owner
    elif actor in {"admin_viewer", "admin_editor"}:
        actor_client = system_admin
        user = (await system_admin.get("/api/v1/auth/me")).json()
        member = await _invite(client, actor_client, project["id"], "settings_admin")
    else:
        actor_client = make_client()
        name = f"e4if-{actor.replace('_', '-')}"
        user = await register(actor_client, name)
        if actor != "grant_editor":
            await create_ai_config(actor_client, api_key="sk-actor-e4if-0001")
        member = await _invite(client, actor_client, project["id"], name, allow_owner_ai=actor == "grant_editor")
    return SimpleNamespace(owner=client, client=actor_client, user=user, project=project, base=base, member=member,
                           invitations=[member["invitation_id"]] if member else [])


async def _create(world, title="会话"):
    response = await world.client.post(f"{world.base}/conversations", json={"title": title})
    assert response.status_code == 201, response.text
    assert response.json()["writable"] is True
    return response.json()["id"]


async def _transition(world, actor):
    pid, uid = world.project["id"], world.user["id"]
    if actor in {"viewer", "admin_viewer"}:
        await _patch_member(world.owner, pid, uid, world.member["revision"], role="viewer")
    elif actor == "model_toggled":
        # 只改模型授权（revision 变、invitation_id 不变）：会话仍属当前轮次。
        await _patch_member(world.owner, pid, uid, world.member["revision"], allow_owner_ai=True)
    elif actor == "reinvited":
        response = await world.owner.delete(f"/api/v1/projects/{pid}/collaborators/{uid}",
                                            params={"expected_revision": world.member["revision"]})
        assert response.status_code == 204, response.text
        world.member = await _invite(world.owner, world.client, pid, "e4if-reinvited")
        world.invitations.append(world.member["invitation_id"])


def _no_uuid(world, text):
    for invitation_id in world.invitations:
        assert invitation_id not in text
    assert "collaboration_invitation_id" not in text


@pytest.mark.parametrize("actor", ACTORS)
async def test_writable_across_actor_matrix(app, client, make_client, system_admin, actor):
    world = await _world(client, make_client, system_admin, actor)
    cid = await _create(world)
    await _transition(world, actor)
    expected = actor not in {"viewer", "admin_viewer", "reinvited"}

    listed = await world.client.get(f"{world.base}/conversations")
    assert listed.status_code == 200, listed.text
    assert [(item["id"], item["writable"]) for item in listed.json()] == [(cid, expected)]
    _no_uuid(world, listed.text)
    detail = await world.client.get(f"{world.base}/conversations/{cid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["writable"] is expected
    _no_uuid(world, detail.text)

    renamed = await world.client.patch(f"{world.base}/conversations/{cid}", json={"title": "改名"})
    if expected:
        assert renamed.status_code == 200 and renamed.json()["writable"] is True, renamed.text
        _no_uuid(world, renamed.text)
    else:
        # writable=false 时写入口仍以 409 兜底。
        assert renamed.status_code == 409 and renamed.json()["code"] == "permission_denied", renamed.text

    if actor == "reinvited":
        # 新轮次的新会话可写，旧会话仍只读。
        fresh = await _create(world, "新轮次")
        rows = (await world.client.get(f"{world.base}/conversations")).json()
        assert {item["id"]: item["writable"] for item in rows} == {cid: False, fresh: True}
        assert (await world.client.get(f"{world.base}/conversations/{fresh}")).json()["writable"] is True


async def test_writable_turns_false_after_downgrade_and_back(app, client, make_client):
    """同一会话：editor → viewer（假）→ 恢复 editor（同一邀请轮次，真）；撤权后整体不可见。"""
    world = await _world(client, make_client, None, "nogrant_editor")
    cid = await _create(world)
    pid, uid = world.project["id"], world.user["id"]
    downgraded = await _patch_member(world.owner, pid, uid, world.member["revision"], role="viewer")
    assert (await world.client.get(f"{world.base}/conversations/{cid}")).json()["writable"] is False
    restored = await _patch_member(world.owner, pid, uid, downgraded["revision"], role="editor")
    assert (await world.client.get(f"{world.base}/conversations/{cid}")).json()["writable"] is True
    response = await world.owner.delete(f"/api/v1/projects/{pid}/collaborators/{uid}",
                                        params={"expected_revision": restored["revision"]})
    assert response.status_code == 204, response.text
    assert (await world.client.get(f"{world.base}/conversations")).status_code == 404
    assert (await world.client.get(f"{world.base}/conversations/{cid}")).status_code == 404


async def test_detail_writable_is_rechecked_by_final_projection(app, client, make_client, monkeypatch):
    """读取时算出的 writable 与核验快照重算不一致（模拟两次读取间降级）→ 详情失败关闭，不返回过期的可写。"""
    world = await _world(client, make_client, None, "nogrant_editor")
    cid = await _create(world)
    await _patch_member(world.owner, world.project["id"], world.user["id"], world.member["revision"], role="viewer")
    monkeypatch.setattr(agent_access, "conversation_writable", lambda *_args, **_kwargs: True)
    detail = await world.client.get(f"{world.base}/conversations/{cid}")
    assert detail.status_code == 404, detail.text


def test_fact_function_matches_entry_rule():
    fact = agent_access.conversation_writable_fact
    common = {"user_id": 2, "member_status": "accepted", "member_role": "editor", "member_invitation_id": "inv-a"}
    assert fact(owner_id=1, conversation_invitation_id="inv-a", **common) is True
    assert fact(owner_id=1, conversation_invitation_id="inv-b", **common) is False
    assert fact(owner_id=1, conversation_invitation_id=None, **common) is False
    assert fact(owner_id=1, conversation_invitation_id="inv-a", **{**common, "member_role": "viewer"}) is False
    assert fact(owner_id=1, conversation_invitation_id="inv-a", **{**common, "member_status": "revoked"}) is False
    assert fact(owner_id=2, conversation_invitation_id=None, **common) is True
    assert fact(owner_id=2, conversation_invitation_id="inv-a", **common) is False
    assert fact(owner_id=None, conversation_invitation_id=None, **common) is False
