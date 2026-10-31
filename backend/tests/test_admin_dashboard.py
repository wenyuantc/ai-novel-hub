"""管理员首页聚合、完整任务统计读取与北京时间历史口径。"""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.models import (
    Chapter,
    ChapterVersion,
    DashboardDailySnapshot,
    DashboardTracking,
    Project,
    Task,
    TaskStatistic,
    User,
    WorkshopSubmission,
)
from app.services import admin_dashboard_service as svc
from app.services import task_statistics_service
from tests.helpers import create_project, purge_trashed_project, register

ROOT = "/api/v1/admin/dashboard"
NOW = datetime(2026, 11, 1, 16, tzinfo=UTC)  # 北京时间11月2日00:00


@pytest.fixture
async def dashboard_admin(app, make_client):
    client = make_client()
    user = await register(client, "dashboard_manager")
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    return client


async def clock(app, monkeypatch, *, since: datetime = NOW - timedelta(days=10)) -> None:
    await task_statistics_service.initialize_tracking(app.state.sessionmaker)
    monkeypatch.setattr(svc, "now_utc", lambda: NOW)
    async with app.state.sessionmaker() as db:
        (await db.get(DashboardTracking, 1)).started_at = since
        await db.commit()


def snapshot(day: date, *, captured_at: datetime | None = None, **totals) -> DashboardDailySnapshot:
    values = dict(users=0, projects=0, chapters=0, total_words=0, pending_submissions=0)
    values.update(totals)
    return DashboardDailySnapshot(
        day=day,
        captured_at=captured_at or svc._day_start(day + timedelta(days=1)) - timedelta(seconds=30),
        **values,
    )


def statistic(
    status: str,
    *,
    created: datetime | None = NOW - timedelta(days=2),
    finished: datetime | None = None,
    origin: str = "new",
    removed: datetime | None = None,
) -> TaskStatistic:
    return TaskStatistic(
        key=str(uuid4()), kind="chapter.generate", origin=origin, observed_at=NOW - timedelta(days=2),
        created_event_at=created if origin == "new" else None, status=status,
        finished_event_at=finished, removed_at=removed,
    )


@pytest.mark.parametrize("path", ["overview", "tasks", "history"])
async def test_dashboard_requires_current_admin(app, client, dashboard_admin, path):
    assert (await client.get(f"{ROOT}/{path}")).status_code == 401
    ordinary = await register(client, "ordinary_reader")
    assert (await client.get(f"{ROOT}/{path}")).status_code == 403
    assert (await dashboard_admin.get(f"{ROOT}/{path}")).status_code == 200
    async with app.state.sessionmaker() as db:
        (await db.get(User, ordinary["id"])).is_active = False
        admin = await db.scalar(select(User).where(User.username == "dashboard_manager"))
        admin.is_admin = False
        await db.commit()
    assert (await client.get(f"{ROOT}/{path}")).status_code == 401
    assert (await dashboard_admin.get(f"{ROOT}/{path}")).status_code == 403


async def test_overview_includes_all_users_recycled_and_deleting_projects(app, client, dashboard_admin):
    owner = await register(client, "content_owner")
    async with app.state.sessionmaker() as db:
        disabled = User(username="disabled_account", password_hash="not-returned", is_active=False)
        db.add(disabled)
        await db.flush()
        active = Project(user_id=owner["id"], title="正常作品")
        recycled = Project(user_id=disabled.id, title="回收作品", deleted_at=NOW)
        deleting = Project(
            user_id=disabled.id, title="删除中作品", deletion_started_at=NOW, deletion_token=str(uuid4())
        )
        db.add_all([active, recycled, deleting])
        await db.flush()
        chapters = [
            Chapter(project_id=active.id, chapter_no=1, content="正文", word_count=2, status="done"),
            Chapter(project_id=recycled.id, chapter_no=1, content="回收正文", word_count=4),
            Chapter(project_id=deleting.id, chapter_no=1, content="写作中", word_count=3, status="generating"),
            Chapter(project_id=recycled.id, chapter_no=2, content="", word_count=0),
        ]
        db.add_all(chapters)
        await db.flush()
        db.add(ChapterVersion(chapter_id=chapters[0].id, seq=1, source="manual", content="旧版", word_count=999))
        for status in ("pending", "pending", "approved", "cancelled"):
            db.add(WorkshopSubmission(
                submitter_id=owner["id"], kind="style", name=status, category="general", content={}, status=status,
            ))
        await db.commit()
    response = await dashboard_admin.get(f"{ROOT}/overview")
    assert response.status_code == 200, response.text
    data = response.json()
    assert {key: data[key] for key in ("users", "projects", "chapters", "total_words", "pending_submissions")} == {
        "users": 3, "projects": 3, "chapters": 4, "total_words": 9, "pending_submissions": 2,
    }
    assert set(data) == {
        "as_of", "tracking_since", "timezone", "users", "projects", "chapters", "total_words", "pending_submissions",
    }
    assert data["timezone"] == "Asia/Shanghai"


