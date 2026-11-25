import asyncio

import pytest
from sqlalchemy import select

from app.models import Project, WritingDailyStat
from app.services.writing_stats import record_words
from tests.helpers import create_project, register
from tests.schema_helpers import isolated_worker_database


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def test_writing_stats_concurrent_increments_and_rollback(client, app):
    await register(client)
    pid = (await create_project(client))["id"]
    sessions = app.state.sessionmaker
    async with sessions() as db:
        uid = await db.scalar(select(Project.user_id).where(Project.id == pid))

    async def add(source, words):
        async with sessions.begin() as db:
            await record_words(db, user_id=uid, project_id=pid, source=source, words=words)

    await asyncio.gather(add("manual", 40), add("manual", 60), add("ai", 20))
    async with sessions() as db:
        await record_words(db, user_id=uid, project_id=pid, source="manual", words=80)
        await db.rollback()
    async with sessions() as db:
        assert (await db.execute(select(WritingDailyStat.manual_words, WritingDailyStat.ai_words))).one() == (100, 20)


async def test_writing_stats_parallel_chapter_saves_and_stale_version(client, app):
    await register(client)
    pid = (await create_project(client))["id"]
    endpoint = f"/api/v1/projects/{pid}/chapters"
    urls = []
    for title in ("甲", "乙"):
        result = await client.post(endpoint, json={"title": title})
        assert result.status_code == 201, result.text
        urls.append(f"{endpoint}/{result.json()['id']}")

    async def save(url, content, revision):
        return await client.patch(url, json={"content": content, "expected_content_revision": revision})

    first = await asyncio.gather(save(urls[0], "甲" * 40, 0), save(urls[1], "乙" * 60, 0))
    assert [r.status_code for r in first] == [200, 200], [r.text for r in first]
    async with app.state.sessionmaker() as db:
        assert (await db.execute(select(WritingDailyStat.manual_words))).scalar_one() == 100
    same_version = await asyncio.gather(save(urls[0], "丙" * 20, 1), save(urls[0], "丁" * 20, 1))
    assert sorted(r.status_code for r in same_version) == [200, 409], [r.text for r in same_version]
    async with app.state.sessionmaker() as db:
        assert (await db.execute(select(WritingDailyStat.manual_words))).scalar_one() == 120
