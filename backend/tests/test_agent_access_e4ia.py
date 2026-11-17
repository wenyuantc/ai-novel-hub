"""E4i-a：Agent 写入的锁后能力判定、授权失效终止轮次与撤销审计协调。

E4i-d 起协作者 HTTP 入口已开放（完整角色矩阵见 test_agent_entries_e4id.py）；这里验证入口之后的
真实能力判定：作者，或原邀请轮次的 accepted editor；管理员人工来源不得并入。
"""

from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import ForbiddenError, NotFoundError
from app.models import AdminAuditLog, AgentChange, AgentConversation, AgentMessage, Project, ProjectCollaborator, Task
from app.services.agent import access as agent_access
from app.services.agent import service, transactions
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import EmptyArgs, Tool, ToolCtx
from app.services.audit_writer import AuditOperation
from tests.helpers import create_project, register
from tests.test_agent_mcp_skills import turn_action


async def _invite(owner, member, project_id, username, role="editor"):
    invitation = await owner.post(
        f"/api/v1/projects/{project_id}/collaborators", json={"username": username, "role": role},
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await member.post(
        f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
        json={"expected_revision": invitation.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def _set_role(owner, project_id, user_id, revision, role):
    response = await owner.patch(
        f"/api/v1/projects/{project_id}/collaborators/{user_id}",
        json={"expected_revision": revision, "role": role},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _revoke(owner, project_id, user_id, revision):
    response = await owner.delete(
        f"/api/v1/projects/{project_id}/collaborators/{user_id}", params={"expected_revision": revision},
    )
    assert response.status_code == 204, response.text


async def _synopsis(app, project_id):
    async with app.state.sessionmaker() as db:
        return (await db.get(Project, project_id)).synopsis


async def _tool_write(app, actor_id, project_id, grant, *, locked, method):
    """以真实 update_project 工具写入；管理员人工 content.edit 足以让该 service 自身放行。"""
    tool = get_tool("update_project")
    async with (
        transactions.session(app.state.sessionmaker) as db,
        transactions.audit_scope(db, app.state.sessionmaker) as coordinator,
    ):
        project = await db.get(Project, project_id)
        ctx = ToolCtx(db, project, actor_id, app.state.task_engine.ctx, visibility_locked=locked,
                      audit_coordinator=coordinator, grant=grant)
        try:
            result = await getattr(tool, method)(ctx, {"synopsis": "Agent 写入"})
        except BaseException:
            await db.rollback()
            raise
        await coordinator.commit(db, app.state.sessionmaker)
        return result


@pytest.mark.parametrize("method", ["preview", "execute"])
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("source", ["viewer", "revoked", "no_grant", "stale_grant"])
async def test_admin_source_never_authorizes_agent_write(app, client, system_admin, source, locked, method):
    await register(client, "e4ia-owner")
    await _check_admin_source(app, client, system_admin, "settings_admin", source, locked=locked, method=method)


async def _check_admin_source(app, client, admin, admin_username, source, *, locked, method):
    project = await create_project(client)
    admin_id = (await admin.get("/api/v1/auth/me")).json()["id"]
    grant = None
    if source == "viewer":
        member = await _invite(client, admin, project["id"], admin_username, role="viewer")
        grant = (member["invitation_id"], member["revision"])
    elif source == "revoked":
        member = await _invite(client, admin, project["id"], admin_username)
        grant = (member["invitation_id"], member["revision"])
        await _revoke(client, project["id"], admin_id, member["revision"])
    elif source == "stale_grant":
        member = await _invite(client, admin, project["id"], admin_username)
        grant = (member["invitation_id"], member["revision"])
        changed = await _set_role(client, project["id"], admin_id, member["revision"], "viewer")
        await _set_role(client, project["id"], admin_id, changed["revision"], "editor")
    # 管理员仍有人工 projects.edit；Agent 写入不能因此放行，且必须终止轮次而非普通工具错误。
    with pytest.raises(transactions.AgentAuthorizationChangedError):
        await _tool_write(app, admin_id, project["id"], grant, locked=locked, method=method)
    assert await _synopsis(app, project["id"]) == project["synopsis"]


@pytest.mark.parametrize("locked", [False, True])
async def test_admin_editor_with_original_grant_and_owner_can_write(app, client, system_admin, locked):
    owner = await register(client, "e4ia-owner")
    project = await create_project(client)
    admin_id = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    member = await _invite(client, system_admin, project["id"], "settings_admin")
    grant = (member["invitation_id"], member["revision"])
    result = await _tool_write(app, admin_id, project["id"], grant, locked=locked, method="execute")
    assert "error" not in result and await _synopsis(app, project["id"]) == "Agent 写入"
    # 作者轮次不绑定 grant；作者带伪造 grant 也失败关闭。
    assert "error" not in await _tool_write(app, owner["id"], project["id"], None, locked=locked, method="execute")
    with pytest.raises(transactions.AgentAuthorizationChangedError):
        await _tool_write(app, owner["id"], project["id"], grant, locked=locked, method="execute")


async def test_entry_locks_follow_original_editor_grant(app, client, system_admin, make_client):
    await register(client, "e4ia-owner")
    project = await create_project(client)
    editor_client = make_client()
    editor = await register(editor_client, "e4ia-editor")
    member = await _invite(client, editor_client, project["id"], "e4ia-editor")
    grant = (member["invitation_id"], member["revision"])
    async with app.state.sessionmaker() as db:
        # E4i-d：提交后复核按原轮次 grant 放行编辑者；不带 grant（作者轮次形状）为授权失效 409。
        with pytest.raises(transactions.AgentAuthorizationChangedError):
            await service._lock_owned_project(db, editor["id"], project["id"], grant=None)
        await db.rollback()
        assert (await service._lock_owned_project(db, editor["id"], project["id"], grant=grant)).id == project["id"]
        await db.rollback()
        # 私人作用域已开放，但不存在 / 不属于本人的提案仍 404。
        with pytest.raises(NotFoundError):
            await service.owned_change(db, editor["id"], project["id"], 1, lock=True)
        await db.rollback()
        # 同一函数在锁后复核模式下放行原轮次的 accepted editor。
        locked = await agent_access.lock_agent_write(db, editor["id"], project["id"], grant=grant)
        assert locked.id == project["id"]
        await db.rollback()


async def _seed_turn(app, actor_id, project_id, grant, *, tool_name, count=1, preview=None, arguments=None):
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(project_id=project_id, user_id=actor_id)
        db.add(conversation)
        await db.flush()
        message = AgentMessage(conversation_id=conversation.id, role="user", content="当前作品的工作")
        db.add(message)
        await db.flush()
        data = {"request": {"conversation_id": conversation.id}, "message_id": message.id, "history_epoch": 0}
        if grant is not None:
            data["_collaboration_grant"] = {"invitation_id": grant[0], "revision": grant[1]}
        task = Task(user_id=actor_id, project_id=project_id, kind="agent.turn", status="paused", input=data)
        db.add(task)
        await db.flush()
        message.turn_task_id, message.turn_task_key = task.id, task.statistics_key
        rows = [AgentChange(
            conversation_id=conversation.id, project_id=project_id, turn_task_id=task.id,
            turn_task_key=task.statistics_key, tool_call_id=f"e4ia-{index}", tool_name=tool_name, kind="small",
            arguments=arguments or {}, preview=preview or {"ok": True}, seq=index + 1,
        ) for index in range(count)]
        db.add_all(rows)
        await db.commit()
        return conversation.id, task.id, task.statistics_key, [row.id for row in rows]


async def test_confirm_path_rechecks_inside_tool(app, client, make_client, monkeypatch):
    """确认路径持锁后把 ToolCtx 设为 visibility_locked；工具内仍须锁后复核，不能沿用入口时的判定。"""
    await register(client, "e4ia-owner")
    await _check_confirm_path(app, client, make_client(), monkeypatch)


async def _check_confirm_path(app, client, editor_client, monkeypatch):
    project = await create_project(client)
    editor = await register(editor_client, "e4ia-editor")
    member = await _invite(client, editor_client, project["id"], "e4ia-editor")
    grant = (member["invitation_id"], member["revision"])
    _, _, _, (change_id,) = await _seed_turn(
        app, editor["id"], project["id"], grant, tool_name="update_project", arguments={"synopsis": "Agent 写入"},
    )
    real = agent_access.lock_agent_write
    calls = []

    async def downgrade_after_first(db, user_id, project_id, **kwargs):
        calls.append(kwargs.get("visibility_write", True))
        project_row = await real(db, user_id, project_id, **kwargs)
        if len(calls) == 1:
            # 同一事务内模拟入口判定之后的降级：之后的工具复核必须读到它。
            await db.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id,
            ).values(role="viewer"))
        return project_row

    monkeypatch.setattr(agent_access, "lock_agent_write", downgrade_after_first)
    with pytest.raises(transactions.AgentAuthorizationChangedError):
        async with (
            transactions.session(app.state.sessionmaker) as db,
            transactions.audit_scope(db, app.state.sessionmaker) as coordinator,
        ):
            change = await db.get(AgentChange, change_id)
            await service.execute_change(
                db, app.state.task_engine.ctx, change, user_id=editor["id"], compare_preview=False,
                audit_coordinator=coordinator,
            )
    # 确认执行入口一次（取发送门禁）+ 工具预览内的锁后复核（同事务已持门禁，不重取）；工具执行未发生。
    assert calls == [True, False]
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, change_id)
        assert change.status == "proposed" and change.error in (None, "")
        member_row = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
        ))
        assert member_row.role == "editor"
    assert await _synopsis(app, project["id"]) == project["synopsis"]


