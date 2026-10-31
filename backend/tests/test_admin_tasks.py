"""管理员现存任务查询、安全投影与参数/权限契约。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from app.models import Project, Task, User
from tests.helpers import register

ROOT = "/api/v1/admin/tasks"


@pytest.fixture
async def monitor_admin(app, make_client):
    client = make_client()
    actor = await register(client, "monitor_admin")
    async with app.state.sessionmaker() as db:
        (await db.get(User, actor["id"])).is_admin = True
        await db.commit()
    await app.state.task_engine.stop()
    return client


async def seed(app):
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        users = [User(username="literal_%", password_hash="x"), User(username="other", password_hash="x")]
        db.add_all(users)
        await db.flush()
        projects = [Project(user_id=users[i].id, title=f"作品{i}") for i in range(2)]
        db.add_all(projects)
        await db.flush()
        projects[1].deleted_at = now
        rows = [Task(user_id=users[i % 2].id, project_id=projects[i % 2].id, kind="agent.turn",
                     title="原始私密指令", input={"secret": "private"}, status=status,
                     stage="调用私有插件: sk-secret", error="https://private/path 密钥sk-secret 正文",
                     created_at=now - timedelta(hours=i), queued_at=now - timedelta(minutes=i + 1),
                     started_at=now - timedelta(minutes=i + 2))
                for i, status in enumerate(("queued", "running", "paused", "failed", "failed", "interrupted"))]
        db.add_all(rows)
        await db.commit()
        return users, projects, rows, now


async def test_admin_list_filters_counts_snapshot_and_safe_projection(app, monitor_admin):
    users, projects, tasks, now = await seed(app)
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        if "filtered_tasks" in statement:
            statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = await monitor_admin.get(ROOT, params=[("status", "failed"), ("status", "interrupted"),
                                                        ("page_size", "1"), ("page", "2")])
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 3 and len(data["items"]) == 1
    assert data["counts"]["failed"] == 2 and data["counts"]["interrupted"] == 1
    assert sum(data["counts"].values()) == 3
    assert len(statements) == 1  # 聚合与分页同一条SQL，不是两次READ COMMITTED读。
    for secret in ("sk-secret", "https://private", "原始私密指令", "调用私有插件", '"input"', '"checkpoint"'):
        assert secret not in response.text
    row = data["items"][0]
    assert row["error_code"] == "execution_failed" and row["error_message"] == "执行失败"
    assert row["title"] == f"项目智能体 #{row['id']}"
    all_rows = (await monitor_admin.get(ROOT)).json()["items"]
    assert {r["owner_user_id"] for r in all_rows} == {u.id for u in users}
    assert {r["project_lifecycle"] for r in all_rows} == {"active", "recycled"}
    assert (await monitor_admin.get(ROOT, params={"q": "原始私密指令"})).json()["total"] == 0
    literal = (await monitor_admin.get(ROOT, params={"q": "_%"})).json()
    assert literal["total"] == 3 and all(r["owner_user_id"] == users[0].id for r in literal["items"])
    combined = (await monitor_admin.get(ROOT, params={
        "owner_user_id": users[0].id, "project_id": projects[0].id, "task_id": tasks[4].id,
        "kind": "agent.turn", "status": "failed", "from_at": (now - timedelta(hours=5)).isoformat(),
        "to_at": (now - timedelta(hours=3)).isoformat(),
    })).json()
    assert combined["total"] == 1 and combined["items"][0]["id"] == tasks[4].id
    empty_page = (await monitor_admin.get(ROOT, params={"page": 100})).json()
    assert empty_page["items"] == [] and empty_page["total"] == 6


async def test_unknown_and_old_clock_and_hub_identity(app, monitor_admin):
    users, projects, tasks, now = await seed(app)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == tasks[0].id).values(queued_at=None))
        await db.execute(update(Task).where(Task.id == tasks[1].id).values(kind="private-kind", status="bad-state"))
        await db.commit()
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(tasks[0].id, statistics_key=str(uuid4()), progress=91, stage="私密信息")
    response = await monitor_admin.get(f"{ROOT}/{tasks[0].statistics_key}")
    row = response.json()
    assert row["queued_seconds"] is None and row["progress"] == 0
    unknown = (await monitor_admin.get(f"{ROOT}/{tasks[1].statistics_key}")).json()
    assert unknown["kind"] == "unknown" and unknown["status"] == "unknown" and not unknown["can_cancel"]
    assert "private-kind" not in str(unknown) and "bad-state" not in str(unknown)
    hub.statistics_key = tasks[0].statistics_key
    hub.stage, hub.progress = "私密插件正文", 32
    fresh = (await monitor_admin.get(f"{ROOT}/{tasks[0].statistics_key}")).json()
    assert fresh["progress"] == 32 and fresh["stage"] == "排队中"
    hub.closed = True
    assert (await monitor_admin.get(f"{ROOT}/{tasks[0].statistics_key}")).json()["progress"] == 0


async def test_succeeded_ignores_stale_raw_error(app, monitor_admin):
    _, _, tasks, _ = await seed(app)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == tasks[0].id).values(status="succeeded"))
        await db.commit()
    row = (await monitor_admin.get(f"{ROOT}/{tasks[0].statistics_key}")).json()
    assert row["status"] == "succeeded" and row["error_code"] is None and row["error_message"] is None


@pytest.mark.parametrize("stage,expected", [
    ("第 2/10 章 · 分析中", "第 2/10 章 · 分析中"),
    ("第 2/10 章 · 生成失败，第 1 次重试", "第 2/10 章 · 生成失败，第 1 次重试"),
    ("通读 第 1/2 批（第 1–20 章）", "通读 第 1/2 批（第 1–20 章）"),
    ("等待确认 2 项修改", "等待确认 2 项修改"),
    ("第 2/10 章 · 私密正文", "生成中"),
    ("通读 第 1/2 批（第 1–20 章）私密正文", "生成中"),
])
async def test_only_exact_numeric_stage_templates_are_projected(app, monitor_admin, stage, expected):
    _, _, tasks, _ = await seed(app)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == tasks[0].id).values(status="running", stage=stage))
        await db.commit()
    row = (await monitor_admin.get(f"{ROOT}/{tasks[0].statistics_key}")).json()
    assert row["stage"] == expected


async def test_duration_sort_uses_current_round_and_null_last(app, monitor_admin):
    _, _, tasks, now = await seed(app)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == tasks[4].id).values(status="queued", queued_at=None))
        await db.execute(update(Task).where(Task.id == tasks[2].id).values(
            status="queued", queued_at=now-timedelta(days=1)
        ))
        await db.commit()
    rows = (await monitor_admin.get(ROOT, params={"sort": "queue_longest"})).json()["items"]
    assert [r["id"] for r in rows[:3]] == [tasks[2].id, tasks[0].id, tasks[4].id]
    assert rows[0]["queued_seconds"] >= 86400 and rows[2]["queued_seconds"] is None
    rows = (await monitor_admin.get(ROOT, params={"sort": "running_longest"})).json()["items"]
    assert rows[0]["id"] == tasks[1].id and rows[0]["queued_seconds"] is None
    assert all(r["running_seconds"] is None for r in rows[1:])


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page_size": 101}, {"task_id": -1}, {"owner_user_id": 0}, {"project_id": 0},
    {"status": "bad"}, {"kind": "private-kind"}, {"sort": "bad"}, {"from_at": "2026-10-01T00:00:00"},
    {"from_at": "2026-10-01T00:00:00Z"},
    {"from_at": "2026-10-03T00:00:00Z", "to_at": "2026-10-01T00:00:00Z"},
])
async def test_filters_validation(monitor_admin, params):
    assert (await monitor_admin.get(ROOT, params=params)).status_code == 422


@pytest.mark.parametrize("body", [
    {"confirm": True, "reason": "  "}, {"confirm": 1, "reason": "原因"}, {"confirm": False, "reason": "原因"},
    {"confirm": True, "reason": "a" * 201}, {"confirm": True, "reason": "原因\nsecret"},
])
async def test_cancel_validation(monitor_admin, body):
    assert (await monitor_admin.post(f"{ROOT}/{uuid4()}/cancel", json=body)).status_code == 422


async def test_current_admin_and_owner_boundaries(app, monitor_admin, client):
    _, _, tasks, now = await seed(app)
    assert (await client.get(ROOT)).status_code == 401
    await register(client, "ordinary")
    for path in (ROOT, f"{ROOT}/{tasks[0].statistics_key}"):
        assert (await client.get(path)).status_code == 403
    response = await client.post(f"{ROOT}/{tasks[0].statistics_key}/cancel", json={"confirm": True, "reason": "处理"})
    assert response.status_code == 403
    assert (await monitor_admin.get(f"/api/v1/tasks/{tasks[0].id}")).status_code == 404
    assert (await monitor_admin.post(f"/api/v1/tasks/{tasks[0].id}/retry")).status_code == 404
    assert (await monitor_admin.get(f"{ROOT}/{uuid4()}")).status_code == 404
    async with app.state.sessionmaker() as db:
        admin = await db.scalar(select(User).where(User.username == "monitor_admin"))
        admin.is_admin = False
        await db.commit()
    assert (await monitor_admin.get(ROOT)).status_code == 403
