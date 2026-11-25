import asyncio
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import update

from app.models import Project
from tests.helpers import create_project, register
from tests.schema_helpers import isolated_worker_database


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def _book(client):
    user = await register(client)
    project = await create_project(client)
    return user["id"], project["id"], f"/api/v1/projects/{project['id']}/writing-stats"


async def test_personal_goal_conflict_and_close(client):
    _, _, base = await _book(client)
    overview = await client.get(base + "/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["goal"] == {"period": None, "target_words": None, "revision": 0}
    serial = base.removesuffix("/writing-stats") + "/serialization"
    assert (await client.put(serial + "/goal", json={"daily_goal": 2, "expected_revision": 0})).is_success
    requests = [{"period": "week", "target_words": n, "expected_revision": 0} for n in (4000, 5000)]
    results = await asyncio.gather(*(client.put(base + "/goal", json=body) for body in requests))
    assert sorted(res.status_code for res in results) == [200, 409]
    winner = next(res.json() for res in results if res.status_code == 200)
    assert winner["revision"] == 1
    change = {"period": "day", "target_words": 3000, "expected_revision": 1}
    assert (await client.put(base + "/goal", json=change)).json()["revision"] == 2
    close = {"period": None, "target_words": None, "expected_revision": 2}
    assert (await client.put(base + "/goal", json=close)).json()["revision"] == 3
    assert (await client.put(base + "/goal", json=change)).status_code == 409
    assert (await client.get(base + "/overview")).json()["goal"]["period"] is None
    assert (await client.get(serial + "/overview?month=2026-11")).json()["daily_goal"] == 2
    for period, words in [(None, 2), ("day", None), ("day", -1), ("week", True), ("month", 1)]:
        bad = {"period": period, "target_words": words, "expected_revision": 3}
        assert (await client.put(base + "/goal", json=bad)).status_code == 422


async def test_personal_series_and_week_boundary(app, client, monkeypatch):
    from app.models.writing_stats import WritingDailyStat, WritingStatsTracking
    from app.services import writing_stats_overview as service

    owner, pid, base = await _book(client)
    # 周二 16:00 UTC 是北京时间周三零点。
    now = datetime(2026, 11, 24, 16, tzinfo=UTC)
    monkeypatch.setattr(service, "_now", lambda: now)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Project).where(Project.id == pid).values(created_at=datetime(2020, 1, 1, tzinfo=UTC)))
        await db.execute(update(WritingStatsTracking).values(started_at=datetime(2026, 11, 20, 16, tzinfo=UTC)))
        for day, manual, ai in [(23, 800, 0), (24, 1200, 0), (25, 0, 5000)]:
            db.add(WritingDailyStat(user_id=owner, project_id=pid, day=date(2026, 11, day),
                                    manual_words=manual, ai_words=ai))
        await db.commit()
    assert (await client.put(base + "/goal", json={
        "period": "week", "target_words": 5000, "expected_revision": 0,
    })).status_code == 200
    query = {"start_date": "2026-11-20", "end_date": "2026-11-22"}
    data = (await client.get(base + "/overview", params=query)).json()
    assert data["today"] == "2026-11-25"
    assert (data["today_manual_words"], data["today_ai_words"]) == (0, 5000)
    assert (data["period_manual_words"], data["streak_days"]) == (2000, 2)
    assert [(d["manual_words"], d["ai_words"], d["partial"]) for d in data["days"]] == [
        (None, None, False), (0, 0, True), (0, 0, False)]
    now = datetime(2026, 11, 25, 16, tzinfo=UTC)
    assert (await client.get(base + "/overview")).json()["streak_days"] == 0
    now = datetime(2026, 11, 29, 16, tzinfo=UTC)
    assert (await client.get(base + "/overview")).json()["period_manual_words"] == 0


async def test_project_creation_date_and_streak_across_year(app, client, monkeypatch):
    from app.models.writing_stats import WritingDailyStat, WritingStatsTracking
    from app.services import writing_stats_overview as service

    owner, pid, base = await _book(client)
    created = datetime(2026, 12, 29, 16, 30, tzinfo=UTC)
    monkeypatch.setattr(service, "_now", lambda: datetime(2027, 1, 1, 18, tzinfo=UTC))
    async with app.state.sessionmaker() as db:
        await db.execute(update(Project).where(Project.id == pid).values(created_at=created))
        await db.execute(update(WritingStatsTracking).values(started_at=datetime(2025, 1, 1, tzinfo=UTC)))
        for day in [date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)]:
            db.add(WritingDailyStat(user_id=owner, project_id=pid, day=day, manual_words=5, ai_words=0))
        await db.commit()
    data = (await client.get(base + "/overview?start_date=2026-12-29")).json()
    assert data["tracking_started_at"] == "2026-12-29T16:30:00Z"
    assert data["streak_days"] == 4
    assert [d["manual_words"] for d in data["days"]] == [None, 5, 5, 5, 5]
    assert [d["partial"] for d in data["days"]] == [False, True, False, False, True]


async def test_permissions_and_personal_project_scope(client, make_client, system_admin, app):
    from app.models.writing_stats import WritingDailyStat
    from app.services.serialization_read import BEIJING

    owner, pid, base = await _book(client)
    project_base = base.removesuffix("/writing-stats")
    editor, viewer, stranger = make_client(), make_client(), make_client()
    await register(stranger, "stranger")
    for guest, role in [(editor, "editor"), (viewer, "viewer")]:
        guest_info = await register(guest, role)
        invite = (await client.post(project_base + "/collaborators", json={"username": role, "role": role})).json()
        link = f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept"
        accepted = await guest.post(link, json={"expected_revision": 1})
        assert accepted.status_code == 200, accepted.text
        if role == "editor":
            editor_id = guest_info["id"]
    day = datetime.now(BEIJING).date()
    other = await create_project(client)
    async with app.state.sessionmaker() as db:
        for uid, book, words in [(owner, pid, 10), (editor_id, pid, 30), (owner, other["id"], 50)]:
            db.add(WritingDailyStat(user_id=uid, project_id=book, day=day, manual_words=words, ai_words=2*words))
        await db.commit()
    body = {"period": "week", "target_words": 5000, "expected_revision": 0}
    assert (await editor.put(base + "/goal", json=body)).status_code == 200
    for guest, words in [(client, 10), (editor, 30), (viewer, 0), (system_admin, 0)]:
        data = (await guest.get(base + "/overview")).json()
        assert (data["today_manual_words"], data["today_ai_words"]) == (words, 2*words)
        assert data["goal"]["target_words"] == (5000 if guest == editor else None)
    assert (await viewer.put(base + "/goal", json=body)).status_code == 403
    assert (await stranger.get(base + "/overview")).status_code == 404
    other_overview = await client.get(f"/api/v1/projects/{other['id']}/writing-stats/overview")
    assert other_overview.json()["today_manual_words"] == 50
    assert (await client.delete(f"{project_base}/collaborators/{editor_id}?expected_revision=2")).is_success
    assert (await editor.get(base + "/overview")).status_code == 404


@pytest.mark.parametrize("query", [
    "start_date=2024-01-01&end_date=2025-01-01", "end_date=9999-12-31", "start_date=2026-02-30",
    "start_date=2025-01-02&end_date=2025-01-01", "start_date=0",
])
async def test_invalid_date_ranges(client, query):
    _, _, base = await _book(client)
    assert (await client.get(base + "/overview?" + query)).status_code == 422
