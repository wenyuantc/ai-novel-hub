"""人工记忆派生的作者任务必须携带原认证令牌，仍能正常领取执行。"""

from sqlalchemy import select

from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import RetrievalChunk, Task
from tests.helpers import create_ai_config, create_project, register, wait_task


async def test_owner_manual_memory_index_uses_original_token_and_completes(app, client, fake_llm):
    user = await register(client, "manual-index-owner")
    project = await create_project(client)
    await create_ai_config(client, model_type="embedding", model="owner-vector")
    response = await client.post(f"/api/v1/projects/{project['id']}/memories", json={
        "chapter_no": 1, "content": "人工索引正文",
    })
    assert response.status_code == 201, response.text
    decoded = decode_access_token(client.cookies.get(ACCESS_TOKEN_COOKIE), app.state.settings.jwt_secret)
    assert decoded and decoded[0] == user["id"]
    async with app.state.sessionmaker() as db:
        task = await db.scalar(select(Task).where(Task.kind == "kb.index", Task.project_id == project["id"]))
        assert task is not None and task.user_id == user["id"]
        assert task.input["_task_actor_token_version"] == decoded[1]
        identity = (task.id, task.statistics_key)
    finished = await wait_task(client, identity)
    assert finished["status"] == "succeeded", finished
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == response.json()["id"],
        ))
        assert row is not None and row.embedding is not None and row.embedding_key.startswith("openai_compat:")
    assert len(fake_llm.embed_calls) == 1
