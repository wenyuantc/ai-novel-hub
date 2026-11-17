"""在 PostgreSQL 真实复合外键和任务事务下验证向导缩略图覆盖。"""

import json

import httpx
import pytest
from sqlalchemy import select

from app.models.setting_images import SettingImageResource, SettingThumbnailBinding
from tests.helpers import create_ai_config, create_project, register, run_task
from tests.test_series_postgresql import postgres_series_app as _postgres_series_fixture
from tests.test_wizard_thumbnail_replacement import (
    KINDS,
    _assert_wizard_thumbnail_rollback,
    _binding,
    _entity,
    _upload,
)

postgres_series_app = _postgres_series_fixture


@pytest.mark.parametrize("kind", KINDS)
async def test_postgres_wizard_thumbnail_cascade_retains_resource(postgres_series_app, fake_llm, kind):
    app = postgres_series_app
    engine = app.state.task_engine
    await engine.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await register(client)
            await create_ai_config(client)
            project = await create_project(client)
            other = await create_project(client, title="另一作品")
            old = await _entity(client, project["id"], kind)
            foreign = await _entity(client, other["id"], kind)
            await _upload(client, project["id"], kind, old)
            await _upload(client, other["id"], kind, foreign)
            resource = (await _binding(app, project["id"])).image_resource_id
            other_binding = await _binding(app, other["id"])
            fake_llm.push(json.dumps(KINDS[kind][4], ensure_ascii=False))
            task, _ = await run_task(client, await client.post(f"/api/v1/projects/{project['id']}/wizard/{kind}"))
            assert task["status"] == "succeeded", task
            rows = (await client.get(f"/api/v1/projects/{project['id']}/{KINDS[kind][0]}")).json()
            assert len(rows) == 1 and rows[0]["thumbnail"] is None
            async with app.state.sessionmaker() as db:
                assert await db.get(SettingImageResource, resource) is not None
                assert await db.scalar(select(SettingThumbnailBinding).where(
                    SettingThumbnailBinding.image_resource_id == resource,
                )) is None
                assert await db.get(SettingThumbnailBinding, other_binding.id) is not None
            route = f"/api/v1/projects/{other['id']}/settings/{KINDS[kind][1]}/{foreign}/thumbnail"
            assert (await client.get(route)).status_code == 200
    finally:
        await engine.stop()


async def test_postgres_failed_wizard_replacement_rolls_back_thumbnail(postgres_series_app, fake_llm, monkeypatch):
    app = postgres_series_app
    await app.state.task_engine.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            await _assert_wizard_thumbnail_rollback(app, client, fake_llm, monkeypatch)
    finally:
        await app.state.task_engine.stop()
