"""摘要生成不得将旧执行体结果写入后来复用整数 ID 的任务。"""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.core.errors import ConflictError
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AgentChange, AgentConversation, AgentMessage, AIConfig, GenerationLog, Project, Task, User
from app.services import ai_config_service, ai_dispatch, authorization_service, project_access, project_ai_config
from app.services.agent.context import build_messages, change_result_for_model, guard_agent_provider, message_dict
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine import execution_identity
from tests.helpers import Gate
from tests.test_agent_runtime import send_task, setup_agent


async def _provider(app, cfg, factory):
    return await ai_config_service.build_provider(
        cfg, app.state.cipher, ai_dispatch.chapter_provider_factory(factory), app.state.settings.ai_request_timeout,
        allow_private=app.state.settings.allow_private_ai_endpoints,
    )


async def _capture_execution(app, task_ref, actor):
    """从真实受理的 queued 行锁中保留原执行身份和 v2 模型绑定。"""
    authenticated = AuthenticatedActor(*actor)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = authenticated
        await authorization_service.require_authenticated(db, authenticated, write=True)
        project = await project_access.require_project(db, actor[0], task_ref["project_id"], "ai.use",
                                                       for_update=True, visibility_write=True)
        task = await db.scalar(select(Task).where(
            Task.id == task_ref["id"], Task.statistics_key == task_ref["task_key"],
        ).with_for_update())
        assert task is not None and task.status == "queued" and task.user_id == actor[0]
        assert task.input["_agent_actor_token_version"] == task.input["_task_actor_token_version"] == actor[1]
        model = project_ai_config.deserialize_project_model(
            task.input["_project_model"], actor_id=actor[0], project_id=project.id, project_owner_id=project.user_id,
        )
        execution_key = str(uuid4())
        snapshot = execution_identity.from_queued_task(task, execution_key)
        changed = await db.execute(update(Task).where(
            Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key, Task.status == "queued",
            Task.execution_key.is_(None), Task.queued_at == snapshot.queued_at,
        ).values(status="running", execution_key=execution_key))
        assert changed.rowcount == 1
        execution = SimpleNamespace(
            task_id=snapshot.task_id, user_id=snapshot.actor_id, project_id=snapshot.project_id,
            conversation_id=snapshot.input["request"]["conversation_id"], input_message_id=snapshot.input["message_id"],
            task_key=snapshot.task_key, queued_at=snapshot.queued_at, execution_key=snapshot.execution_key,
            actor_token_version=snapshot.actor_token_version, task_execution_identity=snapshot,
            model_binding=model, cipher=app.state.cipher, allow_private=app.state.settings.allow_private_ai_endpoints,
        )
        await db.commit()
    return execution


async def _setup(app, client, *, base_url=None):
    project, _, url = await setup_agent(client)
    await app.state.task_engine.stop()
    actor = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
    assert actor is not None
    async with app.state.sessionmaker() as db:
        conversation = await db.scalar(select(AgentConversation))
        assert actor[0] == conversation.user_id
        config = await db.scalar(select(AIConfig))
        config.context_window_k = 1
        if base_url is not None:
            config.base_url = base_url
        for i in range(4):
            db.add(AgentMessage(conversation_id=conversation.id, role="user", content=f"旧消息{i}" + "较早" * 500))
        config_id = config.id
        await db.commit()
    task_ref = await send_task(client, url, "本轮输入")
    assert task_ref["project_id"] == project["id"]
    execution = await _capture_execution(app, task_ref, actor)
    return execution, config_id


@pytest.mark.parametrize(
    "change", ["replace_task", "disable_actor", "next_round", "changed_config", "changed_history", "changed_input"],
)
async def test_summary_rejects_execution_changed_after_model_send(app, client, fake_llm, change):
    execution, config_id = await _setup(app, client)
    gate = Gate()
    fake_llm.push([gate, "旧执行体生成的摘要"])

    async def summarize():
        async with app.state.sessionmaker() as db:
            conversation = await db.get(AgentConversation, execution.conversation_id)
            project = await db.get(Project, execution.project_id)
            config = await db.get(AIConfig, config_id)
            provider = await _provider(app, config, fake_llm.factory)
            return await build_messages(
                db, conversation, project, config, provider, task_id=execution.task_id, execution=execution,
            )

    work = asyncio.create_task(summarize())
    try:
        await gate.wait_reached()
        async with app.state.sessionmaker() as db:
            if change == "replace_task":
                original = await db.get(Task, execution.task_id)
                await db.delete(original)
                await db.flush()
                db.add(Task(
                    id=execution.task_id, user_id=execution.user_id, project_id=execution.project_id,
                    kind="agent.turn", status="running", queued_at=execution.queued_at,
                    execution_key=str(uuid4()),
                    input={"request": {"conversation_id": execution.conversation_id},
                           "message_id": execution.input_message_id},
                ))
            elif change == "disable_actor":
                (await db.get(User, execution.user_id)).is_active = False
            elif change == "next_round":
                row = await db.get(Task, execution.task_id)
                row.queued_at = execution.queued_at + timedelta(seconds=1)
                row.execution_key = str(uuid4())
            elif change == "changed_config":
                (await db.get(AIConfig, config_id)).model = "另一模型"
            elif change == "changed_input":
                (await db.get(AgentMessage, execution.input_message_id)).content = "输入已变"
            else:
                oldest = await db.scalar(select(AgentMessage).where(
                    AgentMessage.conversation_id == execution.conversation_id,
                ).order_by(AgentMessage.id))
                oldest.content = "历史在摘要期间被替换"
            await db.commit()
        gate.release()
        with pytest.raises(ConflictError):
            await work
        async with app.state.sessionmaker() as db:
            conversation = await db.get(AgentConversation, execution.conversation_id)
            assert conversation.summary == "" and conversation.summarized_until is None
            assert await db.scalar(select(func.count(GenerationLog.id))) == 0
    finally:
        gate.release()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.parametrize("failure", ["no_execution", "disabled", "grant", "pending"])