async def test_recycle_restore_keep_totals_but_purge_changes_current_not_snapshot(
    app, client, dashboard_admin, monkeypatch
):
    await clock(app, monkeypatch)
    await register(client, "recycle_owner")
    project = await create_project(client)
    pid = project["id"]
    async with app.state.sessionmaker() as db:
        db.add(Chapter(project_id=pid, chapter_no=1, content="正文", word_count=2))
        await db.commit()
    before = (await dashboard_admin.get(f"{ROOT}/overview")).json()
    await svc.collect_snapshot(app.state.sessionmaker)
    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    recycled = (await dashboard_admin.get(f"{ROOT}/overview")).json()
    for key in ("projects", "chapters", "total_words"):
        assert recycled[key] == before[key]
    listing = (await dashboard_admin.get("/api/v1/admin/recycle-bin/projects")).json()["items"]
    item = next(row for row in listing if row["id"] == pid)
    restored = await dashboard_admin.post(
        f"/api/v1/admin/recycle-bin/projects/{pid}/restore", json={"deleted_at": item["deleted_at"], "confirm": True}
    )
    assert restored.status_code == 204, restored.text
    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    await purge_trashed_project(dashboard_admin, pid)
    after = (await dashboard_admin.get(f"{ROOT}/overview")).json()
    assert (after["projects"], after["chapters"], after["total_words"]) == (0, 0, 0)
    async with app.state.sessionmaker() as db:
        stored = await db.get(DashboardDailySnapshot, NOW.astimezone(svc.STATISTICS_TIMEZONE).date())
        assert (stored.projects, stored.chapters, stored.total_words) == (1, 1, 2)


async def test_empty_counts_are_real_zero_not_missing(app, dashboard_admin):
    data = (await dashboard_admin.get(f"{ROOT}/overview")).json()
    assert data["users"] == 1  # 当前登录的管理员本身也计入所有账号。
    assert [data[field] for field in ("projects", "chapters", "total_words", "pending_submissions")] == [0] * 4
    task_data = (await dashboard_admin.get(f"{ROOT}/tasks")).json()
    assert all(task_data[field] == 0 for field in (
        "baseline_tasks", "new_tasks", "queued", "running", "paused", "succeeded", "failed", "cancelled",
        "interrupted", "failed_24h", "interrupted_24h", "removed_unfinished",
    ))


async def test_task_statistics_read_independent_history_and_exact_rolling_24h(
    app, dashboard_admin, monkeypatch
):
    await clock(app, monkeypatch)
    await app.state.task_engine.stop()
    cutoff = NOW - timedelta(hours=24)
    async with app.state.sessionmaker() as db:
        admin_id = await db.scalar(select(User.id).where(User.username == "dashboard_manager"))
        db.add_all([
            Task(user_id=admin_id, kind="chapter.generate", status=status, input={})
            for status in ("queued", "running", "paused")
        ])
        records = [
            statistic("failed", finished=cutoff + timedelta(microseconds=1)),
            statistic("failed", finished=cutoff),
            statistic("failed", finished=NOW + timedelta(microseconds=1)),
            statistic("interrupted", finished=NOW),
            statistic("interrupted", finished=cutoff),
            statistic("succeeded", finished=NOW - timedelta(hours=2)),
            statistic("cancelled", finished=NOW - timedelta(hours=1)),
            statistic("failed", origin="baseline"),
            statistic("succeeded", origin="baseline", finished=NOW - timedelta(hours=1)),
            statistic("running", removed=NOW - timedelta(hours=1)),
        ]
        db.add_all(records)
        await db.commit()
    response = await dashboard_admin.get(f"{ROOT}/tasks")
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["queued"], data["running"], data["paused"]) == (1, 1, 1)
    assert (data["baseline_tasks"], data["new_tasks"]) == (2, 8)
    assert (data["succeeded"], data["failed"], data["cancelled"], data["interrupted"]) == (2, 2, 1, 2)
    assert (data["failed_24h"], data["interrupted_24h"], data["removed_unfinished"]) == (1, 1, 1)
    async with app.state.sessionmaker() as db:
        await db.execute(delete(Task))
        await db.commit()
    after = (await dashboard_admin.get(f"{ROOT}/tasks")).json()
    assert (after["queued"], after["running"], after["paused"]) == (0, 0, 0)
    for key in ("new_tasks", "succeeded", "failed", "cancelled", "interrupted", "failed_24h", "interrupted_24h"):
        assert after[key] == data[key]