@pytest.mark.parametrize("timing", ["after_entry", "committed"])
@pytest.mark.parametrize("actor", ["editor", "admin"])
async def test_authorization_loss_terminates_turn_instead_of_failing_change(
    app, client, make_client, system_admin, monkeypatch, actor, timing,
):
    await register(client, "e4ia-owner")
    project = await create_project(client)
    if actor == "admin":
        member_client, username = system_admin, "settings_admin"
    else:
        member_client, username = make_client(), "e4ia-editor"
        await register(member_client, username)
    actor_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
    member = await _invite(client, member_client, project["id"], username)
    grant = (member["invitation_id"], member["revision"])
    conversation_id, task_id, task_key, change_ids = await _seed_turn(
        app, actor_id, project["id"], grant, tool_name="e4ia_write", count=2,
    )
    executed = []

    async def operation(ctx, _args):
        if ctx.db._agent_audit_scope.mode == "execute":
            executed.append(True)
            (await ctx.db.get(Project, ctx.project_id)).synopsis = "Agent 写入"
        return {"ok": True}

    # E4i-d：使用真实协作者 HTTP 入口；确认执行由真实能力函数把关。
    resumed = []

    async def no_resume(*_args, **_kwargs):
        resumed.append(True)

    tool = Tool("e4ia_write", "测试", EmptyArgs, "small", operation, preview_operation=operation)
    monkeypatch.setattr(service, "maybe_resume", no_resume)
    monkeypatch.setattr(service, "get_tool", lambda name: tool if name == "e4ia_write" else None)

    base = f"/api/v1/projects/{project['id']}/agent"
    detail = await member_client.get(f"{base}/conversations/{conversation_id}")
    assert detail.status_code == 200, detail.text
    changes = [item for item in detail.json()["changes"] if item["id"] in change_ids]
    assert len(changes) == 2
    # 作者把协作者降为 viewer（管理员仍保有人工 projects.edit）。
    if timing == "committed":
        await _set_role(client, project["id"], actor_id, member["revision"], "viewer")
    else:
        async def downgrade_after_entry(db, **_kwargs):
            # 确认请求已通过入口与提案核对、尚未执行：同一事务内降级，执行前的锁后复核必须读到。
            await db.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == actor_id,
            ).values(role="viewer"))
            return False

        monkeypatch.setattr(service, "recover_external", downgrade_after_entry)
    response = await member_client.post(
        f"{base}/turns/{task_id}/confirm-all", json=turn_action({"task_key": task_key}, changes),
    )
    assert response.status_code == 409, response.text
    # 授权失效是终止整轮的策略错误，不是把第一项记 failed 后返回 200；E4i-d 起已提交的降级也在投影核对前
    # 以 permission_denied 拒绝（错误码与入口后降级一致）。
    assert response.json().get("code") == "permission_denied"
    assert executed == [] and resumed == []
    if timing == "committed":
        # E4i-e（裁决 11）：已提交的降级随后由系统受限收尾：轮次 failed、未执行的提案作废（不执行）。
        await app.state.task_engine.scan_collaboration_paused()
    expected = ("failed", "failed") if timing == "committed" else ("proposed", "paused")
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AgentChange).where(AgentChange.id.in_(change_ids))))
        assert [row.status for row in rows] == [expected[0]] * 2
        assert (await db.get(Task, task_id)).status == expected[1]
    assert executed == []
    assert await _synopsis(app, project["id"]) == project["synopsis"]


