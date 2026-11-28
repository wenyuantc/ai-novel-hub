"""每人每集的模型选择、权限、绑定失效和备份边界。"""

import asyncio
import sqlite3
import threading
import time
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy import delete, inspect, select

from app.models import AIConfig, User
from app.plugins.drama.models import DramaEpisode, DramaEpisodeModelPreference
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from tests.helpers import register
from tests.test_drama_assets import BASE
from tests.test_drama_assets import library as library
from tests.test_drama_transfer import _seed_native
from tests.test_project_transfer import _backup, _import
from tests.test_serialization_concurrency import settings_overrides as settings_overrides
from tests.test_task_worker_migration import migration_db as migration_db

EMPTY = {"text": None, "image": None, "video": None}


async def config(db, uid, kind):
    row = AIConfig(user_id=uid, name=kind, provider="openai_compat", model=kind, model_type=kind, api_key_enc="unused")
    db.add(row)
    await db.flush()
    return row.id


async def share(owner, guest, pid, username, role):
    user = await register(guest, username)
    invite = (
        await owner.post(f"/api/v1/projects/{pid}/collaborators", json={"username": username, "role": role})
    ).json()
    result = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
        json={"expected_revision": invite["revision"]},
    )
    assert result.is_success, result.text
    return user["id"]


async def test_episode_models_private_per_user_and_episode(app, client, make_client, library):
    sid, eid, pid = library
    path = f"{BASE}/episodes/{eid}/models"
    initial = await client.get(path)
    assert initial.status_code == 200 and initial.json() == EMPTY
    assert "no-store" in initial.headers["cache-control"]
    editor, viewer, stranger = make_client(), make_client(), make_client()
    uid = await share(client, editor, pid, "model-editor", "editor")
    await share(client, viewer, pid, "model-viewer", "viewer")
    await register(stranger, "model-stranger")
    async with app.state.sessionmaker.begin() as db:
        author = await db.scalar(select(User.id).where(User.username == "alice"))
        text_id = await config(db, author, "text")
        image_id = await config(db, author, "image")
        video_id = await config(db, author, "video")
        editor_id = await config(db, uid, "text")
    saved = {"text": text_id, "image": image_id, "video": video_id}
    assert (await client.patch(path, json=saved)).json() == saved
    device = make_client()
    assert (await device.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})).is_success
    assert (await device.get(path)).json() == saved
    # 两台设备从同一个空选择开始并发修改两个用途，两项都应保留。
    assert (await client.patch(path, json=EMPTY)).json() == EMPTY
    first, second_device = await asyncio.gather(
        device.patch(path, json={"text": text_id}),
        client.patch(path, json={"image": image_id}),
    )
    assert first.is_success and second_device.is_success, (first.text, second_device.text)
    assert (await client.patch(path, json={"video": video_id})).json() == saved
    second = (await client.post(f"{BASE}/series/{sid}/episodes", json={"title": "第二集"})).json()
    assert (await client.get(f"{BASE}/episodes/{second['id']}/models")).json() == EMPTY
    assert (await editor.get(path)).json() == EMPTY
    assert (await editor.patch(path, json={"text": editor_id})).json() == {**EMPTY, "text": editor_id}
    assert (await client.get(path)).json() == saved
    assert (await viewer.get(path)).json() == EMPTY
    assert (await viewer.patch(path, json={"text": None})).status_code in (403, 404)
    assert (await stranger.get(path)).status_code == 404
    assert (await stranger.patch(path, json={"text": None})).status_code == 404
    assert (await client.patch(path, json={"image": None, "text": editor_id})).status_code == 404
    assert (await client.patch(path, json={"text": video_id})).status_code == 400
    assert (await client.patch(path, json={"image": True})).status_code == 422
    assert (await client.get(path)).json() == saved
    mismatch = {"X-Editor-Actor-Id": str(uid)}
    assert (await client.get(path, headers=mismatch)).status_code == 409
    assert (await client.patch(path, json={"text": None}, headers=mismatch)).status_code == 409
    assert (await client.patch(path, json={"image": None})).json() == {**saved, "image": None}
    # 用户可更改配置类型；已不匹配的选择恢复为空。
    async with app.state.sessionmaker.begin() as db:
        (await db.get(AIConfig, video_id)).model_type = "image"
    assert (await client.get(path)).json() == {**EMPTY, "text": text_id}
    assert (await client.delete(f"/api/v1/ai-configs/{text_id}")).is_success
    assert (await client.get(path)).json() == EMPTY
    backup = await _backup(client, pid)
    assert "drama_episode_model_preferences" not in backup["tables"]
    copied = (await _import(client, backup))["project"]["id"]
    sid2 = (await client.get(f"{BASE}/projects/{copied}/series")).json()[0]["id"]
    eid2 = (await client.get(f"{BASE}/series/{sid2}/episodes")).json()[0]["id"]
    assert (await client.get(f"{BASE}/episodes/{eid2}/models")).json() == EMPTY


async def test_preference_migration_and_lifecycle(migration_db, tmp_path):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0085")
    table = DramaEpisodeModelPreference.__table__
    async with engine.begin() as db:
        if engine.dialect.name == "sqlite":
            await db.exec_driver_sql("PRAGMA foreign_keys=ON")
        await _seed_native(db)

        def shape(c):
            view = inspect(c)
            assert view.get_pk_constraint(table.name)["constrained_columns"] == ["episode_id", "user_id"]
            assert {k["name"]: k["options"].get("ondelete") for k in view.get_foreign_keys(table.name)} == {
                k.name: k.ondelete for k in table.foreign_key_constraints
            }

        await db.run_sync(shape)
        await db.execute(User.__table__.insert().values(id=2, username="editor", password_hash="synthetic"))
        await db.execute(
            AIConfig.__table__.insert().values(
                id=1, user_id=1, name="text", provider="openai_compat", model="test", api_key_enc="unused"
            )
        )
        await db.execute(table.insert().values(episode_id=1, user_id=1, text_config_id=1))
        await db.execute(table.insert().values(episode_id=1, user_id=2))
        await db.execute(delete(User).where(User.id == 2))
        assert (await db.scalars(select(table.c.user_id))).all() == [1]
    if engine.dialect.name == "sqlite":
        snapshot, restored = tmp_path / "snapshot", tmp_path / "restored.db"
        await asyncio.to_thread(
            snapshot_database, str(engine.url), snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, restored) == "0085"
        with closing(sqlite3.connect(restored)) as conn:
            assert conn.execute(
                "SELECT episode_id,user_id,text_config_id FROM drama_episode_model_preferences"
            ).fetchall() == [(1, 1, 1)]
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.execute("DROP TABLE drama_episode_model_preferences")
        with pytest.raises(ValueError, match="模型偏好"):
            await asyncio.to_thread(restore_sqlite_snapshot, snapshot, tmp_path / "broken.db")
    with pytest.raises(RuntimeError, match="模型偏好"):
        await asyncio.to_thread(command.downgrade, cfg, "0084")
    async with engine.begin() as db:
        if engine.dialect.name == "sqlite":
            await db.exec_driver_sql("PRAGMA foreign_keys=ON")
        await db.execute(delete(AIConfig).where(AIConfig.id == 1))
        assert (await db.execute(select(table.c.text_config_id))).one() == (None,)

        await db.execute(delete(DramaEpisode).where(DramaEpisode.id == 1))
        assert not (await db.scalars(select(table.c.user_id))).all()
    await asyncio.to_thread(command.downgrade, cfg, "0084")
