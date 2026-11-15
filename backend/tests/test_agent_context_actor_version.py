"""Agent 摘要和主模型必须继承 HTTP 接受时的原始认证代次。"""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.models import AgentChange, AgentConversation, AgentMessage, AIConfig, GenerationLog, Project, Task, User
from app.services.agent import context
from app.services.ai_dispatch import VisibilityPolicyError
from tests.helpers import Gate
from tests.test_agent_context_execution_identity import _provider, _setup


async def _guarded(app, fake_llm, execution, config_id):
    async with app.state.sessionmaker() as db:
        config = await db.get(AIConfig, config_id)
        guard = await context.guard_agent_provider(
            db, await db.get(AgentConversation, execution.conversation_id),
            await db.get(Project, execution.project_id), config,
            await _provider(app, config, fake_llm.factory), execution=execution,
        )
        await db.commit()
        return guard


@pytest.mark.parametrize(
    "change", ["token_revoked", "task_version_changed", "task_version_missing", "task_version_bool"],
)
async def test_main_model_cannot_send_after_original_actor_version_changes(app, client, fake_llm, change):
    execution, config_id = await _setup(app, client)
    guarded = await _guarded(app, fake_llm, execution, config_id)
    async with app.state.sessionmaker() as db:
        if change == "token_revoked":
            user = await db.get(User, execution.user_id)
            user.token_version += 1
        else:
            task = await db.get(Task, execution.task_id)
            data = dict(task.input)
            if change == "task_version_changed":
                data["_agent_actor_token_version"] = execution.actor_token_version + 1
            elif change == "task_version_bool":
                data["_agent_actor_token_version"] = True
            else:
                data.pop("_agent_actor_token_version")
            task.input = data
        await db.commit()
    fake_llm.push_tools([("text", {"text": "不应发送"})])
    with pytest.raises(VisibilityPolicyError):
        _ = [event async for event in guarded.chat_tools_stream([{"role": "user", "content": "旧内容"}], "model", [])]
    assert fake_llm.tool_calls == []


def test_model_history_never_contains_agent_actor_token_evidence():
    data = {"error": "操作未完成", "nested": json.dumps({"_agent_actor_token_version": "private-version"})}
    change = AgentChange(kind="external", status="failed", result=data)
    message = AgentMessage(role="tool", tool_name="external", tool_call_id="c", content=json.dumps({"data": data}))
    for projected in (context.change_result_for_model(change), context.message_dict(message)):
        result = json.dumps(projected, ensure_ascii=False)
        assert "操作未完成" in result
        assert "_agent_actor_token_version" not in result and "private-version" not in result
    assert change.result == data


@pytest.mark.parametrize("version", [None, True, -1])
async def test_job_without_original_actor_version_is_rejected_before_provider(app, client, fake_llm, version):
    execution, config_id = await _setup(app, client)
    execution.actor_token_version = version
    with pytest.raises(VisibilityPolicyError):
        await _guarded(app, fake_llm, execution, config_id)
    assert fake_llm.tool_calls == []


async def test_summary_cannot_commit_after_original_token_is_revoked(app, client, fake_llm):
    execution, config_id = await _setup(app, client)
    gate = Gate()
    fake_llm.push([gate, "旧令牌摘要"])

    async def summarize():
        async with app.state.sessionmaker() as db:
            config = await db.get(AIConfig, config_id)
            return await context.build_messages(
                db, await db.get(AgentConversation, execution.conversation_id),
                await db.get(Project, execution.project_id), config, await _provider(app, config, fake_llm.factory),
                task_id=execution.task_id, execution=execution,
            )

    work = asyncio.create_task(summarize())
    try:
        await asyncio.wait_for(gate.wait_reached(), 10)
        async with app.state.sessionmaker() as db:
            (await db.get(User, execution.user_id)).token_version += 1
            await db.commit()
        gate.release()
        with pytest.raises(VisibilityPolicyError):
            await asyncio.wait_for(work, 10)
        async with app.state.sessionmaker() as db:
            conversation = await db.get(AgentConversation, execution.conversation_id)
            assert conversation.summary == "" and conversation.summarized_until is None
            assert await db.scalar(select(func.count(GenerationLog.id))) == 0
            assert await db.scalar(select(func.count(AgentMessage.id))) == 5
    finally:
        gate.release()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
