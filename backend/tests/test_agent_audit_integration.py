"""通过真实 Agent HTTP 确认链测试显式保存点审计协调。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.core.errors import ForbiddenError
from app.models import AdminAuditLog, AgentChange, AgentConversation, AgentMessage, Project, Task, User
from app.services import project_ai_config
from app.services.agent import history, runner, service, transactions
from app.services.agent.tools.base import EmptyArgs, Tool
from app.services.audit_writer import AuditOperation
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import create_project, register
from tests.test_agent_audit_coordinator import _write_project
from tests.test_agent_mcp_skills import change_action


async def _prepare(app, owner, admin, monkeypatch, action):
    await register(owner, "agent-integration-owner")
    project = await create_project(owner)
    manager = (await admin.get("/api/v1/auth/me")).json()["id"]
    base = f"/api/v1/projects/{project['id']}"
    invitation = await owner.post(f"{base}/collaborators", json={"username": "settings_admin", "role": "editor"})
    assert invitation.status_code == 201, invitation.text
    path = f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept"
    accepted = await admin.post(path, json={"expected_revision": 1})
    assert accepted.status_code == 200

    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(project_id=project["id"], user_id=manager)
        db.add(conversation)
        await db.flush()
        message = AgentMessage(conversation_id=conversation.id, role="user", content="当前作品的工作")
        db.add(message)
        await db.flush()
        task = Task(
            user_id=manager, project_id=project["id"], kind="agent.turn", status="paused",
            input={"request": {"conversation_id": conversation.id}, "message_id": message.id, "history_epoch": 0,
                   "_collaboration_grant": {"invitation_id": invitation.json()["invitation_id"],
                                            "revision": accepted.json()["revision"]}},
        )
        db.add(task)
        await db.flush()
        message.turn_task_id = task.id
        message.turn_task_key = task.statistics_key
        rows = [AgentChange(
            conversation_id=conversation.id, project_id=project["id"], turn_task_id=task.id,
            turn_task_key=task.statistics_key,
            tool_call_id=f"test-{index}", tool_name="test_audit", kind="small", arguments={}, preview={"ok": True},
        ) for index in range(2)]
        db.add_all(rows)
        await db.commit()
        conversation_id, change_id = conversation.id, rows[0].id

    # E4i-d：使用真实协作者 HTTP 入口（管理员兼 accepted editor、原轮次 grant），
    # 入口之后的确认执行与工具写入走真实的锁后能力判定。
    async def no_resume(*_args, **_kwargs):
        # 派生任务与续跑尚未开放，本测试只核实当前确认事务。
        return None

    ids = []

    async def operation(ctx, _args):
        operation_id = str(uuid4())
        ids.append(operation_id)
        mode = ctx.db._agent_audit_scope.mode
        if action == "fatal" and mode == "execute":
            async with AuditOperation(
                ctx.db, manager, "project.update", "project", project["id"],
                project_id=project["id"], operation_id=operation_id,
            ):
                raise transactions.AgentAuthorizationChangedError()
        if action == "preview_changed" and mode == "preview":
            try:
                await _write_project(ctx.db, manager, project["id"], operation_id, failure=True)
            except ForbiddenError:
                # 真实工具可自行处理拒绝；先前的管理失败仍须随预览更新被保存。
                return {"ok": True, "changed": True}
        await _write_project(
            ctx.db, manager, project["id"], operation_id,
            failure=(action == f"{mode}_failure"),
        )
        return {"ok": True}

    tool = Tool("test_audit", "测试", EmptyArgs, "small", operation, preview_operation=operation)
    monkeypatch.setattr(service, "maybe_resume", no_resume)
    monkeypatch.setattr(service, "get_tool", lambda name: tool if name == "test_audit" else None)

    detail = await admin.get(f"{base}/agent/conversations/{conversation_id}")
    assert detail.status_code == 200, detail.text
    change = next(item for item in detail.json()["changes"] if item["id"] == change_id)
    assert change["change_key"] and change["view_version"] and change["turn_task_key"]
    return project, change, ids


@pytest.mark.parametrize("action", ["success", "preview_failure", "execute_failure", "fatal", "preview_changed"])
async def test_http_confirm_coordinates_nested_audit(app, client, system_admin, monkeypatch, action):
    project, change, ids = await _prepare(app, client, system_admin, monkeypatch, action)
    change_id = change["id"]
    response = await system_admin.post(
        f"/api/v1/projects/{project['id']}/agent/changes/{change_id}/confirm", json=change_action(change),
    )
    assert response.status_code == (409 if action in {"fatal", "preview_changed"} else 200), response.text
    assert "PRIVATE-NOT-FOR-AUDIT" not in response.text and '"_private_' not in response.text
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.operation_id.in_(ids))))
        stored = await db.get(Project, project["id"])
        change = await db.get(AgentChange, change_id)
        assert len(rows) == 1
        if action == "success":
            assert rows[0].result == "success" and stored.title == "未经提交的内容" and change.status == "executed"
        else:
            assert rows[0].result == "failure" and stored.title == project["title"]
            assert change.status == ("proposed" if action in {"fatal", "preview_changed"} else "failed")
            if action == "preview_changed":
                assert change.preview == {"ok": True, "changed": True}


@pytest.mark.parametrize("action", ["success", "preview_failure", "fatal"])
async def test_background_tool_loop_coordinates_each_root_commit(app, client, system_admin, monkeypatch, action):
    project, _, ids = await _prepare(app, client, system_admin, monkeypatch, action)
    manager = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    async with app.state.sessionmaker() as db:
        conversation = await db.scalar(select(AgentConversation).where(
            AgentConversation.project_id == project["id"], AgentConversation.user_id == manager,
        ))
        task = await db.scalar(select(Task).where(Task.project_id == project["id"], Task.user_id == manager))
        await db.execute(delete(AgentChange).where(AgentChange.turn_task_id == task.id))
        # 按真实领取流程：queued 行带原认证代次，经 from_queued_task 冻结执行身份后转为 running。
        token_version = (await db.get(User, manager)).token_version
        task.input = {**task.input, "_agent_actor_token_version": token_version,
                      "_task_actor_token_version": token_version}
        task.status, task.queued_at = "queued", datetime.now(UTC)
        await db.flush()
        execution_key = str(uuid4())
        execution = from_queued_task(task, execution_key)
        task.status = "running"
        task.execution_key = execution_key
        conversation.auto_execute = True
        assistant = AgentMessage(
            conversation_id=conversation.id, turn_task_id=task.id, turn_task_key=task.statistics_key,
            role="assistant", content="",
            tool_calls=[{"id": "background-test", "name": "test_audit", "arguments": "{}"}],
        )
        db.add(assistant)
        await db.flush()
        checkpoint = {"assistant_message_id": assistant.id, "allowed_calls": 1}
        stamp = await service.read_visibility_stamp(db, project["id"])
        scope = await history.build_tool_scope(db, stamp, "当前作品的工作")
        # 生产中协作轮次总带领取时冻结的模型绑定（E4i-b：运行器据此在发送写门禁前锁作者 User）。
        binding = project_ai_config.ResolvedProjectModel(
            project_id=project["id"], actor_id=manager,
            project_owner_id=(await db.get(Project, project["id"])).user_id,
            model_type="text", source="actor", config_id=1, config_owner_id=manager,
            grant_identity=execution.grant_identity, config_fingerprint="0" * 64, config_instance_key=str(uuid4()),
        )
        job = runner.AgentJob(
            task_id=task.id, user_id=manager, project_id=project["id"], conversation_id=conversation.id,
            input_message_id=task.input["message_id"], config=None, provider=None, stamp=stamp,
            task_key=task.statistics_key, queued_at=task.queued_at, execution_key=task.execution_key,
            actor_token_version=token_version, execution=execution,
            execution_state=runner._AgentExecutionState(execution), model_binding=binding,
        )
        await db.commit()

    # E4i-b 起 runner 按协作者执行身份读取作品（job_project），不再需要模拟作者入口。
    monkeypatch.setattr(runner, "get_tool", service.get_tool)
    events = []
    if action == "fatal":
        with pytest.raises(transactions.AgentAuthorizationChangedError):
            async for event in runner._process_calls(job, app.state.task_engine.ctx, checkpoint, None, scope):
                events.append(event)
    else:
        async for event in runner._process_calls(job, app.state.task_engine.ctx, checkpoint, None, scope):
            events.append(event)
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.operation_id.in_(ids))))
        stored = await db.get(Project, project["id"])
        changes = list(await db.scalars(select(AgentChange).where(AgentChange.turn_task_id == job.task_id)))
        assert len(rows) == 1
        assert rows[0].result == ("success" if action == "success" else "failure")
        assert stored.title == ("未经提交的内容" if action == "success" else project["title"])
        if action == "fatal":
            assert len(changes) == 1 and changes[0].status == "proposed"
            assert not any(event == "executed" for event, _ in events)
        if action == "preview_failure":
            assert changes == []
