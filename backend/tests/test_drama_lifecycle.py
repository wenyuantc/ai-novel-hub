"""跨独立数据库的单书备份，以及回收站中的短剧生命周期。"""

from contextlib import AsyncExitStack

import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models import Base
from app.plugins.drama.transfer import TRANSFER_RULES
from app.schemas.auth import Credentials
from app.services import authorization_service
from app.services.auth_service import create_user
from tests.conftest import _new_client
from tests.helpers import create_project, make_settings, purge_trashed_project, register
from tests.test_project_transfer import _backup, _import

DRAMA = "/api/v1/plugins/drama"
TRASH = "/api/v1/admin/recycle-bin/projects"


async def _source_backup(client, admin):
    await register(client, "source_author")
    enabled = await admin.patch("/api/v1/admin/plugins/drama", json={"revision": 0, "enabled": True})
    assert enabled.is_success, enabled.text
    project = await create_project(client)
    pid = project["id"]
    novel = f"/api/v1/projects/{pid}"

    async def post(url, **fields):
        response = await client.post(url, json=fields)
        assert response.is_success, response.text
        return response.json()

    chapter = await post(novel + "/chapters", title="第一章", content="长廊尽头响起钟声。")
    person = await post(novel + "/characters", name="守钟人")
    place = await post(novel + "/world", title="钟楼", category="place")
    series = await post(f"{DRAMA}/projects/{pid}/series", title="长廊回声")
    book = f"{DRAMA}/series/{series['id']}"
    episode = await post(book + "/episodes", title="钟声", chapter_ids=[chapter["id"]])
    page = f"{DRAMA}/episodes/{episode['id']}"
    saved = await client.patch(page, json={"revision": episode["revision"], "script": "守钟人：听。"})
    assert saved.is_success, saved.text
    actor = await post(book + "/characters", name="老钟", novel_character_id=person["id"])
    look = actor["looks"][0]
    patched = await client.patch(
        f"{DRAMA}/looks/{look['id']}",
        json={
            "revision": look["revision"],
            "image_prompt": "守钟人蓝色外套",
        },
    )
    assert patched.is_success, patched.text
    scene = await post(book + "/scenes", location="旧楼", novel_world_setting_id=place["id"], image_prompt="黄昏")
    prop = await post(book + "/props", name="铜钟", image_prompt="铜绿")
    for resource, key in (("looks", look["id"]), ("scenes", scene["id"]), ("props", prop["id"])):
        current = (await client.get(page)).json()["revision"]
        attached = await client.put(f"{page}/assets/{resource}/{key}", params={"revision": current})
        assert attached.is_success, attached.text
    for name, prompt in (("远景", "扫过长廊"), ("特写", "推近指针")):
        await post(
            page + "/shots",
            title=name,
            video_prompt=prompt,
            look_ids=[look["id"]],
            scene_id=scene["id"],
            prop_ids=[prop["id"]],
        )
    backup = await _backup(client, pid)
    assert all(backup["tables"][rule.table] for rule in TRANSFER_RULES)
    return backup


@pytest.fixture
async def destination(tmp_path):
    directory = tmp_path / "destination"
    directory.mkdir()
    target = create_app(make_settings(directory), start_task_engine=False)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(target.state.engine.dispose)
        stack.push_async_callback(target.state.system_settings.close)
        async with target.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await authorization_service.initialize(target.state.sessionmaker)
        await target.state.system_settings.load()
        await target.state.task_engine.request_worker.start()
        stack.push_async_callback(target.state.task_engine.request_worker.stop)
        owner = await stack.enter_async_context(_new_client(target))
        await register(owner, "destination_author")
        admin = await stack.enter_async_context(_new_client(target))

        async with target.state.sessionmaker() as db:
            await create_user(db, Credentials(username="destination_admin", password="secret123"), is_admin=True)
        login = await admin.post("/api/v1/auth/login", json={"username": "destination_admin", "password": "secret123"})
        assert login.is_success, login.text

        yield target, owner, admin


async def test_cross_instance_import_restore_and_purge(app, client, system_admin, destination):
    target, owner, admin = destination
    assert app.state.engine.url.database != target.state.engine.url.database
    backup = await _source_backup(client, system_admin)
    # 目标库先保留一份同内容作品，让新书每个节点的旧 ID 均已被占用。
    first = (await _import(owner, backup, "占用旧编号的作品"))["project"]["id"]
    copied = (await _import(owner, backup, "恢复的作品"))["project"]["id"]
    removed = await owner.delete(f"/api/v1/projects/{first}")
    assert removed.is_success, removed.text
    await purge_trashed_project(admin, first)
    enabled = await admin.patch("/api/v1/admin/plugins/drama", json={"revision": 0, "enabled": True})
    assert enabled.is_success, enabled.text
    restored = (await _backup(owner, copied))["tables"]
    for table in ("chapters", "characters", "world_settings", *(r.table for r in TRANSFER_RULES if r.has_id)):
        assert {r["id"] for r in restored[table]}.isdisjoint(r["id"] for r in backup["tables"][table])

    (series,) = (await owner.get(f"{DRAMA}/projects/{copied}/series")).json()
    (episode,) = (await owner.get(f"{DRAMA}/series/{series['id']}/episodes")).json()
    page = f"{DRAMA}/episodes/{episode['id']}"
    body = (await owner.get(page)).json()
    assert (body["source_text"], body["script"]) == ("长廊尽头响起钟声。", "守钟人：听。")
    assert body["chapter_ids"] == [restored["chapters"][0]["id"]]
    assets = (await owner.get(page + "/assets")).json()
    actor, scene, prop = (assets[k][0] for k in ("characters", "scenes", "props"))
    look = actor["looks"][0]
    assert (actor["novel_character_id"], actor["novel_character_name"]) == (
        restored["characters"][0]["id"],
        "守钟人",
    )
    assert (scene["novel_world_setting_id"], scene["novel_world_setting_title"]) == (
        restored["world_settings"][0]["id"],
        "钟楼",
    )
    assert (look["image_prompt"], scene["image_prompt"], prop["image_prompt"]) == ("守钟人蓝色外套", "黄昏", "铜绿")
    shots = (await owner.get(page + "/shots")).json()
    assert [(s["title"], s["video_prompt"]) for s in shots] == [("远景", "扫过长廊"), ("特写", "推近指针")]
    for shot in shots:
        assert (shot["look_ids"], shot["scene_id"], shot["prop_ids"]) == ([look["id"]], scene["id"], [prop["id"]])

    project_url = f"/api/v1/projects/{copied}"
    assert (await owner.delete(project_url)).status_code == 204
    assert (await owner.get(page)).status_code == 404
    listing = (await admin.get(TRASH)).json()["items"]
    record = next(row for row in listing if row["id"] == copied)
    recovered = await admin.post(
        f"{TRASH}/{copied}/restore", json={"deleted_at": record["deleted_at"], "confirm": True}
    )
    assert recovered.status_code == 204, recovered.text
    assert (await owner.get(page + "/shots")).json() == shots
    assert (await owner.get(page + "/assets")).json() == assets
    assert (await owner.delete(project_url)).status_code == 204
    await purge_trashed_project(admin, copied)
    assert (await owner.get(page)).status_code == 404
    async with target.state.sessionmaker() as db:
        for rule in TRANSFER_RULES:
            assert await db.scalar(select(func.count()).select_from(rule.model)) == 0, rule.table
