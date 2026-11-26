"""两种顺序、日期继承、历法修改影响、作品授权与双库迁移。"""

import asyncio

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, inspect, select, text

from app.models import AdminAuditLog, Chapter, ChapterStoryDate, Project, Scene, SceneStoryDate, StoryCalendar, User
from tests.helpers import create_project
from tests.test_collaboration_content_access import _content_setup
from tests.test_scenes import setup_scene
from tests.test_task_worker_migration import migration_db as migration_db

MONTHS = [{"id": "mist", "name": "雾月", "days": 20}, {"id": "snow", "name": "雪月", "days": 15}]
CALENDAR = {"expected_revision": 0, "era_name": "星历", "months": MONTHS}


def date(day, month="mist"):
    return {"year": 3, "month_id": month, "day": day}


async def test_dates_edit_permissions_and_retired_corrections(app, client, make_client, system_admin):
    _, _, _, project, chapter, scene, base, chapter_url, viewer, editor, stranger = await _content_setup(
        client, make_client,
    )
    timeline = f"{base}/story-timeline"
    calendar = f"{timeline}/calendar"
    chapter_date = f"{timeline}/chapter/{chapter['id']}/date"
    scene_date = f"{timeline}/scene/{scene['id']}/date"
    original = (await client.get(chapter_url)).json()
    assert (await viewer.get(calendar)).json() == {"era_name": None, "revision": 0, "months": []}
    assert (await stranger.get(timeline)).status_code == 404
    assert (await viewer.put(calendar, json=CALENDAR)).status_code == 403
    assert (await editor.put(calendar, json=CALENDAR | {"era_name": "星\0历"})).status_code == 422
    assert (await editor.put(calendar, json=CALENDAR)).json()["revision"] == 1
    assert (await viewer.get(scene_date)).json()["source"] == "unknown"
    assert (await editor.put(chapter_date, json={"expected_revision": 1, "date": date(21)})).status_code == 400
    result = await editor.put(chapter_date, json={"expected_revision": 1, "date": date(20)})
    assert result.json()["date_label"] == "星历3年雾月20日"
    assert (await viewer.get(scene_date)).json()["source"] == "inherited"
    assert (await viewer.put(scene_date, json={"expected_revision": 2, "date": date(1)})).status_code == 403
    saved = await editor.put(scene_date, json={"expected_revision": 2, "date": date(1, "snow")})
    assert saved.json()["revision"] == 3
    assert (await client.get(chapter_url)).json() == original
    proposed = CALENDAR | {"expected_revision": 3, "months": list(reversed(MONTHS))}
    impact = (await client.post(f"{calendar}/preview", json=proposed)).json()
    assert {item["kind"] for item in impact["reordered"]} == {"chapter", "scene"}
    assert (await client.put(calendar, json=proposed)).status_code == 409
    saved = await client.put(calendar, json=proposed | {"confirm_reorder": True})
    assert saved.json()["revision"] == 4
    assert [i["kind"] for i in (await viewer.get(timeline)).json()["items"]] == ["scene", "chapter"]
    assert [i["kind"] for i in (await viewer.get(timeline, params={"order": "chapter"})).json()["items"]] == [
        "chapter", "scene",
    ]
    page = (await viewer.get(timeline, params={"offset": 1, "limit": 1})).json()
    assert page["total"] == 2 and [i["kind"] for i in page["items"]] == ["chapter"]
    async with app.state.sessionmaker() as db:
        (await db.get(Scene, scene["id"])).active = False
        await db.commit()
    restricted = CALENDAR | {"expected_revision": 4, "months": [MONTHS[0]]}
    assert (await client.get(timeline)).json()["total"] == 1
    blocked = (await client.put(calendar, json=restricted)).json()["blocked"]
    assert len(blocked) == 1 and blocked[0]["scene_id"] == scene["id"] and not blocked[0]["active"]
    cleared = await editor.put(scene_date, json={"expected_revision": 4, "date": None})
    assert cleared.json()["source"] == "inherited"
    assert (await editor.put(calendar, json=restricted)).status_code == 409
    assert (await system_admin.put(calendar, json=restricted | {"expected_revision": 5})).json()["revision"] == 6
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.project_id == project["id"],
                                                         AdminAuditLog.target_label == "编辑作品历法"))
        assert log is not None and log.actor_username == "settings_admin" and log.result == "success"