@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_undo_coordinates_nested_audit(app, client, system_admin, monkeypatch, outcome):
    """撤销链路内的服务若进入管理审计作用域，失败审计不能随保存点/根回滚丢失。"""
    await _check_undo(app, client, system_admin, monkeypatch, outcome)


async def _check_undo(app, client, admin, monkeypatch, outcome):
    owner = await register(client, "e4ia-owner")
    project = await create_project(client)
    admin_id = (await admin.get("/api/v1/auth/me")).json()["id"]
    _, _, _, (change_id,) = await _seed_turn(app, owner["id"], project["id"], None, tool_name="update_project")
    async with app.state.sessionmaker() as db:
        await db.execute(update(AgentChange).where(AgentChange.id == change_id).values(status="executed", effects=[]))
        await db.commit()
    operation_id = str(uuid4())

    async def audited_undo(proxy, project_id, _effects, *, discard_inserts=True):
        async with proxy.begin_nested():
            async with AuditOperation(
                proxy, admin_id, "project.update", "project", project_id,
                project_id=project_id, operation_id=operation_id,
            ) as audit:
                row = await proxy.get(Project, project_id)
                audit.target(row)
                row.synopsis = "撤销写入"
                if outcome == "failure":
                    raise ForbiddenError("PRIVATE-NOT-FOR-AUDIT")
                await audit.commit()
        return False

    monkeypatch.setattr(service.effects, "undo", audited_undo)
    async with app.state.sessionmaker() as db:
        row = await db.get(AgentChange, change_id)
        if outcome == "failure":
            with pytest.raises(ForbiddenError):
                await service._undo_changes(db, owner["id"], project["id"], [row])
        else:
            await service._undo_changes(db, owner["id"], project["id"], [row])
    async with app.state.sessionmaker() as db:
        events = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.operation_id == operation_id)))
        assert len(events) == 1 and events[0].result == outcome
        assert "PRIVATE-NOT-FOR-AUDIT" not in str(events[0].summary)
        change = await db.get(AgentChange, change_id)
        stored = await db.get(Project, project["id"])
        if outcome == "success":
            assert change.status == "undone" and stored.synopsis == "撤销写入"
        else:
            assert change.status == "executed" and stored.synopsis == project["synopsis"]










