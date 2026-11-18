"""独立于章节生成日志的用量入口。"""

from sqlalchemy import select

from app.models.ai_usage import AiUsageCall
from app.services.ai_dispatch import read_visibility_stamp, visibility_sessionmaker
from app.services.retrieval_service import get_embedder, guard_embedder
from tests.helpers import create_ai_config, create_project, parse_sse, register


async def test_style_trial_records_use_without_project(client, fake_llm, app):
    author = await register(client)
    await create_ai_config(client)
    fake_llm.push("展示这个风格的试写片段。")

    response = await client.post("/api/v1/styles/trial", json={"style": {"extra": "清冷"}, "scene": "雨夜码头"})

    assert response.status_code == 200, response.text
    assert parse_sse(response.text)[-1][0] == "done"
    async with app.state.sessionmaker() as db:
        records = list(await db.scalars(select(AiUsageCall)))
    assert len(records) == 1
    record = records[0]
    assert record.kind == "style.trial" and record.status == "succeeded"
    assert record.actor_id == record.payer_id == author["id"]
    assert record.project_id is None and record.task_id is None and record.author_paid is False
    assert record.input_tokens is None and record.output_tokens is None


async def test_guarded_query_embedding_records_one_request_and_ignores_cache(client, fake_llm, app):
    author = await register(client)
    project = await create_project(client)
    await create_ai_config(client, model_type="embedding")
    async with app.state.sessionmaker() as db:
        maker = visibility_sessionmaker(db)
        stamp = await read_visibility_stamp(db, project["id"])
        embedder = await get_embedder(
            db, author["id"], cipher=app.state.cipher, factory=fake_llm.factory, ai_timeout=10,
            allow_private=False, project_id=project["id"],
        )
        embedder = guard_embedder(embedder, stamp, maker)
        await db.commit()
    assert embedder is not None

    await embedder.embed(["同一个检索词"])
    await embedder.embed(["同一个检索词"])

    assert len(fake_llm.embed_calls) == 1
    async with maker() as db:
        records = list(await db.scalars(select(AiUsageCall)))
    assert len(records) == 1
    record = records[0]
    assert record.kind == "retrieval.query" and record.model_type == "embedding"
    assert record.payer_id == author["id"] and record.project_id == project["id"] and record.author_paid