async def test_narrative_flashback_unknown_and_same_day_pagination(client):
    project, first, _, _ = await setup_scene(client, "归来")
    base = f"/api/v1/projects/{project['id']}"
    root = f"{base}/story-timeline"
    chapters = [first]
    for title in ("未知日期", "回忆出发", "同日同伴"):
        result = await client.post(f"{base}/chapters", json={"title": title})
        assert result.status_code == 201
        chapters.append(result.json())
    assert (await client.put(f"{root}/calendar", json=CALENDAR)).is_success
    for rev, (idx, day) in enumerate(((0, 10), (2, 3), (3, 3)), start=1):
        saved = await client.put(f"{root}/chapter/{chapters[idx]['id']}/date", json={
            "expected_revision": rev, "date": date(day),
        })
        assert saved.is_success, saved.text
    ordered = (await client.get(root)).json()["items"]
    expected = [chapters[i]["id"] for i in [2, 3, 0, 1] for _kind in range(2)]
    assert [i["chapter_id"] for i in ordered] == expected
    assert [i["kind"] for i in ordered] == ["chapter", "scene"] * 4
    assert [i["source"] for i in ordered] == ["chapter", "inherited"] * 3 + ["unknown", "unknown"]
    assert (await client.get(root, params={"limit": 2, "offset": 1})).json()["items"] == ordered[1:3]
    narrative = (await client.get(root, params={"order": "chapter"})).json()["items"]
    assert [i["chapter_id"] for i in narrative if i["kind"] == "chapter"] == [c["id"] for c in chapters]
    renamed = [MONTHS[0] | {"name": "薄雾月"}, MONTHS[1]]
    renamed_result = await client.put(f"{root}/calendar", json=CALENDAR | {"expected_revision": 4, "months": renamed})
    assert renamed_result.is_success
    assert (await client.get(root)).json()["items"][0]["date_label"] == "星历3年薄雾月3日"
    removed = await client.delete(f"{base}/chapters/{first['id']}")
    assert removed.status_code == 204, removed.text
    assert (await client.get(f"{root}/calendar")).json()["revision"] == 6
    assert (await client.get(root)).json()["total"] == 6
    other = await create_project(client)
    foreign = f"/api/v1/projects/{other['id']}/story-timeline"
    for kind, target in (("scene", ordered[1]["scene_id"]), ("chapter", chapters[2]["id"])):
        url = f"{foreign}/{kind}/{target}/date"
        assert (await client.get(url)).status_code == 404
        assert (await client.put(url, json={"expected_revision": 0, "date": None})).status_code == 404


async def test_same_revision_concurrent_saves(client):
    project, chapter, _, scene = await setup_scene(client, "稿件")
    root = f"/api/v1/projects/{project['id']}/story-timeline"
    assert (await client.put(f"{root}/calendar", json=CALENDAR)).is_success
    requests = [
        client.put(f"{root}/{kind}/{target_id}/date", json={"expected_revision": 1, "date": date(4)})
        for kind, target_id in [("chapter", chapter["id"]), ("scene", scene["id"])]
    ]
    results = await asyncio.gather(*requests)
    assert sorted(r.status_code for r in results) == [200, 409]
    page = (await client.get(root)).json()
    assert page["revision"] == 2 and sum(i["explicit_date"] is not None for i in page["items"]) == 1


async def test_calendar_migration_preserves_manuscript_and_blocks_destructive_downgrade(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0074")
    async with engine.begin() as db:
        for model, values in (
            (User, {"username": "旧作者", "password_hash": "-"}), (Project, {"user_id": 1, "title": "旧作品"}),
            (Chapter, {"project_id": 1, "title": "旧章"}), (Scene, {"chapter_id": 1, "content": "旧稿"}),
        ):
            await db.execute(model.__table__.insert().values(id=1, **values))
    await asyncio.to_thread(command.upgrade, cfg, "0075")
    async with engine.begin() as db:
        assert await db.scalar(select(StoryCalendar.project_id)) is None
        assert await db.scalar(select(Scene.content)) == "旧稿"
        for model in (StoryCalendar, ChapterStoryDate, SceneStoryDate):
            await db.run_sync(_check_table, model)
        await db.execute(StoryCalendar.__table__.insert().values(
            project_id=1, era_name="星历", months=MONTHS, revision=1,
        ))
    with pytest.raises(RuntimeError, match="存在故事历法或日期"):
        await asyncio.to_thread(command.downgrade, cfg, "0074")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0075"
        assert await db.scalar(select(StoryCalendar.months)) == MONTHS
        await db.execute(StoryCalendar.__table__.delete())
    await asyncio.to_thread(command.downgrade, cfg, "0074")
    async with engine.begin() as db:
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("story_calendars"))
        assert await db.scalar(select(Scene.content)) == "旧稿"


def _check_table(conn, model):
    inspector = inspect(conn)
    table = model.__table__
    columns = {c["name"]: c for c in inspector.get_columns(table.name)}
    assert set(columns) == set(table.c.keys())
    for c in table.c:
        assert columns[c.name]["nullable"] == c.nullable
        assert columns[c.name]["type"].compile(conn.dialect) == c.type.compile(conn.dialect)
    assert inspector.get_pk_constraint(table.name)["constrained_columns"] == list(table.primary_key.columns.keys())
    assert inspector.get_foreign_keys(table.name)[0]["options"]["ondelete"] == "CASCADE"
    assert {c["name"] for c in inspector.get_check_constraints(table.name)} == {
        c.name for c in table.constraints if isinstance(c, CheckConstraint)
    }