@pytest.mark.parametrize("days", [7, 30, 90])
async def test_history_ranges_are_bounded_and_default_is_30(app, dashboard_admin, monkeypatch, days):
    await clock(app, monkeypatch)
    response = await dashboard_admin.get(f"{ROOT}/history", params={"days": days})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["days"] == days and len(data["points"]) == days
    assert data["end_date"] == "2026-11-02"
    assert data["start_date"] == (date(2026, 11, 2) - timedelta(days=days - 1)).isoformat()
    assert (await dashboard_admin.get(f"{ROOT}/history")).json()["days"] == 30


@pytest.mark.parametrize("days", [0, 1, 8, 31, 91, "7.0", "invalid"])
async def test_history_rejects_invalid_days(dashboard_admin, days):
    assert (await dashboard_admin.get(f"{ROOT}/history", params={"days": days})).status_code == 422


async def test_history_keeps_missing_distinct_from_zero_and_uses_shanghai_days(
    app, dashboard_admin, monkeypatch
):
    since = datetime(2026, 10, 28, 2, tzinfo=UTC)
    await clock(app, monkeypatch, since=since)
    async with app.state.sessionmaker() as db:
        db.add_all([
            snapshot(date(2026, 10, 27), users=999),  # 起点前不得伪装成已采集历史。
            snapshot(date(2026, 10, 28), users=3),
            snapshot(date(2026, 10, 30)),
            snapshot(date(2026, 11, 2), captured_at=NOW, users=4),
            statistic("succeeded", created=NOW - timedelta(microseconds=1), finished=NOW),
            statistic("failed", created=NOW, finished=NOW),
        ])
        await db.commit()
    data = (await dashboard_admin.get(f"{ROOT}/history", params={"days": 7})).json()
    points = {point["day"]: point for point in data["points"]}
    assert points["2026-10-27"]["totals"] is None and points["2026-10-27"]["tasks"] is None
    assert points["2026-10-29"]["totals"] is None and points["2026-10-29"]["tasks"] is None
    assert points["2026-10-30"]["totals"]["users"] == 0
    assert points["2026-10-30"]["tasks"] == dict(created=0, succeeded=0, failed=0, cancelled=0, interrupted=0)
    assert points["2026-11-01"]["totals"] is None
    assert points["2026-11-01"]["tasks"]["created"] == 1
    assert points["2026-11-01"]["tasks"]["succeeded"] == 0
    assert points["2026-11-01"]["partial"] is True
    assert points["2026-11-02"]["tasks"]["created"] == 1
    assert points["2026-11-02"]["tasks"]["succeeded"] == 1
    assert points["2026-11-02"]["tasks"]["failed"] == 1
    assert points["2026-10-28"]["partial"] is True
    assert points["2026-11-02"]["partial"] is True
    assert data["comparison"] == dict(
        from_day="2026-10-27", to_day="2026-11-02", users=None, projects=None, chapters=None, total_words=None,
    )


async def test_history_negative_net_changes_use_strict_endpoints(app, dashboard_admin, monkeypatch):
    await clock(app, monkeypatch)
    async with app.state.sessionmaker() as db:
        db.add_all([
            snapshot(date(2026, 10, 27), users=5, projects=10, chapters=100, total_words=1000),
            snapshot(date(2026, 10, 31), users=999),
            snapshot(date(2026, 11, 2), captured_at=NOW, users=3, projects=7, chapters=70, total_words=900),
        ])
        await db.commit()
    data = (await dashboard_admin.get(f"{ROOT}/history", params={"days": 7})).json()
    assert data["comparison"] == dict(
        from_day="2026-10-27", to_day="2026-11-02", users=-2, projects=-3, chapters=-30, total_words=-100,
    )
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(DashboardDailySnapshot, date(2026, 10, 27)))
        await db.commit()
    missing_first = (await dashboard_admin.get(f"{ROOT}/history", params={"days": 7})).json()
    assert all(missing_first["comparison"][key] is None for key in ("users", "projects", "chapters", "total_words"))


async def test_missing_initialization_is_failure_not_zero(app, dashboard_admin):
    async with app.state.sessionmaker() as db:
        await db.execute(delete(DashboardTracking))
        await db.commit()
    for path in ("overview", "tasks", "history"):
        response = await dashboard_admin.get(f"{ROOT}/{path}")
        assert response.status_code == 503
        assert response.json()["detail"] == "管理统计尚未初始化，请稍后重试"
