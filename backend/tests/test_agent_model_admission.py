"""Agent 模型的 DNS 预检必须在写屏障外，锁内只能接收同一原始绑定。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import AgentMessage, AIConfig, Project, Task, User
from app.services.task_engine import admission
from tests.test_agent_runtime import setup_agent


@pytest.mark.parametrize("case", ["independent_write", "token_revoked", "config_replaced"])
async def test_agent_model_preflight_is_outside_write_locks_and_bound(app, client, fake_llm, monkeypatch, case):
    project, _, conversation_url = await setup_agent(client)
    owner = (await client.get("/api/v1/auth/me")).json()
    ready, release = asyncio.Event(), asyncio.Event()
    original = admission.ai_config_service.build_provider

    async def gated(*args, **kwargs):
        ready.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(admission.ai_config_service, "build_provider", gated)
    fake_llm.push_tools([("text", {"text": "完成"})])
    pending = asyncio.create_task(client.post(conversation_url + "/messages", json={"content": "请创作"}))
    try:
        await asyncio.wait_for(ready.wait(), 10)

        async def concurrent_write():
            async with app.state.sessionmaker() as db:
                if case == "independent_write":
                    (await db.get(Project, project["id"])).theme = "预检期间提交的内容"
                elif case == "token_revoked":
                    (await db.get(User, owner["id"])).token_version += 1
                else:
                    config = await db.scalar(select(AIConfig).where(AIConfig.user_id == owner["id"]))
                    fields = {name: getattr(config, name) for name in (
                        "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type",
                        "is_default", "thinking_level", "context_window_k", "max_output_k", "created_at",
                        "updated_at",
                    )}
                    config_id = config.id
                    await db.delete(config)
                    await db.flush()
                    db.add(AIConfig(id=config_id, instance_key=str(uuid4()), **fields))
                await db.commit()

        # 模型构建被挂起时，其他写事务应可提交；不能持用户或作品写锁等待 DNS。
        await asyncio.wait_for(concurrent_write(), 5)
        release.set()
        response = await asyncio.wait_for(pending, 15)
        if case == "independent_write":
            assert response.status_code == 202, response.text
        else:
            assert response.status_code == (401 if case == "token_revoked" else 409), response.text
            async with app.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(Task.id))) == 0
                assert await db.scalar(select(func.count(AgentMessage.id))) == 0
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
