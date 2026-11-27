"""短剧迁移、作品备份及所有引用的往返。"""

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import closing
from copy import deepcopy

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, UniqueConstraint, delete, func, inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.errors import BadRequestError, ConflictError
from app.models import Project, User
from app.plugins.drama import models as m
from app.plugins.drama.transfer import TRANSFER_RULES
from app.schemas.transfer import BackupOptions
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from app.services.project_transfer import exporter
from app.services.project_transfer.importer import parse_backup
from tests.helpers import create_project, register
from tests.test_annotation_native_backup import _postgres_pair, _restore
from tests.test_project_transfer import _backup, _import
from tests.test_scenes import setup_scene
from tests.test_serialization_concurrency import settings_overrides as settings_overrides
from tests.test_setting_fields_migration import _config
from tests.test_task_worker_migration import migration_db as migration_db


async def _seed_native(connection):
    await connection.execute(User.__table__.insert().values(id=1, username="drama-author", password_hash="synthetic"))
    await connection.execute(Project.__table__.insert().values(id=1, user_id=1, title="原作"))
    await connection.execute(m.DramaSeries.__table__.insert().values(id=1, project_id=1, title="改编"))
    await connection.execute(
        m.DramaEpisode.__table__.insert().values(id=1, series_id=1, title="第一集", number=1, script="短剧快照")
    )
    await connection.execute(
        m.DramaShot.__table__.insert().values(id=1, episode_id=1, position=1024, title="开场镜头", video_prompt="街景")
    )


async def test_drama_migration_roundtrip(migration_db, tmp_path):
    config, engine = migration_db
    await asyncio.to_thread(command.upgrade, config, "0083")

    def check_schema(connection):
        inspector = inspect(connection)
        for rule in TRANSFER_RULES:
            model = rule.model.__table__
            name = rule.table
            assert {
                c["name"]: (c["type"].compile(connection.dialect), c["nullable"]) for c in inspector.get_columns(name)
            } == {c.name: (c.type.compile(connection.dialect), c.nullable) for c in model.columns}
            assert inspector.get_pk_constraint(name)["constrained_columns"] == list(model.primary_key.columns.keys())
            assert {x["name"] for x in inspector.get_unique_constraints(name)} == {
                c.name for c in model.constraints if isinstance(c, UniqueConstraint)
            }
            assert {x["name"] for x in inspector.get_check_constraints(name)} == {
                c.name for c in model.constraints if isinstance(c, CheckConstraint)
            }
            assert {x["name"] for x in inspector.get_indexes(name) if not x.get("duplicates_constraint")} == {
                i.name for i in model.indexes
            }
            assert {f["name"]: f["options"].get("ondelete") for f in inspector.get_foreign_keys(name)} == {
                f.name: f.ondelete for f in model.foreign_key_constraints
            }

    async with engine.begin() as connection:
        await connection.run_sync(check_schema)
        await _seed_native(connection)
    with pytest.raises(RuntimeError, match="仍有短剧"):
        await asyncio.to_thread(command.downgrade, config, "0082")
    if engine.dialect.name == "sqlite":
        snapshot = tmp_path / "short-drama.snapshot"
        await asyncio.to_thread(
            snapshot_database, str(engine.url), snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, tmp_path / "restored.db") == "0083"
        with closing(sqlite3.connect(tmp_path / "restored.db")) as copy:
            assert copy.execute("SELECT title, video_prompt FROM drama_shots").fetchall() == [("开场镜头", "街景")]
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.execute("DROP TABLE drama_shot_props")
        with pytest.raises(ValueError, match="短剧数据表"):
            await asyncio.to_thread(restore_sqlite_snapshot, snapshot, tmp_path / "incomplete.db")
    async with engine.begin() as connection:
        await connection.execute(delete(m.DramaSeries))
    await asyncio.to_thread(command.downgrade, config, "0082")
    async with engine.connect() as connection:
        names = await connection.run_sync(lambda c: inspect(c).get_table_names())
        assert all(not n.startswith("drama_") for n in names)


