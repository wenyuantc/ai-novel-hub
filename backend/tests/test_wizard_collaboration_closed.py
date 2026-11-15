"""向导协作边界（E4-g3c 开放 accepted editor 后改写，原关闭态安全断言不放宽）。

- viewer（即使作者授权模型）不能触发任何向导：精确 403，零任务、零文本/向量模型调用；
- 请求体伪造的内部保留键（`_collaboration_grant` / `_project_model`）一律不被信任：editor 的任务只带服务端
  锁内取得的真实邀请轮次与本人模型绑定，不能借伪造键改用作者配置或指定其他轮次。
完整的撤权时点、收据收尾与费用来源矩阵见 tests/test_collaboration_wizard_entry_e4g3c.py。
"""

from sqlalchemy import select

from app.models import Task
from tests.helpers import create_ai_config, create_project, register, wait_task


async def test_wizard_rejects_viewer_and_never_trusts_forged_internal_keys(app, client, make_client, fake_llm):
    owner = await register(client, "wizard-owner")
    editor, viewer = make_client(), make_client()
    editor_user = await register(editor, "wizard-editor")
    await register(viewer, "wizard-viewer")
    await create_ai_config(client, api_key="sk-owner-wizard-closed")
    editor_config = await create_ai_config(editor, api_key="sk-editor-wizard-closed")
    await create_ai_config(viewer)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    members = []
    for actor, username, role in ((editor, "wizard-editor", "editor"), (viewer, "wizard-viewer", "viewer")):
        invited = await client.post(f"{base}/collaborators", json={
            "username": username, "role": role, "allow_owner_ai": True,
        })
        assert invited.status_code == 201, invited.text
        invitation = invited.json()
        accepted = await actor.post(
            f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
            json={"expected_revision": invitation["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
        members.append(accepted.json())
    payload = {
        "ai_config_id": editor_config["id"],
        "_collaboration_grant": {"invitation_id": members[0]["invitation_id"], "revision": members[0]["revision"] + 7},
        "_project_model": {"source": "owner"},
    }
    for kind in ("outline", "characters", "world", "careers"):
        response = await viewer.post(f"{base}/wizard/{kind}", json=payload)
        assert response.status_code == 403, response.text
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(Task).where(Task.project_id == project["id"]))) == []
    assert not fake_llm.calls and not fake_llm.embed_calls

    fake_llm.push('[{"title":"山门","content":"云海"}]')
    created = await editor.post(f"{base}/wizard/world", json=payload)
    assert created.status_code == 202, created.text
    finished = await wait_task(editor, created.json(), within=15)
    assert finished["status"] == "succeeded", finished
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(Task).where(Task.project_id == project["id"])))
    assert len(rows) == 1 and rows[0].user_id == editor_user["id"] != owner["id"]
    assert rows[0].input["_collaboration_grant"] == {
        "invitation_id": members[0]["invitation_id"], "revision": members[0]["revision"],
    }
    model = rows[0].input["_project_model"]
    assert (model["source"], model["config_owner_id"], model["config_id"]) == (
        "actor", editor_user["id"], editor_config["id"],
    )
    assert [call["api_key"] for call in fake_llm.calls] == ["sk-editor-wizard-closed"]
    assert not fake_llm.embed_calls
