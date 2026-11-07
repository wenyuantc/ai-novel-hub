"""同一确定性 FakeLLM 场景提交/取消/过期竞争在真实隔离 PG 上验证。"""

import httpx
import pytest

from tests.test_scene_generation import (
    test_whole_chapter_failure_or_stale_context_never_commits_partial_scenes as _race,
)
from tests.test_scene_generation import (
    test_whole_chapter_stages_latest_preceding_proposals_without_old_or_future_scene as _whole,
)
from tests.test_scenes_postgresql import postgres_scenes_app  # noqa: F401


@pytest.fixture
async def pg_ai_client(postgres_scenes_app):  # noqa: F811
    app = postgres_scenes_app
    await app.state.task_engine.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        await app.state.task_engine.stop()


@pytest.mark.parametrize("failure", ["error", "cancel", "planning", "timeline"])
async def test_pg_scene_ai_race_never_commits_partial_chapter(pg_ai_client, fake_llm, failure):
    await _race(pg_ai_client, fake_llm, failure)


async def test_pg_scene_ai_all_proposals_commit_together(pg_ai_client, fake_llm):
    await _whole(pg_ai_client, fake_llm)
