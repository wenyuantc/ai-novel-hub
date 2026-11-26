"""单书历法、跨章场景和历史证据在新身份下保持可用。"""

import json
from copy import deepcopy

import pytest

from app.core.errors import BadRequestError, ConflictError
from app.models import ConsistencyIssue, Project
from app.schemas.transfer import BackupOptions
from app.services.project_transfer import exporter, importer
from app.services.project_transfer.story_timeline import TIMELINE_TABLES
from tests.schema_helpers import isolated_worker_database
from tests.test_project_transfer import API, _backup, _import
from tests.test_scene_move import preview
from tests.test_scene_planning import scene_setup
from tests.test_serialization_concurrency import successful

MONTHS = [{"id": "fog", "name": "雾月", "days": 20}, {"id": "snow", "name": "雪月", "days": 15}]


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def test_timeline_roundtrip_cross_chapter_and_historical_evidence(app, client):
    project, base, origin, source, scenes = await scene_setup(client)
    sid = scenes[1]["id"]
    target = successful(await client.post(base + "/chapters", json={"title": "归来", "content": "初十归来"}))
    tl = base + "/story-timeline"
    revision = successful(await client.put(tl + "/calendar", json={
        "expected_revision": 0, "era_name": "星历", "months": MONTHS,
    }))["revision"]
    for kind, target_id, month, day in (("chapter", target["id"], "snow", 10), ("scene", sid, "fog", 3)):
        revision = successful(await client.put(f"{tl}/{kind}/{target_id}/date", json={
            "expected_revision": revision, "date": {"year": 3, "month_id": month, "day": day},
        }))["revision"]
    _, body = await preview(client, source, sid, target)
    successful(await client.post(f"{source}/{sid}/move", json=body))

    stamp = {"scene_id": sid, "source": "scene", "label": "改名前的雾月", "year": 3,
             "month_id": "fog", "month_order": 1, "day": 3}
    evidence = {"chapter_id": origin["id"], "fact_id": "1", "quote": "第二段", "start": None, "end": None,
                "text_hash": "a" * 64, "story_time": stamp}
    async with app.state.sessionmaker() as db:
        db.add(ConsistencyIssue(project_id=project["id"], title="旧证据", category="timeline", severity="high",
                                identity_key="1" * 64, evidence=[
                                    evidence, {**evidence, "story_time": {**stamp, "scene_id": 2_000_000}},
                                ]))
        await db.commit()
    backup = await _backup(client, project["id"], versions=0)
    restored = await _import(client, backup)
    copied = restored["project"]["id"]
    url = f"{API}/{copied}/story-timeline"
    items = successful(await client.get(url))["items"]
    assert [r["source"] for r in items] == ["scene", "chapter", "inherited", "unknown", "unknown"]
    moved, chapter, inherited = items[:3]
    assert moved["chapter_id"] == chapter["chapter_id"] == inherited["chapter_id"] != target["id"]
    assert moved["scene_id"] != sid
    assert moved["effective_date"] == {"year": 3, "month_id": "fog", "day": 3}
    assert inherited["explicit_date"] is None and inherited["effective_date"] == chapter["explicit_date"]
    assert successful(await client.get(url + "/calendar")) == {"era_name": "星历", "months": MONTHS, "revision": 1}
    old = deepcopy(backup)
    old["version"] = 17
    for table in TIMELINE_TABLES:
        old["tables"].pop(table)
    with pytest.raises(BadRequestError, match="旧版备份不支持故事日期证据"):
        importer.parse_backup(json.dumps(old).encode())
    recovered = (await _backup(client, copied))["tables"]["consistency_issues"][0]["evidence"]
    assert recovered[0]["story_time"] == {**stamp, "scene_id": moved["scene_id"]}
    assert recovered[1]["story_time"] == {**stamp, "scene_id": None}
    assert recovered[0]["chapter_id"] == items[-1]["chapter_id"] != origin["id"]
    assert any("故事日期证据" in warning for warning in restored["preview"]["warnings"])


async def test_timeline_preview_validates_calendar_references_and_old_books(app, client):
    project, _, origin, _, scenes = await scene_setup(client)
    data = await _backup(client, project["id"])
    tables = data["tables"]
    old = deepcopy(data)
    old.update(version=17, app_schema="0074")
    for name in TIMELINE_TABLES:
        old["tables"].pop(name)
    copied = (await _import(client, old))["project"]["id"]
    assert all(item["source"] == "unknown" for item in successful(
        await client.get(f"{API}/{copied}/story-timeline"))["items"])

    calendar = {"era_name": "星历", "months": MONTHS}
    date = {"year": 3, "day": 20, "month_id": "fog"}
    tables.update(story_calendars=[calendar], chapter_story_dates=[{**date, "chapter_id": origin["id"]}],
                  scene_story_dates=[{**date, "scene_id": scenes[0]["id"]}])

    def parse(**replace):
        return importer.parse_backup(json.dumps({**data, "tables": {**tables, **replace}}).encode())

    parse()
    for replace in (
        {"story_calendars": []}, {"story_calendars": [calendar, calendar]},
        {"story_calendars": [{**calendar, "project_id": project["id"] + 1}]},
        {"story_calendars": [{**calendar, "months": MONTHS + [MONTHS[0]]}]},
        {"scene_story_dates": tables["scene_story_dates"] * 2},
        {"chapter_story_dates": [{**date, "chapter_id": 2_000_000}]},
        {"scene_story_dates": [{**date, "scene_id": scenes[0]["id"], "day": 21}]},
        {"scene_story_dates": [{**date, "scene_id": scenes[0]["id"], "month_id": "future"}]},
    ):
        with pytest.raises(BadRequestError):
            parse(**replace)
    with pytest.raises(BadRequestError, match="旧版备份不支持故事时间线"):
        importer.parse_backup(json.dumps({**data, "version": 17}).encode())

    async with app.state.sessionmaker() as db:
        owner = (await db.get(Project, project["id"])).user_id
        plan = await exporter.prepare_export(db, owner, project["id"], BackupOptions())
    successful(await client.put(f"{API}/{project['id']}/story-timeline/calendar",
                                json={**calendar, "expected_revision": 0}))
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError, match="故事日期已变化"):
            await exporter.validate_export(db, plan)
