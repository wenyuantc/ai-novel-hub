"""向导批量写入失败后，即使调用方捕获错误并提交也不能残留部分删除。"""

import json

import pytest
from sqlalchemy import select

from app.models import Career, Character, Outline, WorldSetting
from app.services import career_service, planning_lifecycle, story_service, wizard_service
from tests.helpers import create_project, register


class InjectedFailure(Exception):
    pass


@pytest.mark.parametrize("kind", ["characters", "world", "careers", "outline"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_wizard_write_failure_remains_atomic_even_if_caller_commits(app, client, monkeypatch, kind, wrapped):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    path = {"characters": "characters", "world": "world", "careers": "careers", "outline": "outlines"}[kind]
    payload = {
        "characters": {"name": "原角色"}, "world": {"title": "原世界", "content": "原文"},
        "careers": {"name": "原职业", "stages": [{"name": "起步"}]}, "outline": {"title": "原大纲"},
    }[kind]
    created = await client.post(f"{base}/{path}", json=payload)
    assert created.status_code == 201, created.text
    if kind == "characters":
        async def fail(*_args, **_kwargs):
            raise InjectedFailure()

        monkeypatch.setattr(wizard_service, "_save_character_relations", fail)
        raw = '[{"name":"新角色"}]'
    elif kind == "world":
        original = story_service.new_world

        async def fail_after_create(*args, **kwargs):
            await original(*args, **kwargs)
            raise InjectedFailure()

        monkeypatch.setattr(story_service, "new_world", fail_after_create)
        raw = '[{"title":"新世界","content":"新文"}]'
    elif kind == "careers":
        original = career_service.new_career

        async def fail_after_create(*args, **kwargs):
            await original(*args, **kwargs)
            raise InjectedFailure()

        monkeypatch.setattr(career_service, "new_career", fail_after_create)
        raw = json.dumps([{"name": "新职业", "stages": [{"name": "新阶段"}]}], ensure_ascii=False)
    else:
        async def fail(*_args, **_kwargs):
            raise InjectedFailure()

        monkeypatch.setattr(planning_lifecycle, "bump_revision", fail)
        raw = '[{"title":"新大纲"}]'

    async with app.state.sessionmaker() as db:
        if wrapped:
            from app.services.agent.tools.base import TransactionSession

            service_db = TransactionSession(db)
        else:
            service_db = db
        with pytest.raises(InjectedFailure):
            await wizard_service.save_wizard_result(service_db, project["id"], kind, raw)
        await db.commit()
    model = {"characters": Character, "world": WorldSetting, "careers": Career, "outline": Outline}[kind]
    label = "title" if kind in ("world", "outline") else "name"
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(model).where(model.project_id == project["id"])))
        assert [getattr(row, label) for row in rows] == [payload[label]]