async def test_summary_denies_unproven_execution_before_any_model_call(app, client, fake_llm, failure):
    execution, config_id = await _setup(app, client)
    async with app.state.sessionmaker() as db:
        if failure == "disabled":
            (await db.get(User, execution.user_id)).is_active = False
        elif failure == "grant":
            task = await db.get(Task, execution.task_id)
            task.input = {**task.input, "_collaboration_grant": {"invitation_id": str(uuid4()), "revision": 1}}
        if failure in ("disabled", "grant"):
            await db.commit()
        conversation = await db.get(AgentConversation, execution.conversation_id)
        project = await db.get(Project, execution.project_id)
        config = await db.get(AIConfig, config_id)
        if failure == "pending":
            conversation.title = "未提交的合法草稿"
        with pytest.raises(ConflictError):
            await build_messages(
                db, conversation, project, config, await _provider(app, config, fake_llm.factory),
                task_id=execution.task_id, execution=None if failure == "no_execution" else execution,
            )
        if failure == "pending":
            assert conversation.title == "未提交的合法草稿" and conversation in db.dirty
        assert not fake_llm.calls
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, execution.conversation_id)
        assert conversation.summary == "" and conversation.summarized_until is None
        assert await db.scalar(select(func.count(GenerationLog.id))) == 0


@pytest.mark.parametrize("changed", ["next_round", "edited_input", "no_execution"])
async def test_agent_main_model_guard_rechecks_original_execution_before_send(app, client, fake_llm, changed):
    execution, config_id = await _setup(app, client)
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, execution.conversation_id)
        project = await db.get(Project, execution.project_id)
        config = await db.get(AIConfig, config_id)
        provider = await _provider(app, config, fake_llm.factory)
        if changed == "no_execution":
            with pytest.raises(ConflictError):
                await guard_agent_provider(db, conversation, project, config, provider, execution=None)
            assert not fake_llm.calls
            return
        guarded = await guard_agent_provider(db, conversation, project, config, provider, execution=execution)
        await db.commit()
    async with app.state.sessionmaker() as db:
        if changed == "next_round":
            (await db.get(Task, execution.task_id)).execution_key = str(uuid4())
        else:
            (await db.get(AgentMessage, execution.input_message_id)).content = "本轮输入已改变"
        await db.commit()
    with pytest.raises(ConflictError):
        _ = [event async for event in guarded.chat_tools_stream([{"role": "user", "content": "旧负载"}], "model", [])]
    assert not fake_llm.calls


def test_old_external_results_cannot_replay_internal_keys_to_model():
    private = {"_external_claim_id": "claim-secret", "_external_task_key": "task-secret", "task_key": "key-secret"}
    change = AgentChange(kind="external", status="executed", result={"data": {"nested": [private, {"ok": True}]}})
    history = AgentMessage(
        role="tool", tool_call_id="old-call", tool_name="external",
        content='{"untrusted":true,"data":{"nested":{"_external_claim_id":"claim-secret",'
                '"_external_task_key":"task-secret","task_key":"key-secret","ok":true}}}',
    )
    for model_value in (change_result_for_model(change), message_dict(history)):
        rendered = str(model_value)
        assert "claim-secret" not in rendered and "task-secret" not in rendered and "key-secret" not in rendered
        assert "ok" in rendered
    assert change.result["data"]["nested"][0] == private  # 不修改持久内部事实。


@pytest.mark.parametrize("content", [
    '{"data":"{\\"nested\\":{\\"_external_claim_id\\":\\"claim-secret\\",\\"ok\\":true}}"}',
    '{"data":{"_external_claim_id":"claim-secret",',  # 旧数据可能在 JSON 中途截断。
])
def test_old_tool_result_with_encoded_or_truncated_private_json_is_safe(content):
    message = AgentMessage(role="tool", tool_call_id="call", tool_name="read", content=content)
    projected = message_dict(message)["content"]
    assert "claim-secret" not in projected and "_external_claim_id" not in projected
    if content.startswith('{"data":"'):
        assert json.loads(json.loads(projected)["data"])["nested"]["ok"] is True


def test_assistant_tool_call_arguments_do_not_replay_private_identity():
    message = AgentMessage(role="assistant", content="继续", tool_calls=[{
        "id": "call", "name": "read", "arguments": '{"task_key":"task-secret","safe":"可见"}',
        "provider_metadata": {"signature": "保留协议签名"},
    }])
    projected = message_dict(message)
    assert "task-secret" not in str(projected) and "可见" in str(projected)
    assert projected["tool_calls"][0]["provider_metadata"]["signature"] == "保留协议签名"


def test_failed_external_unknown_outcome_keeps_error_but_hides_all_claim_facts():
    result = {
        "error": "外部调用结果未知，请核对",
        "_external_outcome_unknown": True,
        "_external_claim_id": "claim-secret",
        "_external_task_key": "task-secret",
        "nested": json.dumps({"nested": {"_external_outcome_unknown": True, "_external_task_key": "task-secret"}}),
    }
    change = AgentChange(kind="external", status="failed", result=result)
    tool = AgentMessage(role="tool", tool_call_id="call", tool_name="external", content=json.dumps({"data": result}))
    for value in (change_result_for_model(change), message_dict(tool)):
        rendered = str(value)
        assert "外部调用结果未知，请核对" in rendered
        assert all(marker not in rendered for marker in (
            "_external_outcome_unknown", "_external_claim_id", "_external_task_key", "claim-secret", "task-secret",
        ))
    assert change.result == result