async def test_drama_transfer_remaps_all_links_and_cascades(app, client):
    project, chapter, _, _ = await setup_scene(client, "改编正文")
    pid = project["id"]
    novel = (await client.post(f"/api/v1/projects/{pid}/characters", json={"name": "原作人物"})).json()["id"]
    place = (
        await client.post(f"/api/v1/projects/{pid}/world", json={"title": "原作地点", "category": "place"})
    ).json()["id"]
    async with app.state.sessionmaker.begin() as db:

        async def add(model, **fields):
            obj = model(**fields)
            db.add(obj)
            await db.flush()
            return obj.id

        series = await add(m.DramaSeries, project_id=pid, title="剧")
        episode = await add(
            m.DramaEpisode, series_id=series, title="集", number=1, source_text="改编正文", script="剧本"
        )
        await add(m.DramaEpisodeSource, episode_id=episode, chapter_id=chapter["id"], chapter_title="来源", sort=0)
        character = await add(m.DramaCharacter, series_id=series, name="主角", novel_character_id=novel)
        look = await add(m.DramaLook, character_id=character, name="便装", is_default=True, image_prompt="肖像")
        scene = await add(m.DramaScene, series_id=series, location="教室", novel_world_setting_id=place)
        prop = await add(m.DramaProp, series_id=series, name="桌子")
        shot = await add(
            m.DramaShot, episode_id=episode, title="开场", position=10, scene_id=scene, video_prompt="推镜"
        )
        db.add_all(
            [
                m.DramaEpisodeLook(episode_id=episode, look_id=look),
                m.DramaEpisodeProp(episode_id=episode, prop_id=prop),
                m.DramaEpisodeScene(episode_id=episode, scene_id=scene),
                m.DramaShotProp(shot_id=shot, prop_id=prop),
                m.DramaShotLook(shot_id=shot, look_id=look),
            ]
        )

    # 关联后重新整理原作设定分类是合法的作品操作，迁移要保留这条历史关联。
    changed = await client.patch(f"/api/v1/projects/{pid}/world/{place}", json={"category": "other"})
    assert changed.is_success, changed.text
    original = await _backup(client, pid)
    sources = deepcopy(original)
    row = sources["tables"]["drama_episode_sources"][0]
    sources["tables"]["drama_episode_sources"].append({**row, "id": row["id"] + 1, "sort": row["sort"] + 1})
    with pytest.raises(BadRequestError, match="唯一字段重复"):
        parse_backup(json.dumps(sources).encode())
    for row in sources["tables"]["drama_episode_sources"]:
        row["chapter_id"] = None
    assert len(parse_backup(json.dumps(sources).encode()).tables["drama_episode_sources"]) == 2
    # 文件内所有外键都存在，但把另一部剧的道具绑定到本剧分镜仍应在预览时报错。
    invalid = deepcopy(original)
    tables = invalid["tables"]
    second = {**tables["drama_series"][0], "id": series + 1}
    tables["drama_series"].append(second)
    tables["drama_props"][0]["series_id"] = second["id"]
    with pytest.raises(BadRequestError, match="跨剧引用"):
        parse_backup(json.dumps(invalid).encode())
    invalid = deepcopy(original)
    invalid["tables"]["drama_looks"][0]["is_default"] = False
    with pytest.raises(BadRequestError, match="默认造型"):
        parse_backup(json.dumps(invalid).encode())
    invalid = deepcopy(original)
    invalid["version"] = 18
    with pytest.raises(BadRequestError, match="旧版"):
        parse_backup(json.dumps(invalid).encode())

    copied = (await _import(client, original))["project"]["id"]
    restored = (await _backup(client, copied))["tables"]
    id_maps = {}
    for rule in TRANSFER_RULES:
        (old,) = original["tables"][rule.table]
        (new,) = restored[rule.table]
        if rule.has_id:
            assert new["id"] != old["id"]
            id_maps[rule.table] = {old["id"]: new["id"]}
        for key, value in old.items():
            if key == "id":
                continue
            ref = rule.fk.get(key)
            if ref in ("characters", "world_settings", "chapters"):
                assert new[key] != value and new[key] in {r["id"] for r in restored[ref]}
            elif ref:
                assert new[key] == id_maps[ref][value]
            else:
                assert new[key] == value

    older = deepcopy(original)
    older["version"] = 18
    for rule in TRANSFER_RULES:
        del older["tables"][rule.table]
    other_pid = (await _import(client, older))["project"]["id"]
    empty = (await _backup(client, other_pid))["tables"]
    assert all(empty[rule.table] == [] for rule in TRANSFER_RULES)

    # 即使插件未启用，删除正文来源仍保留剧集的章节名和原文快照。
    removed = await client.delete(f"/api/v1/projects/{pid}/chapters/{chapter['id']}")
    assert removed.is_success, removed.text
    async with app.state.sessionmaker() as db:
        source = (
            await db.scalars(select(m.DramaEpisodeSource).where(m.DramaEpisodeSource.episode_id == episode))
        ).one()
        assert source.chapter_id is None and source.chapter_title == "来源"
        assert (await db.get(m.DramaEpisode, episode)).source_text == "改编正文"

    async with app.state.sessionmaker.begin() as db:
        await db.execute(delete(Project).where(Project.id.in_([pid, copied])))
        for rule in TRANSFER_RULES:
            assert await db.scalar(select(func.count()).select_from(rule.model)) == 0


async def test_drama_transfer_detects_concurrent_edit(app, client):
    await register(client)
    pid = (await create_project(client))["id"]
    async with app.state.sessionmaker.begin() as db:
        series = m.DramaSeries(project_id=pid, title="短剧")
        db.add(series)
        await db.flush()
        sid = series.id
    async with app.state.sessionmaker() as db:
        plan = await exporter.prepare_export(db, (await db.get(Project, pid)).user_id, pid, BackupOptions())
    async with app.state.sessionmaker.begin() as db:
        db.add(m.DramaEpisode(series_id=sid, number=1, title="新增一集"))
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError, match="插件资料已变化"):
            await exporter.validate_export(db, plan)


async def test_drama_postgres_native_snapshot_restores_script_and_shot(tmp_path):
    async with _postgres_pair() as (source, target):
        config = _config(tmp_path / "unused.db")
        config.set_main_option("sqlalchemy.url", source.replace("%", "%%"))
        await asyncio.to_thread(command.upgrade, config, "0083")
        engine = create_async_engine(source)
        try:
            async with engine.begin() as connection:
                await _seed_native(connection)
        finally:
            await engine.dispose()
        snapshot = tmp_path / "short-drama.dump"
        await asyncio.to_thread(
            snapshot_database, source, snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        await _restore(snapshot, target)
        restored = create_async_engine(target)
        try:
            async with restored.connect() as conn:
                assert (
                    await conn.execute(
                        select(m.DramaEpisode.script, m.DramaShot.video_prompt).join_from(m.DramaEpisode, m.DramaShot)
                    )
                ).all() == [("短剧快照", "街景")]
        finally:
            await restored.dispose()
