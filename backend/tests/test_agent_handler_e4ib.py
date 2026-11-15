"""E4i-b：agent.turn 受理 / 重试 / 续跑 / 构建接受 accepted editor（模型来源、grant、配置归属）。

协作者 HTTP 入口仍关闭（E4i-d 开放），这里按生产 send_message 的顺序直接走中央受理：锁外模型预检 →
受理证明 → create_task，随后由真实任务引擎领取、构建并运行（FakeLLM）。
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    AIConfig,
    GenerationLog,
    Project,
    ProjectCollaborator,
    Task,
    User,
)
from app.schemas.agent import SendMessage
from app.services import ai_dispatch, project_ai_config, task_service
from app.services import authorization_service as authz
from app.services.agent import access as agent_access
from app.services.agent import context, mcp_tools, runner, service, transactions
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.admission import prepare_task_admission
from app.services.task_engine.execution_identity import from_queued_task, require_write_execution
from app.services.task_engine.handlers import agent as agent_handler
from tests.helpers import create_ai_config, create_project, register, task_params, wait_task
from tests.test_agent_access_e4ia import _revoke, _seed_turn, _set_role
from tests.test_agent_runtime import call

OWNER_KEY = "sk-owner-e4ib-000001"
EDITOR_KEY = "sk-editor-e4ib-00001"


async def _invite(owner, member, project_id, username, *, allow_owner_ai=False, role="editor"):
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


async def _world(app, owner_client, editor_client, *, allow_owner_ai=False, editor_config=True):
    owner = await register(owner_client, "e4ib-owner")
    await create_ai_config(owner_client, api_key=OWNER_KEY)
    project = await create_project(owner_client)
    editor = await register(editor_client, "e4ib-editor")
    editor_cfg = await create_ai_config(editor_client, api_key=EDITOR_KEY) if editor_config else None
    member = await _invite(owner_client, editor_client, project["id"], "e4ib-editor", allow_owner_ai=allow_owner_ai)
    async with app.state.sessionmaker() as db:
        # 与真实入口（E4i-d）一致：协作者会话记录创建时的邀请轮次。
        conversation = AgentConversation(user_id=editor["id"], project_id=project["id"],
                                         collaboration_invitation_id=member["invitation_id"])
        db.add(conversation)
        await db.commit()
        cid = conversation.id
    return SimpleNamespace(
        owner=owner, editor=editor, project=project, member=member, conversation_id=cid,
        grant=(member["invitation_id"], member["revision"]), editor_cfg=editor_cfg,
    )


async def _actor(app, user_id):
    async with app.state.sessionmaker() as db:
        user = await db.get(User, user_id)
        return AuthenticatedActor(user.id, user.token_version)


async def _send(app, user_id, project_id, conversation_id, content="请帮我创作", config_id=None):
    """与生产 send_message 同序：锁外预检 → 受理证明 → 中央 create_task（入口开关留给 E4i-d）。"""
    actor = await _actor(app, user_id)
    ctx = app.state.task_engine.ctx
    preflight = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher, config_id=config_id,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    params = {**SendMessage(content=content, ai_config_id=config_id).model_dump(mode="json"),
              "project_id": project_id, "conversation_id": conversation_id}
    admission = await prepare_task_admission(ctx, actor, "agent.turn", params, model_preflight=preflight)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        task = await task_service.create_task(db, app.state.task_engine, user_id, "agent.turn", params,
                                              admission=admission)
    return task.model_dump(mode="json")


async def _task(app, task_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Task, task_id)


# ---------------- 模型来源（裁决 1 / R4） ----------------


async def test_editor_turn_uses_own_model(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _world(app, client, editor_client, allow_owner_ai=True, editor_config=True)
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    done = await wait_task(editor_client, task)
    assert done["status"] == "succeeded", done
    assert [item["api_key"] for item in fake_llm.tool_calls] == [EDITOR_KEY]
    stored = await _task(app, task["id"])
    binding = stored.input["_project_model"]
    assert binding["source"] == "actor" and binding["config_owner_id"] == world.editor["id"]
    assert stored.input["_collaboration_grant"] == {"invitation_id": world.grant[0], "revision": world.grant[1]}
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task["id"]))
        assert log.actor_user_id == world.editor["id"] and log.model_source == "actor"
        assert log.model_config_owner_id == world.editor["id"]


async def test_editor_without_config_falls_back_to_owner_model_when_granted(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _world(app, client, editor_client, allow_owner_ai=True, editor_config=False)
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    done = await wait_task(editor_client, task)
    assert done["status"] == "succeeded", done
    assert [item["api_key"] for item in fake_llm.tool_calls] == [OWNER_KEY]
    stored = await _task(app, task["id"])
    assert stored.user_id == world.editor["id"]
    binding = stored.input["_project_model"]
    assert binding["source"] == "owner" and binding["config_owner_id"] == world.owner["id"]
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task["id"]))
        # 费用来源记作者配置，actor 仍为真实触发者。
        assert log.actor_user_id == world.editor["id"] and log.model_source == "owner"
        assert log.model_config_owner_id == world.owner["id"]


@pytest.mark.parametrize("case", ["invalid_own_config", "no_grant_no_config"])
async def test_editor_model_does_not_fall_back_wrongly(app, client, make_client, fake_llm, case):
    editor_client = make_client()
    world = await _world(app, client, editor_client, allow_owner_ai=case == "invalid_own_config",
                         editor_config=case == "invalid_own_config")
    if case == "invalid_own_config":
        # 本人配置存在但无效：明确报错，不静默改用作者配置。
        async with app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == world.editor_cfg["id"]).values(
                api_key_enc="损坏的密文",
            ))
            await db.commit()
    with pytest.raises(BadRequestError):
        await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    assert not fake_llm.tool_calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Task.id).where(Task.user_id == world.editor["id"])) is None


async def test_author_turn_unchanged(app, client, fake_llm):
    owner = await register(client, "e4ib-author")
    await create_ai_config(client, api_key=OWNER_KEY)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    conversation = (await client.post(base + "/conversations", json={})).json()
    response = await client.post(base + f"/conversations/{conversation['id']}/messages", json={"content": "你好"})
    assert response.status_code == 202, response.text
    done = await wait_task(client, response.json()["task"])
    assert done["status"] == "succeeded"
    stored = await _task(app, done["id"])
    assert "_collaboration_grant" not in stored.input and stored.user_id == owner["id"]
    assert stored.input["_project_model"]["source"] == "actor"
    assert [item["api_key"] for item in fake_llm.tool_calls] == [OWNER_KEY]


# ---------------- 撤权后零模型请求 ----------------


class _PausedProvider:
    """在受控发送之前停住：撤权在 build_job 之后、真实发送之前提交。"""

    def __init__(self, inner, reached, release):
        self._inner, self._reached, self._release = inner, reached, release

    def chat_tools_stream(self, *args, **kwargs):
        async def stream():
            self._reached.set()
            await self._release.wait()
            async for item in self._inner.chat_tools_stream(*args, **kwargs):
                yield item
        return stream()


@pytest.mark.parametrize(("change", "fallback"), [
    ("revoke", False), ("viewer", False), ("revoke", True), ("model_off", True),
])
async def test_authorization_change_before_send_yields_zero_model_requests(
    app, client, make_client, fake_llm, monkeypatch, change, fallback,
):
    editor_client = make_client()
    world = await _world(app, client, editor_client, allow_owner_ai=True, editor_config=not fallback)
    reached, release = asyncio.Event(), asyncio.Event()
    original = context.guard_agent_provider

    async def paused(*args, **kwargs):
        return _PausedProvider(await original(*args, **kwargs), reached, release)

    monkeypatch.setattr(context, "guard_agent_provider", paused)
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    try:
        await asyncio.wait_for(reached.wait(), 10)
        base = f"/api/v1/projects/{world.project['id']}"
        if change == "revoke":
            await _revoke(client, world.project["id"], world.editor["id"], world.member["revision"])
        elif change == "viewer":
            await _set_role(client, world.project["id"], world.editor["id"], world.member["revision"], "viewer")
        else:
            response = await client.patch(f"{base}/collaborators/{world.editor['id']}", json={
                "expected_revision": world.member["revision"], "allow_owner_ai": False,
            })
            assert response.status_code == 200, response.text
    finally:
        release.set()
    async with app.state.sessionmaker() as db:
        for _ in range(500):
            row = await db.get(Task, task["id"], populate_existing=True)
            if row.status not in ("queued", "running"):
                break
            await db.rollback()
            await asyncio.sleep(0.01)
    assert row.status == "failed", (row.status, row.error)
    assert not fake_llm.tool_calls


# ---------------- 重试 / 续跑与重邀（裁决 7） ----------------


async def _failed_turn(app, editor_client, world, fake_llm):
    from app.ai.providers import AIProviderError

    fake_llm.push_tools(AIProviderError("模型调用失败：测试"))
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    done = await wait_task(editor_client, task)
    assert done["status"] == "failed", done
    return task


async def _reinvite(owner_client, editor_client, world):
    await _revoke(owner_client, world.project["id"], world.editor["id"], world.member["revision"])
    return await _invite(owner_client, editor_client, world.project["id"], "e4ib-editor", allow_owner_ai=True)


async def test_editor_retry_keeps_original_grant_and_rejects_after_reinvite(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _world(app, client, editor_client, allow_owner_ai=True, editor_config=False)
    first = await _failed_turn(app, editor_client, world, fake_llm)
    # 同一授权轮次：重试受理成功，沿用原模型来源（授权回退的作者配置）与原 grant。
    retried = await editor_client.post(f"/api/v1/tasks/{first['id']}/retry", params=task_params(first))
    assert retried.status_code == 202, retried.text
    done = await wait_task(editor_client, retried.json())
    assert done["status"] == "succeeded", done
    stored = await _task(app, done["id"])
    assert stored.retry_of_id == first["id"]
    assert stored.input["_collaboration_grant"]["invitation_id"] == world.grant[0]
    assert fake_llm.tool_calls[-1]["api_key"] == OWNER_KEY

    second = await _failed_turn(app, editor_client, world, fake_llm)
    calls = len(fake_llm.tool_calls)
    await _reinvite(client, editor_client, world)
    response = await editor_client.post(f"/api/v1/tasks/{second['id']}/retry", params=task_params(second))
    # 旧轮次绑定的模型授权随原邀请轮次失效：原模型预检即拒绝（400），不能以新轮次补签。
    assert response.status_code in (400, 404, 409), response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Task.id).where(Task.retry_of_id == second["id"])) is None
    assert len(fake_llm.tool_calls) == calls


async def test_editor_resume_rejected_after_reinvite(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _world(app, client, editor_client, allow_owner_ai=False, editor_config=True)
    failed = await _failed_turn(app, editor_client, world, fake_llm)
    async with app.state.sessionmaker() as db:
        # 构造无待确认提案的待审核轮次（只验证续跑判定，不依赖工具迁移）。
        await db.execute(update(Task).where(Task.id == failed["id"]).values(status="paused", finished_at=None))
        await db.commit()
    # 同轮次：handler 续跑判定接受原 grant。
    async with app.state.sessionmaker() as db:
        await agent_handler.AgentHandler().revalidate_resume(
            db, app.state.task_engine.ctx, await db.get(Task, failed["id"]),
        )
        await db.rollback()
    await _reinvite(client, editor_client, world)
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await agent_handler.AgentHandler().revalidate_resume(
                db, app.state.task_engine.ctx, await db.get(Task, failed["id"]),
            )
        await db.rollback()
    response = await editor_client.post(f"/api/v1/tasks/{failed['id']}/resume", params=task_params(failed))
    assert response.status_code in (404, 409), response.text
    # E4i-e（裁决 11）：旧轮次的待审核轮次不再永久停在 paused，由系统受限收尾为 failed（从未续跑）。
    await app.state.task_engine.scan_collaboration_paused()
    row = await _task(app, failed["id"])
    assert (row.status, row.execution_key) == ("failed", None)


# ---------------- runner 锁序（裁决 16，SQLite 顺序断言；死锁另见 PG 用例） ----------------


async def _running_job(app, actor_id, project_id, conversation_id, *, grant, owner_id):
    async with app.state.sessionmaker() as db:
        version = (await db.get(User, actor_id)).token_version
        data = {"request": {"conversation_id": conversation_id}, "message_id": 1,
                "_agent_actor_token_version": version, "_task_actor_token_version": version}
        if grant is not None:
            data["_collaboration_grant"] = {"invitation_id": grant[0], "revision": grant[1]}
        task = Task(user_id=actor_id, project_id=project_id, kind="agent.turn", status="queued", input=data)
        db.add(task)
        await db.flush()
        execution = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", execution.execution_key
        await db.commit()
    binding = project_ai_config.ResolvedProjectModel(
        project_id=project_id, actor_id=actor_id, project_owner_id=owner_id, model_type="text", source="actor",
        config_id=1, config_owner_id=actor_id, grant_identity=grant, config_fingerprint="0" * 64,
        config_instance_key=str(uuid4()),
    )
    return runner.AgentJob(
        task.id, actor_id, project_id, conversation_id, 1, None, None, None,
        task_key=task.statistics_key, queued_at=execution.queued_at, execution_key=execution.execution_key,
        actor_token_version=version, execution=execution, execution_state=runner._AgentExecutionState(execution),
        model_binding=binding,
    )


@pytest.mark.parametrize("actor", ["editor", "author"])
async def test_lock_execution_locks_users_before_send_write_gate(app, client, make_client, monkeypatch, actor):
    editor_client = make_client()
    world = await _world(app, client, editor_client)
    is_editor = actor == "editor"
    actor_id = world.editor["id"] if is_editor else world.owner["id"]
    job = await _running_job(app, actor_id, world.project["id"], world.conversation_id,
                             grant=world.grant if is_editor else None, owner_id=world.owner["id"])
    order = []
    original_users, original_gate = authz.lock_users, ai_dispatch.lock_visibility_write

    async def lock_users(db, *ids, **kwargs):
        order.append(("users", tuple(sorted(set(ids)))))
        return await original_users(db, *ids, **kwargs)

    async def lock_visibility_write(db, project_id, **kwargs):
        order.append(("send_gate", project_id))
        return await original_gate(db, project_id, **kwargs)

    monkeypatch.setattr(authz, "lock_users", lock_users)
    monkeypatch.setattr(ai_dispatch, "lock_visibility_write", lock_visibility_write)
    async with transactions.session(app.state.sessionmaker) as db:
        await runner._lock_execution(db, job)
        gate_at = order.index(("send_gate", world.project["id"]))
        locked_before = {user for kind, ids in order[:gate_at] if kind == "users" for user in ids}
        if is_editor:
            # 作者 User 与 actor 一起按升序先于发送写门禁锁定；工具随后的复核只是重入。
            assert ("users", tuple(sorted((world.editor["id"], world.owner["id"])))) in order[:gate_at]
            await agent_access.lock_agent_write(db, actor_id, world.project["id"], grant=world.grant,
                                                visibility_write=False)
            later = {user for kind, ids in order[gate_at:] if kind == "users" for user in ids}
            assert later <= locked_before
        else:
            # 作者轮次行为不变：只锁本人。
            assert locked_before == {world.owner["id"]}
        await db.rollback()


async def test_require_write_execution_rejects_wrong_owner(app, client, make_client):
    editor_client = make_client()
    world = await _world(app, client, editor_client)
    job = await _running_job(app, world.editor["id"], world.project["id"], world.conversation_id,
                             grant=world.grant, owner_id=world.owner["id"])
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await require_write_execution(db, job.execution, project_owner_id=world.editor["id"])
        await db.rollback()
        third = await register(make_client(), "e4ib-third")
        with pytest.raises(ConflictError):
            await require_write_execution(db, job.execution, project_owner_id=third["id"])
        await db.rollback()
        await require_write_execution(db, job.execution, project_owner_id=world.owner["id"])
        await db.rollback()


# ---------------- MCP 对编辑者关闭（裁决 2 / 15） ----------------


async def test_editor_runner_never_resolves_mcp(app, client, make_client, fake_llm, monkeypatch):
    editor_client = make_client()
    world = await _world(app, client, editor_client, editor_config=True)
    used = []

    async def build_tool_map(*args, **kwargs):
        used.append("build")
        return {"mcp_fake": {"plugin_id": 1, "name": "fake"}}

    async def tool_specs(*args, **kwargs):
        used.append("specs")
        return [{"name": "mcp_fake", "description": "外部", "parameters": {"type": "object"}}]

    async def resolve(*args, **kwargs):
        used.append("resolve")
        return None

    monkeypatch.setattr(mcp_tools, "build_tool_map", build_tool_map)
    monkeypatch.setattr(mcp_tools, "tool_specs", tool_specs)
    monkeypatch.setattr(mcp_tools, "resolve", resolve)
    fake_llm.push_tools([call("mcp_fake", {"q": "x"})])
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    done = await wait_task(editor_client, task)
    assert done["status"] == "succeeded", done
    assert used == []
    assert all(tool["name"] != "mcp_fake" for tool in fake_llm.tool_calls[0]["tools"])
    stored = await _task(app, task["id"])
    assert stored.checkpoint["mcp_tool_map"] == {}
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AgentChange.id).where(AgentChange.turn_task_id == task["id"])) is None
        result = await db.scalar(select(AgentMessage.content).where(
            AgentMessage.turn_task_id == task["id"], AgentMessage.role == "tool",
        ))
        assert "工具不存在" in result


async def test_external_call_refuses_collaboration_job(app, client, make_client):
    editor_client = make_client()
    world = await _world(app, client, editor_client)
    job = await _running_job(app, world.editor["id"], world.project["id"], world.conversation_id,
                             grant=world.grant, owner_id=world.owner["id"])
    external = SimpleNamespace(label="插件", auto_confirm=True, entry={})
    async with transactions.session(app.state.sessionmaker) as db:
        events = [event async for event in runner._external_call(
            job, db, {"id": "x1", "name": "mcp_fake"}, {}, external, None, [],
        )]
    assert events == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AgentChange.id).where(AgentChange.turn_task_id == job.task_id)) is None
        content = await db.scalar(select(AgentMessage.content).where(AgentMessage.tool_call_id == "x1"))
        assert "工具不存在" in content


# ---------------- 摘要来源（裁决 13） ----------------


async def _editor_summary_execution(app, world, fake_llm):
    """编辑者真实受理的轮次；引擎停住后按领取协议冻结执行身份（与摘要测试同一形状）。"""
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.user_id == world.editor["id"]).values(context_window_k=1))
        for index in range(4):
            db.add(AgentMessage(conversation_id=world.conversation_id, role="user",
                                content=f"旧消息{index}" + "较早" * 500))
        await db.commit()
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id, "本轮输入")
    actor = await _actor(app, world.editor["id"])
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        snapshot = from_queued_task(row, str(uuid4()))
        row.status, row.execution_key = "running", snapshot.execution_key
        model = project_ai_config.deserialize_project_model(
            row.input["_project_model"], actor_id=actor.id, project_id=world.project["id"],
            project_owner_id=world.owner["id"],
        )
        await db.commit()
    return SimpleNamespace(
        task_id=snapshot.task_id, user_id=snapshot.actor_id, project_id=snapshot.project_id,
        conversation_id=world.conversation_id, input_message_id=snapshot.input["message_id"],
        task_key=snapshot.task_key, queued_at=snapshot.queued_at, execution_key=snapshot.execution_key,
        actor_token_version=snapshot.actor_token_version, task_execution_identity=snapshot,
        model_binding=model, cipher=app.state.cipher, allow_private=app.state.settings.allow_private_ai_endpoints,
    )


async def _build(app, fake_llm, execution):
    from tests.test_agent_context_execution_identity import _provider

    async with app.state.sessionmaker() as db:
        cfg = await db.get(AIConfig, execution.model_binding.config_id)
        provider = await _provider(app, cfg, fake_llm.factory)
        return await context.build_messages(
            db, await db.get(AgentConversation, execution.conversation_id),
            await db.get(Project, execution.project_id), cfg, provider,
            task_id=execution.task_id, execution=execution,
        )


async def test_editor_summary_is_marked_and_reusable_only_in_same_grant(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _world(app, client, editor_client, editor_config=True)
    execution = await _editor_summary_execution(app, world, fake_llm)
    fake_llm.push("编辑者摘要")
    await _build(app, fake_llm, execution)
    assert [item["api_key"] for item in fake_llm.calls] == [EDITOR_KEY]
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, world.conversation_id)
        assert conversation.summary == "编辑者摘要"
        assert conversation.summary_source == context.summary_source_for(world.grant)
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == execution.task_id))
        assert log.actor_user_id == world.editor["id"]
    # 同一授权轮次：本人摘要作为可信历史继续使用（不被判为不可验证）；历史仍超预算时可再次压缩。
    fake_llm.push("编辑者摘要二")
    messages, _ = await _build(app, fake_llm, execution)
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, world.conversation_id)
        assert conversation.summary_source == context.summary_source_for(world.grant)
        current = conversation.summary
    assert any(current in str(item["content"]) for item in messages)
    if len(fake_llm.calls) > 1:
        # 再次压缩时，上一份编辑者摘要随本人历史一并作为输入。
        assert "编辑者摘要" in str(fake_llm.calls[-1]["messages"])
    # 来源不明（无标记）或来自其他邀请轮次的摘要：不可验证，零发送。
    for source in (None, context.summary_source_for((str(uuid4()), 1))):
        async with app.state.sessionmaker() as db:
            await db.execute(update(AgentConversation).where(
                AgentConversation.id == world.conversation_id,
            ).values(summary_source=source))
            await db.commit()
        calls = len(fake_llm.calls)
        with pytest.raises(context.AgentHistoryUnverifiable):
            await _build(app, fake_llm, execution)
        assert len(fake_llm.calls) == calls


async def test_author_summary_with_collaborator_marker_is_unverifiable(app, client, fake_llm):
    from tests.test_agent_context_execution_identity import _setup

    execution, _ = await _setup(app, client)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AgentConversation).where(AgentConversation.id == execution.conversation_id).values(
            summary="旧摘要", summarized_until=1, summary_source=context.summary_source_for((str(uuid4()), 1)),
        ))
        await db.commit()
    style = SimpleNamespace(owner_id=execution.user_id, actor_id=execution.user_id, project_id=execution.project_id)
    conversation = SimpleNamespace(summary_source="collaborator:x:1")
    assert not context._summary_source_verified(conversation, style, execution)
    conversation.summary_source = None
    assert context._summary_source_verified(conversation, style, execution)


@pytest.mark.parametrize("change", ["revoke", "viewer"])
async def test_editor_summary_send_blocked_after_revocation(app, client, make_client, fake_llm, change):
    editor_client = make_client()
    world = await _world(app, client, editor_client, editor_config=True)
    execution = await _editor_summary_execution(app, world, fake_llm)
    if change == "revoke":
        await _revoke(client, world.project["id"], world.editor["id"], world.member["revision"])
    else:
        await _set_role(client, world.project["id"], world.editor["id"], world.member["revision"], "viewer")
    fake_llm.push("不应发送")
    with pytest.raises(AppError):
        await _build(app, fake_llm, execution)
    assert not fake_llm.calls
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, world.conversation_id)
        assert conversation.summary == "" and conversation.summary_source is None


# ---------------- 撤销授权拒绝 409（裁决 14） ----------------


async def _executed_change(app, actor_id, project_id, grant):
    _, _, _, (change_id,) = await _seed_turn(app, actor_id, project_id, grant, tool_name="update_project")
    async with app.state.sessionmaker() as db:
        await db.execute(update(AgentChange).where(AgentChange.id == change_id).values(status="executed", effects=[]))
        await db.commit()
    return change_id


@pytest.mark.parametrize("change", ["revoke", "viewer", "stale_grant"])
async def test_collaborator_undo_denied_is_409_permission_denied(app, client, make_client, change):
    editor_client = make_client()
    world = await _world(app, client, editor_client)
    change_id = await _executed_change(app, world.editor["id"], world.project["id"], world.grant)
    if change == "revoke":
        await _revoke(client, world.project["id"], world.editor["id"], world.member["revision"])
    elif change == "viewer":
        await _set_role(client, world.project["id"], world.editor["id"], world.member["revision"], "viewer")
    else:
        changed = await _set_role(client, world.project["id"], world.editor["id"], world.member["revision"], "viewer")
        await _set_role(client, world.project["id"], world.editor["id"], changed["revision"], "editor")
    async with app.state.sessionmaker() as db:
        row = await db.get(AgentChange, change_id)
        with pytest.raises(transactions.AgentAuthorizationChangedError) as denied:
            await service._undo_changes(db, world.editor["id"], world.project["id"], [row])
        assert denied.value.status_code == 409 and denied.value.extra["code"] == "permission_denied"
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change_id)).status == "executed"


async def test_collaborator_undo_allowed_with_original_grant_and_foreign_conversation_404(app, client, make_client):
    editor_client = make_client()
    world = await _world(app, client, editor_client)
    change_id = await _executed_change(app, world.editor["id"], world.project["id"], world.grant)
    async with app.state.sessionmaker() as db:
        row = await db.get(AgentChange, change_id)
        await service._undo_changes(db, world.editor["id"], world.project["id"], [row])
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change_id)).status == "undone"
    # 会话不属于本人等不存在语义仍为 404（作者查看编辑者的提案）。
    other = await _executed_change(app, world.editor["id"], world.project["id"], world.grant)
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await service.owned_change(db, world.owner["id"], world.project["id"], other, lock=True)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"],
        ))
        assert member.status == "accepted"


def test_0053_summary_source_round_trip(tmp_path):
    """0053 原位增加可空摘要来源列；已有协作来源标记时拒绝降级，无标记时删列。"""
    from pathlib import Path

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

    command.upgrade(cfg, "0053")
    assert columns()["summary_source"]["nullable"] is True
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
            conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                              "VALUES(1,1,'旧书','','','','writing')"))
            conn.execute(text("INSERT INTO agent_conversations(id,user_id,project_id,title,summary,summary_source) "
                              "VALUES(1,1,1,'会话','摘要',:source)"),
                         {"source": context.summary_source_for((str(uuid4()), 1))})
    finally:
        engine.dispose()
    # 已有协作来源标记：拒绝有损降级（删列后再升级会让编辑者摘要永久不可验证）。
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(cfg, "0052")
    assert "summary_source" in columns()
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE agent_conversations SET summary_source=NULL"))
    finally:
        engine.dispose()
    command.downgrade(cfg, "0052")
    assert "summary_source" not in columns() and "summary" in columns()


async def test_editor_turn_with_compaction_sends_main_round_after_marked_summary(app, client, make_client, fake_llm):
    """同一轮内先压缩（写入协作来源标记）再主对话：主发送守卫须以已提交的来源标记核对，不能因内存旧值失败。"""
    editor_client = make_client()
    world = await _world(app, client, editor_client, editor_config=True)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.user_id == world.editor["id"]).values(context_window_k=1))
        for index in range(4):
            db.add(AgentMessage(conversation_id=world.conversation_id, role="user",
                                content=f"旧消息{index}" + "较早" * 500))
        await db.commit()
    fake_llm.push("编辑者摘要")
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id, "本轮输入")
    done = await wait_task(editor_client, task)
    assert done["status"] == "succeeded", done
    assert fake_llm.calls and {item["api_key"] for item in fake_llm.calls} == {EDITOR_KEY}
    assert [item["api_key"] for item in fake_llm.tool_calls] == [EDITOR_KEY]
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, world.conversation_id)
        assert conversation.summary
        assert conversation.summary_source == context.summary_source_for(world.grant)
