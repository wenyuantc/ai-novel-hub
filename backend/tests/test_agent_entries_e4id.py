"""E4i-d：Agent HTTP 入口对协作者开放后的角色矩阵（research §9.2）。

每个参数化身份各建一个独立作品，先以“设置身份”（成员身份尚为 editor 时由其本人；陌生人用作者）经真实 HTTP
入口完成：建会话 → 发送（FakeLLM 产出 3 个写工具提案）→ 确认第一项；另一会话发送失败（供重试）；再建一个空会话。
随后按身份施加降级 / 撤权 / 重邀，再逐个入口断言状态码、副作用与管理审计。

- 可写：作者、授权编辑者（本人无模型，回退作者模型）、未授权编辑者（本人模型）、管理员兼编辑者。
- 只读本人会话（裁决 4、7）：viewer、管理员兼 viewer、撤权后重邀（旧会话 / 旧轮次）。写入口 409 permission_denied
  （裁决 14），新建会话 viewer 403；重邀者可在新轮次新建并发送。
- 不可见：已撤权、陌生人（作者对象）——一律 404。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.ai.providers.base import AIProviderError
from app.models import AdminAuditLog, AgentChange, AgentConversation, Project, Task
from tests.helpers import create_ai_config, create_project, register, task_params, wait_task
from tests.test_agent_mcp_skills import change_action, turn_action
from tests.test_agent_runtime import call

OWNER_KEY = "sk-owner-e4id-000001"
ACTOR_KEY = "sk-actor-e4id-000001"
ORIGINAL = {"synopsis": "原简介", "theme": "原主题", "audience": "原读者"}

ACTORS = [
    "author", "grant_editor", "nogrant_editor", "viewer", "admin_viewer", "admin_editor",
    "revoked", "reinvited", "stranger",
]
WRITERS = {"author", "grant_editor", "nogrant_editor", "admin_editor"}
READONLY = {"viewer", "admin_viewer", "reinvited"}
HIDDEN = {"revoked", "stranger"}
STOP = ("paused", "succeeded", "failed", "cancelled", "interrupted")


async def _invite(owner, member, project_id, username, *, role="editor", allow_owner_ai=False):
    invitation = await owner.post(f"/api/v1/projects/{project_id}/collaborators", json={
        "username": username, "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert invitation.status_code == 201, invitation.text
    accepted = await member.post(
        f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
        json={"expected_revision": invitation.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def _member_patch(owner, project_id, user_id, revision, **changes):
    response = await owner.patch(f"/api/v1/projects/{project_id}/collaborators/{user_id}",
                                 json={"expected_revision": revision, **changes})
    assert response.status_code == 200, response.text
    return response.json()


async def _revoke(owner, project_id, user_id, revision):
    response = await owner.delete(f"/api/v1/projects/{project_id}/collaborators/{user_id}",
                                  params={"expected_revision": revision})
    assert response.status_code == 204, response.text


async def _project_values(app, project_id):
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, project_id)
        return {key: getattr(project, key) for key in ORIGINAL}


async def _row(app, model, ident):
    async with app.state.sessionmaker() as db:
        return await db.get(model, ident)


async def _wait_status(app, task_id, statuses):
    from tests.helpers import wait_until

    async def reached():
        return (await _row(app, Task, task_id)).status in statuses

    await wait_until(reached, within=8.0)
    return await _row(app, Task, task_id)


async def _world(app, client, make_client, system_admin, actor):
    """返回作品、目标身份客户端与“设置身份”客户端（成员在降级前即以本人身份完成设置）。"""
    owner = await register(client, "e4id-owner")
    await create_ai_config(client, api_key=OWNER_KEY)
    project = await create_project(client)
    patched = await client.patch(f"/api/v1/projects/{project['id']}", json=ORIGINAL)
    assert patched.status_code == 200, patched.text
    member = None
    if actor == "author":
        actor_client, actor_user = client, owner
    elif actor in {"admin_viewer", "admin_editor"}:
        actor_client = system_admin
        actor_user = (await system_admin.get("/api/v1/auth/me")).json()
        await create_ai_config(actor_client, api_key=ACTOR_KEY)
        member = await _invite(client, actor_client, project["id"], "settings_admin")
    else:
        actor_client = make_client()
        actor_user = await register(actor_client, f"e4id-{actor.replace('_', '-')}")
        if actor != "grant_editor":
            await create_ai_config(actor_client, api_key=ACTOR_KEY)
        if actor != "stranger":
            username = f"e4id-{actor.replace('_', '-')}"
            member = await _invite(client, actor_client, project["id"], username,
                                   allow_owner_ai=actor == "grant_editor")
    setup_client = client if actor == "stranger" else actor_client
    return SimpleNamespace(owner=owner, owner_client=client, project=project, actor=actor_user, client=actor_client,
                           setup=setup_client, member=member,
                           base=f"/api/v1/projects/{project['id']}/agent")


async def _detail(client, base, cid):
    response = await client.get(f"{base}/conversations/{cid}")
    assert response.status_code == 200, response.text
    return response.json()


async def _new_conversation(client, base, title="会话"):
    response = await client.post(f"{base}/conversations", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _send(client, base, cid, text="请修改作品信息"):
    response = await client.post(f"{base}/conversations/{cid}/messages", json={"content": text})
    assert response.status_code == 202, response.text
    return response.json()["task"]


async def _phase_a(app, world, fake_llm):
    """以设置身份完成：c1 三个提案并确认第一项、c2 发送失败、c3 空会话。"""
    client, base = world.setup, world.base
    c1 = await _new_conversation(client, base, "主会话")
    fake_llm.push_tools([
        call("update_project", {"synopsis": "Agent 简介"}, "p1"),
        call("update_project", {"theme": "Agent 主题"}, "p2"),
        call("update_project", {"audience": "Agent 读者"}, "p3"),
    ])
    t1 = await _send(client, base, c1)
    assert (await wait_task(client, t1, within=8.0, statuses=STOP))["status"] == "paused"
    changes = (await _detail(client, base, c1))["changes"]
    assert [item["status"] for item in changes] == ["proposed"] * 3
    p1, p2, p3 = changes
    confirmed = await client.post(f"{base}/changes/{p1['id']}/confirm", json=change_action(p1))
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
    c2 = await _new_conversation(client, base, "失败会话")
    fake_llm.push_tools(AIProviderError("模型调用失败：测试"))
    t2 = await _send(client, base, c2)
    assert (await wait_task(client, t2, within=8.0, statuses=STOP))["status"] == "failed"
    c3 = await _new_conversation(client, base, "空会话")
    return SimpleNamespace(c1=c1, c2=c2, c3=c3, t1=t1, t2=t2, ids=(p1["id"], p2["id"], p3["id"]))


async def _transition(world, actor):
    pid, uid = world.project["id"], world.actor["id"]
    if actor in {"viewer", "admin_viewer"}:
        await _member_patch(world.owner_client, pid, uid, world.member["revision"], role="viewer")
    elif actor in {"revoked", "reinvited"}:
        await _revoke(world.owner_client, pid, uid, world.member["revision"])
        if actor == "reinvited":
            world.member = await _invite(world.owner_client, world.client, pid, f"e4id-{actor}")


async def _audit_count(app, actor_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count(AdminAuditLog.id)).where(AdminAuditLog.actor_user_id == actor_id))


async def _snapshot(app, world, state):
    async with app.state.sessionmaker() as db:
        changes = {row.id: row.status for row in await db.scalars(
            select(AgentChange).where(AgentChange.id.in_(state.ids)))}
        tasks = {row.id: row.status for row in await db.scalars(
            select(Task).where(Task.project_id == world.project["id"]))}
        conversations = {row.id: row.title for row in await db.scalars(
            select(AgentConversation).where(AgentConversation.project_id == world.project["id"]))}
    return changes, tasks, conversations, await _project_values(app, world.project["id"])


def _requests(base, state, views, t1, t2):
    p1, p2, _p3 = (views[ident] for ident in state.ids)
    proposed = [views[ident] for ident in state.ids[1:]]
    return [
        ("update", "PATCH", f"{base}/conversations/{state.c3}", {"title": "改名"}, None),
        ("delete", "DELETE", f"{base}/conversations/{state.c3}", None, None),
        ("send", "POST", f"{base}/conversations/{state.c3}/messages", {"content": "继续"}, None),
        ("confirm", "POST", f"{base}/changes/{p2['id']}/confirm", change_action(p2), None),
        ("reject", "POST", f"{base}/changes/{p2['id']}/reject", change_action(p2), None),
        ("confirm_all", "POST", f"{base}/turns/{t1['id']}/confirm-all", turn_action(t1, proposed), None),
        ("reject_all", "POST", f"{base}/turns/{t1['id']}/reject-all", turn_action(t1, proposed), None),
        ("undo", "POST", f"{base}/changes/{p1['id']}/undo", change_action(p1), None),
        ("undo_turn", "POST", f"{base}/turns/{t1['id']}/undo", turn_action(t1, [p1]), None),
        ("retry", "POST", f"/api/v1/tasks/{t2['id']}/retry", None, task_params(t2)),
        ("cancel", "POST", f"/api/v1/tasks/{t1['id']}/cancel", None, task_params(t1)),
    ]


@pytest.mark.parametrize("actor", ACTORS)
async def test_agent_entry_role_matrix(app, client, make_client, system_admin, fake_llm, actor):
    world = await _world(app, client, make_client, system_admin, actor)
    state = await _phase_a(app, world, fake_llm)
    # 设置阶段的视图（含原 change_key / view_version、任务身份），降级后仍用它构造请求，证明拒绝来自授权而非身份。
    views = {item["id"]: item for item in (await _detail(world.setup, world.base, state.c1))["changes"]}
    t1, t2 = state.t1, state.t2
    assert await _project_values(app, world.project["id"]) == {**ORIGINAL, "synopsis": "Agent 简介"}
    await _transition(world, actor)
    if actor in READONLY | {"revoked"}:
        # E4i-e（裁决 11）：降级 / 撤权 / 重邀后，待确认轮次由系统受限收尾为 failed，未确认提案作废。
        # 撤权通知已触发后台复查；显式复查一次使快照确定（同一扫描锁串行，幂等）。
        await app.state.task_engine.scan_collaboration_paused()
        assert (await _row(app, Task, t1["id"])).status == "failed"
    audits_before = await _audit_count(app, world.actor["id"])
    before = await _snapshot(app, world, state)
    calls_before = len(fake_llm.tool_calls)
    actor_client, base = world.client, world.base

    if actor in WRITERS:
        await _writer_flow(app, world, state, views, fake_llm)
        return

    # ---- 只读 / 不可见：读取 ----
    listed = await actor_client.get(f"{base}/conversations")
    detail = await actor_client.get(f"{base}/conversations/{state.c1}")
    logs = await actor_client.get(f"{base}/conversations/{state.c1}/generation-logs")
    if actor in READONLY:
        assert listed.status_code == 200, listed.text
        assert {item["id"] for item in listed.json()} == {state.c1, state.c2, state.c3}
        assert detail.status_code == 200, detail.text
        assert [(item["status"], item["error"]) for item in detail.json()["changes"]][1:] == [
            ("failed", "协作授权已失效，提案已作废"),
        ] * 2
        assert detail.json()["changes"][0]["status"] == "executed"
        assert logs.status_code == 200 and logs.json(), logs.text
    else:
        assert (listed.status_code, detail.status_code, logs.status_code) == (404, 404, 404)

    # ---- 写入口 ----
    for name, method, path, body, params in _requests(base, state, views, t1, t2):
        response = await actor_client.request(method, path, json=body, params=params)
        if actor in HIDDEN or name == "cancel":
            # 已撤权 / 陌生人看不到；viewer / 重邀者的旧轮次（E4i-e 起已受限收尾为 failed）取消入口维持 404。
            assert response.status_code == 404, (name, response.text)
        elif name == "retry":
            # 通用重试走中央受理（已结束任务本人可见）：viewer 与其它 AI 入口一致 403；重邀者原轮次的
            # 模型绑定按 grant 失效（E4i-b 既有口径 400）。均不建任务。
            expected = (400, None) if actor == "reinvited" else (403, "project_readonly")
            assert (response.status_code, response.json().get("code")) == expected, (name, response.text)
        else:
            assert response.status_code == 409, (name, response.text)
            assert response.json()["code"] == "permission_denied", (name, response.text)
    after = await _snapshot(app, world, state)
    assert after == before, actor
    assert len(fake_llm.tool_calls) == calls_before
    assert await _audit_count(app, world.actor["id"]) == audits_before

    created = await actor_client.post(f"{base}/conversations", json={"title": "新会话"})
    if actor in {"viewer", "admin_viewer"} | HIDDEN:
        if actor in HIDDEN:
            assert created.status_code == 404, created.text
        else:
            assert created.status_code == 403 and created.json()["code"] == "project_readonly", created.text
        assert (await _snapshot(app, world, state)) == before
    else:
        # 重邀后的新轮次：新建会话、发送均可；旧会话 / 旧轮次仍只读（上面已断言）。
        assert created.status_code == 201, created.text
        fresh = created.json()["id"]
        turn = await _send(actor_client, base, fresh, "新轮次")
        assert (await wait_task(actor_client, turn, within=8.0, statuses=STOP))["status"] == "succeeded"
        stored = await _row(app, Task, turn["id"])
        assert stored.user_id == world.actor["id"]
        assert stored.input["_collaboration_grant"] == {
            "invitation_id": world.member["invitation_id"], "revision": world.member["revision"],
        }
        async with app.state.sessionmaker() as db:
            row = await db.get(AgentConversation, fresh)
            assert row.collaboration_invitation_id == world.member["invitation_id"]
            old = await db.get(AgentConversation, state.c3)
            assert old.collaboration_invitation_id != world.member["invitation_id"]


async def _writer_flow(app, world, state, views, fake_llm):
    actor, client, base = world.actor, world.client, world.base
    pid = world.project["id"]
    collaborator = actor["id"] != world.owner["id"]
    expected_key = OWNER_KEY if (not collaborator or world.member.get("allow_owner_ai")) else ACTOR_KEY
    audits_before = await _audit_count(app, actor["id"])

    listed = await client.get(f"{base}/conversations")
    assert listed.status_code == 200 and {item["id"] for item in listed.json()} == {state.c1, state.c2, state.c3}
    p1, p2, p3 = (views[ident] for ident in state.ids)
    # 拒绝（裁决 15：写操作，锁内判定 ai.use + 原轮次）。
    rejected = await client.post(f"{base}/changes/{p2['id']}/reject", json=change_action(p2))
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected", rejected.text
    undone = await client.post(f"{base}/changes/{p1['id']}/undo", json=change_action(p1))
    assert undone.status_code == 200 and undone.json()["status"] == "undone", undone.text
    assert await _project_values(app, pid) == ORIGINAL
    # 整轮确认最后一项 → 自动续跑（真实 resume，编辑者轮次按原 grant）→ 成功后整轮撤销。
    confirmed = await client.post(f"{base}/turns/{state.t1['id']}/confirm-all", json=turn_action(state.t1, [p3]))
    assert confirmed.status_code == 200, confirmed.text
    assert [item["status"] for item in confirmed.json()["changes"]] == ["executed"]
    assert (await wait_task(client, state.t1, within=8.0, statuses=STOP))["status"] == "succeeded"
    assert (await _project_values(app, pid))["audience"] == "Agent 读者"
    detail = await _detail(client, base, state.c1)
    executed = [item for item in detail["changes"] if item["status"] == "executed"]
    undo_turn = await client.post(f"{base}/turns/{state.t1['id']}/undo", json=turn_action(state.t1, executed))
    assert undo_turn.status_code == 200, undo_turn.text
    assert await _project_values(app, pid) == ORIGINAL

    # 重试失败轮次（通用任务入口）→ 新提案 → 整轮拒绝 → 续跑成功。
    fake_llm.push_tools([call("update_project", {"theme": "重试主题"}, "r1")])
    retried = await client.post(f"/api/v1/tasks/{state.t2['id']}/retry", params=task_params(state.t2))
    assert retried.status_code == 202, retried.text
    retry_task = retried.json()
    assert (await wait_task(client, retry_task, within=8.0, statuses=STOP))["status"] == "paused"
    proposals = [item for item in (await _detail(client, base, state.c2))["changes"]
                 if item["turn_task_id"] == retry_task["id"]]
    rejected_all = await client.post(f"{base}/turns/{retry_task['id']}/reject-all",
                                     json=turn_action(retry_task, proposals))
    assert rejected_all.status_code == 200, rejected_all.text
    assert [item["status"] for item in rejected_all.json()["changes"]] == ["rejected"]
    assert (await wait_task(client, retry_task, within=8.0, statuses=STOP))["status"] == "succeeded"

    # 新会话发送、改名、日志、取消、删除。
    sent = await _send(client, base, state.c3, "你好")
    assert (await wait_task(client, sent, within=8.0, statuses=STOP))["status"] == "succeeded"
    renamed = await client.patch(f"{base}/conversations/{state.c3}", json={"title": "改名"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "改名", renamed.text
    if collaborator:
        # 裁决 2：协作者不开放 MCP 插件。
        mcp = await client.patch(f"{base}/conversations/{state.c3}", json={"mcp_plugin_ids": [1]})
        assert mcp.status_code == 403 and mcp.json()["code"] == "collaborator_mcp_unavailable", mcp.text
    logs = await client.get(f"{base}/conversations/{state.c3}/generation-logs")
    assert logs.status_code == 200 and logs.json(), logs.text
    fake_llm.push_tools([call("update_project", {"theme": "待取消"}, "x1")])
    pending = await _send(client, base, state.c1, "再改一次")
    assert (await wait_task(client, pending, within=8.0, statuses=STOP))["status"] == "paused"
    cancelled = await client.post(f"/api/v1/tasks/{pending['id']}/cancel", params=task_params(pending))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
    deleted = await client.delete(f"{base}/conversations/{state.c3}")
    assert deleted.status_code == 204, deleted.text
    assert await _project_values(app, pid) == ORIGINAL

    # 任务与费用来源：全部 agent.turn 归真实触发者；协作轮次绑定当前邀请轮次与本人优先 / 授权回退的模型。
    async with app.state.sessionmaker() as db:
        tasks = list(await db.scalars(select(Task).where(Task.project_id == pid, Task.kind == "agent.turn")))
        conversations = list(await db.scalars(select(AgentConversation).where(AgentConversation.project_id == pid)))
    assert tasks and all(task.user_id == actor["id"] for task in tasks)
    assert {conversation.user_id for conversation in conversations} == {actor["id"]}
    for task in tasks:
        model = task.input["_project_model"]
        if collaborator:
            assert task.input["_collaboration_grant"] == {
                "invitation_id": world.member["invitation_id"], "revision": world.member["revision"],
            }
            source = "owner" if world.member.get("allow_owner_ai") else "actor"
            assert (model["source"], model["config_owner_id"]) == (
                source, world.owner["id"] if source == "owner" else actor["id"],
            )
        else:
            assert "_collaboration_grant" not in task.input and model["source"] == "actor"
    assert {conversation.collaboration_invitation_id for conversation in conversations} == {
        world.member["invitation_id"] if collaborator else None,
    }
    assert {item["api_key"] for item in fake_llm.tool_calls} == {expected_key}
    # 裁决 6：管理员兼编辑者的 Agent 写入按人工路由规则记管理审计；普通编辑者与作者不产生管理审计。
    audits = await _audit_count(app, actor["id"]) - audits_before
    if world.actor.get("username") == "settings_admin":
        assert audits > 0
    else:
        assert audits == 0


async def test_admin_editor_audit_rows_follow_manual_route(app, client, system_admin, fake_llm):
    """裁决 6：管理员兼编辑者确认 / 撤销写入的审计与人工路由同一动作名，且只在成功写入时记录。"""
    world = await _world(app, client, None, system_admin, "admin_editor")
    base, pid = world.base, world.project["id"]
    cid = await _new_conversation(system_admin, base)
    fake_llm.push_tools([call("update_project", {"synopsis": "管理员 Agent"}, "a1")])
    turn = await _send(system_admin, base, cid)
    assert (await wait_task(system_admin, turn, within=8.0, statuses=STOP))["status"] == "paused"
    change = (await _detail(system_admin, base, cid))["changes"][0]
    confirmed = await system_admin.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
    assert confirmed.status_code == 200, confirmed.text
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.actor_user_id == world.actor["id"], AdminAuditLog.project_id == pid,
        )))
    assert [(row.action, row.result) for row in rows] == [("project.update", "success")]


async def test_derived_start_task_is_owned_by_editor_with_fee_source(app, client, make_client, fake_llm):
    """派生任务（start_task 确认）经真实入口：归编辑者、绑定当前邀请轮次；授权回退时费用来源为作者配置。"""
    for allow_owner_ai, own_config, name in ((True, False, "grant"), (False, True, "own")):
        editor_client = make_client()
        if name == "grant":
            await register(client, "e4id-derive-owner")
            await create_ai_config(client, api_key=OWNER_KEY)
        project = await create_project(client)
        chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters",
                                    json={"title": "启程", "content": "林远推开门。"})
        assert chapter.status_code == 201, chapter.text
        editor = await register(editor_client, f"e4id-derive-{name}")
        if own_config:
            await create_ai_config(editor_client, api_key=ACTOR_KEY)
        member = await _invite(client, editor_client, project["id"], f"e4id-derive-{name}",
                               allow_owner_ai=allow_owner_ai)
        base = f"/api/v1/projects/{project['id']}/agent"
        cid = await _new_conversation(editor_client, base)
        fake_llm.push_tools([call("start_task", {"kind": "chapter.generate",
                                                 "params": {"chapter_id": chapter.json()["id"]}}, "s1")])
        turn = await _send(editor_client, base, cid, "帮我生成这一章")
        assert (await wait_task(editor_client, turn, within=8.0, statuses=STOP))["status"] == "paused"
        change = (await _detail(editor_client, base, cid))["changes"][0]
        assert change["tool_name"] == "start_task"
        await app.state.task_engine.stop()
        confirmed = await editor_client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
        derived = await _row(app, Task, confirmed.json()["result"]["task_id"])
        assert derived.kind == "chapter.generate" and derived.user_id == editor["id"]
        assert derived.input["_collaboration_grant"] == {
            "invitation_id": member["invitation_id"], "revision": member["revision"],
        }
        model = derived.input["_project_model"]
        owner_id = (await _row(app, Project, project["id"])).user_id
        expected = ("owner", owner_id) if allow_owner_ai else ("actor", editor["id"])
        assert (model["source"], model["config_owner_id"], model["actor_id"]) == (*expected, editor["id"])
        await app.state.task_engine.start()
        # 确认后本轮自动续跑、派生任务也随引擎重启运行：必须等两者结束再进入下一轮，否则上一轮续跑会
        # 先弹出下一轮压入的 start_task 脚本（共享 fake_llm.tool_script），使下一轮直接 succeeded（E4i-f 复核定位）。
        assert (await wait_task(editor_client, turn, within=8.0))["status"] == "succeeded"
        await wait_task(editor_client, (derived.id, derived.statistics_key), within=8.0)
        assert not fake_llm.tool_script


async def test_get_conversation_never_exposes_other_members_or_author_conversations(app, client, make_client):
    """会话按创建者私有：作者看不到编辑者会话，编辑者看不到作者或其他成员会话（404，不进入列表）。"""
    await register(client, "e4id-private-owner")
    await create_ai_config(client, api_key=OWNER_KEY)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    clients = {}
    for name in ("a", "b"):
        clients[name] = make_client()
        await register(clients[name], f"e4id-private-{name}")
        await create_ai_config(clients[name], api_key=ACTOR_KEY)
        await _invite(client, clients[name], project["id"], f"e4id-private-{name}")
    owned = {"owner": await _new_conversation(client, base)}
    for name, member_client in clients.items():
        owned[name] = await _new_conversation(member_client, base)
    for viewer, viewer_client in (("owner", client), *clients.items()):
        listed = await viewer_client.get(f"{base}/conversations")
        assert [item["id"] for item in listed.json()] == [owned[viewer]]
        for other, cid in owned.items():
            if other == viewer:
                continue
            for path in (f"{base}/conversations/{cid}", f"{base}/conversations/{cid}/generation-logs"):
                assert (await viewer_client.get(path)).status_code == 404
            patched = await viewer_client.patch(f"{base}/conversations/{cid}", json={"title": "x"})
            assert patched.status_code == 404


async def test_turn_subscription_stays_404_for_collaborators(app, client, make_client, fake_llm):
    """裁决 10：协作者订阅 agent.turn 实时进度 V1 保持 404（前端改轮询）；作者不受影响。"""
    await register(client, "e4id-sse-owner")
    await create_ai_config(client, api_key=OWNER_KEY)
    project = await create_project(client)
    editor_client = make_client()
    await register(editor_client, "e4id-sse-editor")
    await create_ai_config(editor_client, api_key=ACTOR_KEY)
    await _invite(client, editor_client, project["id"], "e4id-sse-editor")
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)
    turn = await _send(editor_client, base, cid)
    assert (await wait_task(editor_client, turn, within=8.0, statuses=STOP))["status"] == "succeeded"
    stream = await editor_client.get(f"/api/v1/tasks/{turn['id']}/stream", params=task_params(turn))
    assert stream.status_code == 404, stream.text
    owner_cid = await _new_conversation(client, base)
    owner_turn = await _send(client, base, owner_cid)
    await wait_task(client, owner_turn, within=8.0)
    owner_stream = await client.get(f"/api/v1/tasks/{owner_turn['id']}/stream", params=task_params(owner_turn))
    assert owner_stream.status_code == 200


def test_0054_collaboration_round_round_trip(tmp_path):
    """0054 原位增加可空会话协作轮次列；已有标记时拒绝降级，无标记时删列。"""
    from pathlib import Path
    from uuid import uuid4

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    backend = Path(__file__).resolve().parents[1]
    db_file = tmp_path / "migrate.db"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False

    def columns():
        engine = create_engine(f"sqlite:///{db_file}")
        try:
            return {c["name"]: c for c in inspect(engine).get_columns("agent_conversations")}
        finally:
            engine.dispose()

    def execute(sql, **params):
        engine = create_engine(f"sqlite:///{db_file}")
        try:
            with engine.begin() as conn:
                conn.execute(text(sql), params)
        finally:
            engine.dispose()

    command.upgrade(cfg, "0054")
    assert columns()["collaboration_invitation_id"]["nullable"] is True
    execute("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')")
    execute("INSERT INTO projects(id,user_id,title,genre,premise,style,status) VALUES(1,1,'旧书','','','','writing')")
    execute("INSERT INTO agent_conversations(id,user_id,project_id,title,summary,collaboration_invitation_id) "
            "VALUES(1,1,1,'会话','',:round)", round=str(uuid4()))
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(cfg, "0053")
    assert "collaboration_invitation_id" in columns()
    execute("UPDATE agent_conversations SET collaboration_invitation_id=NULL")
    command.downgrade(cfg, "0053")
    assert "collaboration_invitation_id" not in columns() and "summary_source" in columns()


@pytest.mark.parametrize("binding", ["frozen", "missing"])
async def test_require_write_execution_prelocks_frozen_owner_before_send_gate(
    app, client, make_client, monkeypatch, binding,
):
    """第 8 项：handler 内未显式传作者的协作执行屏障，也按领取快照冻结的作者与 actor 升序预锁 User，
    再取发送写门禁（之后 guard.require_task_write 等锁作者只是重入）；快照缺绑定时退回原锁集合。"""
    import weakref
    from uuid import uuid4

    from app.models import User
    from app.services import ai_dispatch
    from app.services import authorization_service as authz
    from app.services.task_engine.execution_identity import from_queued_task, require_write_execution

    owner = await register(client, "e4id-lock-owner")
    project = await create_project(client)
    editor_client = make_client()
    editor = await register(editor_client, "e4id-lock-editor")
    member = await _invite(client, editor_client, project["id"], "e4id-lock-editor")
    assert owner["id"] < editor["id"]
    async with app.state.sessionmaker() as db:
        version = (await db.get(User, editor["id"])).token_version
        data = {"request": {"conversation_id": 1}, "_agent_actor_token_version": version,
                "_task_actor_token_version": version,
                "_collaboration_grant": {"invitation_id": member["invitation_id"], "revision": member["revision"]}}
        if binding == "frozen":
            data["_project_model"] = {"source": "actor", "project_owner_id": owner["id"]}
        task = Task(user_id=editor["id"], project_id=project["id"], kind="agent.turn", status="queued", input=data)
        db.add(task)
        await db.flush()
        execution = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", execution.execution_key
        await db.commit()

    order: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
    original_users, original_gate = authz.lock_users, ai_dispatch.lock_visibility_write

    async def lock_users(db, *ids, **kwargs):
        order.setdefault(db, []).append(("users", tuple(sorted(set(ids)))))
        return await original_users(db, *ids, **kwargs)

    async def lock_visibility_write(db, project_id, **kwargs):
        order.setdefault(db, []).append(("send_gate", project_id))
        return await original_gate(db, project_id, **kwargs)

    monkeypatch.setattr(authz, "lock_users", lock_users)
    monkeypatch.setattr(ai_dispatch, "lock_visibility_write", lock_visibility_write)
    async with app.state.sessionmaker() as db:
        await require_write_execution(db, execution)
        steps = list(order[db])
        await db.rollback()
    expected_users = (owner["id"], editor["id"]) if binding == "frozen" else (editor["id"],)
    assert steps == [("users", expected_users), ("send_gate", project["id"])]


@pytest.mark.parametrize("marker", ["missing", "old_round"])
async def test_central_admission_rejects_conversation_from_other_round(app, client, make_client, fake_llm, marker):
    """E4i-d 复核：裁决 7 不能只靠 HTTP 入口。绕过 owned_conversation 直接走中央受理，旧轮次 / 无标记的
    协作者会话也在 handler 锁内被拒（409 permission_denied），不建任务、不写消息、零模型调用。"""
    from uuid import uuid4

    from app.models import AgentMessage, User
    from app.schemas.agent import SendMessage
    from app.services import project_ai_config, task_service
    from app.services.agent.transactions import AgentAuthorizationChangedError
    from app.services.authorization_service import AuthenticatedActor
    from app.services.task_engine.admission import prepare_task_admission

    await register(client, "e4id-central-owner")
    project = await create_project(client)
    editor_client = make_client()
    editor = await register(editor_client, "e4id-central-editor")
    await create_ai_config(editor_client, api_key=ACTOR_KEY)
    member = await _invite(client, editor_client, project["id"], "e4id-central-editor")
    assert member["invitation_id"]
    async with app.state.sessionmaker() as db:
        row = AgentConversation(user_id=editor["id"], project_id=project["id"],
                                collaboration_invitation_id=None if marker == "missing" else str(uuid4()))
        db.add(row)
        await db.commit()
        cid = row.id
        actor = AuthenticatedActor(editor["id"], (await db.get(User, editor["id"])).token_version)
    ctx = app.state.task_engine.ctx
    preflight = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project["id"], "text", cipher=ctx.cipher, config_id=None,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    params = {**SendMessage(content="旧会话").model_dump(mode="json"), "project_id": project["id"],
              "conversation_id": cid}
    admission = await prepare_task_admission(ctx, actor, "agent.turn", params, model_preflight=preflight)
    with pytest.raises(AgentAuthorizationChangedError):
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            await task_service.create_task(db, app.state.task_engine, editor["id"], "agent.turn", params,
                                           admission=admission)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.project_id == project["id"])) == 0
        assert await db.scalar(select(func.count(AgentMessage.id)).where(AgentMessage.conversation_id == cid)) == 0
    assert fake_llm.tool_calls == []
