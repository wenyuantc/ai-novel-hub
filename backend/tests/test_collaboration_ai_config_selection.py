import asyncio
from contextlib import suppress

import pytest
from sqlalchemy import delete, select, text

from app.core.errors import BadRequestError, ConflictError
from app.models import AIConfig, Project
from app.schemas.ai_config import AIConfigCreate
from app.services import ai_config_service, project_ai_config
from tests.test_collaboration_ai_config import _member_change, _resolve

pytest_plugins = ["tests.test_collaboration_ai_config"]


@pytest.mark.parametrize("for_update", [False, True])
async def test_selection_must_not_combine_two_incompatible_config_snapshots(app, model_world, monkeypatch, for_update):
    w = model_world
    async with app.state.sessionmaker() as db:
        await db.execute(delete(AIConfig).where(AIConfig.id == w["owner_text"]))
        await db.commit()
    await _member_change(app, w, allow_owner_ai=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = project_ai_config._default_config

    async def read_then_wait(db, user_id, model_type):
        row = await original(db, user_id, model_type)
        if user_id == w["editor"] and model_type == "text":
            entered.set()
            await release.wait()
        return row

    monkeypatch.setattr(project_ai_config, "_default_config", read_then_wait)
    resolution = asyncio.create_task(_resolve(app, w, w["editor"], for_update=for_update))
    writers = []
    try:
        await asyncio.wait_for(entered.wait(), 5)

        async def create(user_id, name, started):
            async with app.state.sessionmaker() as db:
                started.set()
                return await ai_config_service.create_config(db, app.state.cipher, user_id, AIConfigCreate(
                    name=name, provider="openai_compat", model=name,
                ))

        starts = [asyncio.Event(), asyncio.Event()]
        writers = [asyncio.create_task(create(user_id, name, started)) for user_id, name, started in (
            (w["editor"], "actor", starts[0]), (w["owner"], "owner", starts[1]),
        )]
        await asyncio.gather(*(asyncio.wait_for(event.wait(), 5) for event in starts))
        for writer in writers:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(writer), 0.15)
        release.set()
        with pytest.raises(BadRequestError, match="尚未配置文本模型"):
            await asyncio.wait_for(resolution, 5)
        await asyncio.wait_for(asyncio.gather(*writers), 5)
        result = await _resolve(app, w, w["editor"])
        assert result.source == "actor"
    finally:
        release.set()
        for task in [resolution, *writers]:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def test_sqlite_selection_rejects_stale_snapshot_and_rolls_back(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    async with app.state.engine.connect() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.commit()
    async with app.state.sessionmaker() as stale:
        await stale.execute(text("BEGIN"))
        assert await stale.scalar(select(AIConfig.id).where(AIConfig.user_id == w["editor"])) is None
        async with app.state.sessionmaker() as writer:
            new = await ai_config_service.create_config(writer, app.state.cipher, w["editor"], AIConfigCreate(
                name="新模型", provider="openai_compat", model="personal",
            ))
        pending = Project(user_id=w["owner"], title="冲突前不得写入")
        stale.add(pending)
        with pytest.raises(ConflictError) as error:
            await project_ai_config.resolve_project_model(
                stale, w["editor"], w["project"], "text", cipher=app.state.cipher,
            )
        assert error.value.extra["code"] == "collaboration_busy"
        assert pending not in stale and pending.id is None
        assert await stale.scalar(select(AIConfig.id).where(AIConfig.user_id == w["editor"])) == new.id