@pytest.mark.parametrize("source", ["viewer", "revoked", "stale_grant", "foreign_grant"])
async def test_plain_collaborator_without_original_editor_grant_is_denied(app, client, make_client, source):
    """复核补充：非管理员协作者降为 viewer、撤权、角色往返或持他人轮次 grant 时，锁后复核同样终止。"""
    await register(client, "e4ia-owner")
    project = await create_project(client)
    editor_client = make_client()
    editor = await register(editor_client, "e4ia-editor")
    member = await _invite(client, editor_client, project["id"], "e4ia-editor")
    grant = (member["invitation_id"], member["revision"])
    if source == "viewer":
        await _set_role(client, project["id"], editor["id"], member["revision"], "viewer")
    elif source == "revoked":
        await _revoke(client, project["id"], editor["id"], member["revision"])
    elif source == "stale_grant":
        changed = await _set_role(client, project["id"], editor["id"], member["revision"], "viewer")
        await _set_role(client, project["id"], editor["id"], changed["revision"], "editor")
    else:
        other_client = make_client()
        await register(other_client, "e4ia-other")
        other = await _invite(client, other_client, project["id"], "e4ia-other")
        grant = (other["invitation_id"], other["revision"])
    for locked in (False, True):
        with pytest.raises(transactions.AgentAuthorizationChangedError):
            await _tool_write(app, editor["id"], project["id"], grant, locked=locked, method="execute")
    assert await _synopsis(app, project["id"]) == project["synopsis"]


async def test_undo_grant_fails_closed_on_mixed_or_broken_grants(app, client, make_client):
    """复核补充：撤销批次的轮次 grant 缺失、破损或不一致时失败关闭，不退化为作者撤销。"""
    owner = await register(client, "e4ia-owner")
    project = await create_project(client)
    editor_client = make_client()
    editor = await register(editor_client, "e4ia-editor")
    member = await _invite(client, editor_client, project["id"], "e4ia-editor")
    grant = (member["invitation_id"], member["revision"])
    _, author_task, _, (author_change,) = await _seed_turn(app, owner["id"], project["id"], None, tool_name="x")
    _, editor_task, _, (editor_change,) = await _seed_turn(app, editor["id"], project["id"], grant, tool_name="x")
    async with app.state.sessionmaker() as db:
        rows = {row.id: row for row in await db.scalars(
            select(AgentChange).where(AgentChange.id.in_((author_change, editor_change))))}
        assert await service._undo_grant(db, [rows[author_change]]) is None
        assert await service._undo_grant(db, [rows[editor_change]]) == grant
        with pytest.raises(transactions.AgentAuthorizationChangedError):
            await service._undo_grant(db, [rows[author_change], rows[editor_change]])
        task = await db.get(Task, editor_task)
        task.input = {**task.input, "_collaboration_grant": {"invitation_id": "broken"}}
        await db.flush()
        with pytest.raises(transactions.AgentAuthorizationChangedError):
            await service._undo_grant(db, [rows[editor_change]])
        await db.rollback()
    assert author_task != editor_task
