"""真实HTTP扫描500万码点正文，验证全量计数与缓存边界并记录冷热时长。"""

import json
import time

from app.models import Chapter, Character, KbDocument, WorldSetting
from app.services.mentions.matcher import cache
from tests.helpers import create_project, register


async def test_five_million_character_corpus_is_complete(client, app):
    await register(client)
    project = await create_project(client)
    text = ("林远经过青云城。" + "静" * 10000)[:10000]
    async with app.state.sessionmaker() as db:
        db.add(Character(project_id=project["id"], name="林远"))
        db.add(WorldSetting(project_id=project["id"], title="青云城", category="place"))
        db.add_all(Character(project_id=project["id"], name=f"配角{i:04d}") for i in range(200))
        db.add_all(
            Chapter(
                project_id=project["id"], chapter_no=i + 1, title=f"章节{i + 1}", content=text, word_count=len(text)
            )
            for i in range(500)
        )
        db.add_all(
            KbDocument(project_id=project["id"], filename=f"资料{i}.txt", text=text * 5, sha256=f"{i:064d}")
            for i in range(10)
        )
        await db.commit()
    cache.clear()
    times = []
    for _ in range(2):
        started = time.perf_counter()
        response = await client.get(f"/api/v1/projects/{project['id']}/mentions", params={"page_size": 1})
        times.append(round(time.perf_counter() - started, 4))
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1100
        assert response.json()["source_counts"] == {"chapter": 1000, "chat": 0, "note": 0, "knowledge": 100}
        assert len(response.json()["items"]) == 1
    assert cache.info()["sources"] <= cache.max_sources
    assert cache.info()["spans"] <= cache.max_spans
    print(
        "MENTION_SCALE "
        + json.dumps(
            {
                "chapter_chars": 5_000_000,
                "knowledge_chars": 500_000,
                "sources": 510,
                "entities": 202,
                "cold_seconds": times[0],
                "warm_seconds": times[1],
                "cache": cache.info(),
            }
        )
    )
