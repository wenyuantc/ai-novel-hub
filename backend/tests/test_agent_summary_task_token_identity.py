"""摘要模型发送和写回必须沿用请求受理时的双重任务认证代次。"""

import asyncio

import pytest
from sqlalchemy import func, select, update

from app.models import AgentConversation, AIConfig, GenerationLog, Project, Task
from app.services.agent import context
from app.services.ai_dispatch import VisibilityPolicyError
from tests.helpers import Gate
from tests.test_agent_context_execution_identity import _provider, _setup


async def _summarize(app, fake_llm, execution, config_id):
    async with app.state.sessionmaker() as db:
        cfg = await db.get(AIConfig, config_id)
        # 与生产 handler 相同：经受控工厂登记并使用原配置解密密钥构造。
        provider = await _provider(app, cfg, fake_llm.factory)
        return await context.build_messages(
            db, await db.get(AgentConversation, execution.conversation_id),
            await db.get(Project, execution.project_id), cfg,
            provider, task_id=execution.task_id, execution=execution,
        )


async def _tamper(app, execution, kind):
    async with app.state.sessionmaker() as db:
        data = dict((await db.get(Task, execution.task_id)).input)
        if kind == "changed":
            data["_task_actor_token_version"] = execution.actor_token_version + 1
        elif kind == "bool":
            # 数据库写入必须发生，即使 bool 与原 0/1 整数在 Python 中相等。
            data["_task_actor_token_version"] = bool(execution.actor_token_version)
        else:
            data.pop("_task_actor_token_version")
        await db.execute(update(Task).where(Task.id == execution.task_id).values(input=data))
        await db.commit()


@pytest.mark.parametrize("kind", ["changed", "missing", "bool"])
async def test_unproven_task_actor_version_blocks_summary_before_send(app, client, fake_llm, kind):
    execution, config_id = await _setup(app, client)
    await _tamper(app, execution, kind)
    fake_llm.push("不应发送的摘要")
    with pytest.raises(VisibilityPolicyError):
        await _summarize(app, fake_llm, execution, config_id)
    assert not fake_llm.calls


@pytest.mark.parametrize("kind", ["changed", "missing", "bool"])
async def test_changed_task_actor_version_blocks_summary_commit(app, client, fake_llm, kind):
    execution, config_id = await _setup(app, client)
    gate = Gate()
    fake_llm.push([gate, "不应持久化的摘要"])
    work = asyncio.create_task(_summarize(app, fake_llm, execution, config_id))
    try:
        await asyncio.wait_for(gate.wait_reached(), 10)
        await _tamper(app, execution, kind)
        gate.release()
        with pytest.raises(VisibilityPolicyError):
            await asyncio.wait_for(work, 10)
        async with app.state.sessionmaker() as db:
            conversation = await db.get(AgentConversation, execution.conversation_id)
            assert conversation.summary == "" and conversation.summarized_until is None
            assert await db.scalar(select(func.count(GenerationLog.id))) == 0
    finally:
        gate.release()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def test_original_task_and_agent_token_versions_allow_summary(app, client, fake_llm):
    execution, config_id = await _setup(app, client)
    fake_llm.push("可信摘要")
    await _summarize(app, fake_llm, execution, config_id)
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, execution.conversation_id)
        assert conversation.summary == "可信摘要" and conversation.summarized_until is not None
        assert await db.scalar(select(func.count(GenerationLog.id))) == 1
