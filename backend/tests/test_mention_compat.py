import json
from copy import deepcopy
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.core.errors import BadRequestError, ConflictError
from app.models import WorldSetting
from app.services.agent import effects
from app.services.project_transfer import importer
from tests.helpers import create_project, register
from tests.test_project_transfer import _backup, _import


async def test_world_aliases_new_and_old_backup_round_trip(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await client.post(f"{base}/world", json={"title": "青云城", "category": "place", "aliases": ["古城"]})
    await client.post(f"{base}/notes", json={"title": "笔记", "content": "古城传说"})
    backup = await _backup(client, project["id"])
    assert backup["app_schema"] == "0038"
    imported = await _import(client, backup)
    imported_base = f"/api/v1/projects/{imported['project']['id']}"
    assert (await client.get(f"{imported_base}/world")).json()[0]["aliases"] == ["古城"]
    assert (await client.get(f"{imported_base}/mentions", params={"source_type": "note"})).json()["total"] == 1
    legacy = deepcopy(backup)
    legacy["tables"].pop("project_notes")
    legacy["tables"]["world_settings"][0].pop("aliases")
    legacy["app_schema"] = "0031"
    old_import = await _import(client, legacy)
    old_base = f"/api/v1/projects/{old_import['project']['id']}"
    assert (await client.get(f"{old_base}/world")).json()[0]["aliases"] == []
    assert (await client.get(f"{old_base}/notes")).json()["total"] == 0


async def test_new_backup_values_are_validated_before_import(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await client.post(f"{base}/world", json={"title": "青城", "category": "place"})
    await client.post(f"{base}/notes", json={"title": "笔记", "content": "正文"})
    backup = await _backup(client, project["id"])
    for table, column, value in [
        ("world_settings", "aliases", ["a" * 51]),
        ("world_settings", "aliases", [str(i) for i in range(11)]),
        ("world_settings", "aliases", "古城"),
        ("project_notes", "revision", 0),
        ("project_notes", "title", "  "),
        ("project_notes", "content", "甲" * 200001),
    ]:
        invalid = deepcopy(backup)
        invalid["tables"][table][0][column] = value
        with pytest.raises(BadRequestError):
            importer.parse_backup(json.dumps(invalid).encode())


async def test_world_aliases_effect_restore_and_older_v2_snapshots(client, app):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    world_id = (
        await client.post(f"{base}/world", json={"title": "青城", "category": "place", "aliases": ["古城"]})
    ).json()["id"]
    async with app.state.sessionmaker() as db:
        world = await db.get(WorldSetting, world_id)
        before = await effects.snapshot(db, project["id"])
        world.aliases = ["新城"]
        delta = effects.diff(before, await effects.snapshot(db, project["id"]))
        await db.commit()
        await effects.undo(db, project["id"], delta)
        await db.commit()
        assert world.aliases == ["古城"]
        world.aliases = []
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        world.title = "新青城"
        delta = effects.diff(before, await effects.snapshot(db, project["id"]))
        for effect in delta:
            if effect["table"] == "world_settings":
                effect["format_version"] = 2
                for side in ("before", "after"):
                    effect[side].pop("aliases")
                    effect[side].pop("setting_tags")
                    for key in effects._legacy_visibility_defaults("world_settings"):
                        effect[side].pop(key)
        await db.commit()
        # 0033前的v2快照没有aliases；仅在当前仍为默认空别名时可安全回退。
        await effects.undo(db, project["id"], delta)
        await db.commit()
        assert world.title == "青城" and world.aliases == []
        world.title = "新青城"
        world.aliases = ["作者新设别名"]
        await db.commit()
        with pytest.raises(ConflictError):
            await effects.undo(db, project["id"], delta)


def test_0033_aliases_backfill_and_downgrade_preserves_children(tmp_path):
    path = tmp_path / "aliases.db"
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "0032")
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as db:
            db.execute(text("PRAGMA foreign_keys=ON"))
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'writer','hash')"))
            db.execute(
                text(
                    "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                    "VALUES(1,1,'书','','','','planning')"
                )
            )
            db.execute(text("INSERT INTO world_settings(id,project_id,title,content,sort) VALUES(1,1,'城','旧文',1)"))
            db.execute(
                text(
                    "INSERT INTO setting_timelines(project_id,subject_kind,world_setting_id,baseline) "
                    "VALUES(1,'world',1,:baseline)"
                ),
                {"baseline": '{"content":"旧文"}'},
            )
        command.upgrade(config, "0033")
        with engine.connect() as db:
            assert json.loads(db.scalar(text("SELECT aliases FROM world_settings WHERE id=1"))) == []
            assert db.scalar(text("SELECT count(*) FROM setting_timelines")) == 1
            assert not db.execute(text("PRAGMA foreign_key_check")).all()
        assert "aliases" in {col["name"] for col in inspect(engine).get_columns("world_settings")}
        command.downgrade(config, "0032")
        with engine.connect() as db:
            assert db.scalar(text("SELECT content FROM world_settings WHERE id=1")) == "旧文"
            assert db.scalar(text("SELECT count(*) FROM setting_timelines")) == 1
            assert not db.execute(text("PRAGMA foreign_key_check")).all()
    finally:
        engine.dispose()
