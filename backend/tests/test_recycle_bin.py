"""回收站权限、关联数据往返、期限与危险操作确认。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AdminAuditLog,
    AgentConversation,
    AgentMessage,
    Base,
    Chapter,
    InspirationSession,
    Project,
    ProjectCover,
    Task,
    User,
)
from app.services import project_lifecycle, recycle_bin_service
from app.services.audit_writer import AuditOperation
from tests.cover_fixtures import PNG
from tests.helpers import create_project, register
from tests.test_project_transfer import _backup, _import, _project_tables, _seed

PROJECTS = "/api/v1/projects"
RECYCLE = "/api/v1/admin/recycle-bin/projects"


@pytest.fixture
async def admin(app, make_client):
    client = make_client()
    data = await register(client, "manager")
    async with app.state.sessionmaker() as db:
        (await db.get(User, data["id"])).is_admin = True
        await db.commit()
    return client


async def item(admin, project_id):
    response = await admin.get(RECYCLE)
    assert response.status_code == 200, response.text
    return next(p for p in response.json()["items"] if p["id"] == project_id)


async def _project_snapshot(app):
    result = {}
    async with app.state.sessionmaker() as db:
        for name in sorted(_project_tables()):
            table = Base.metadata.tables[name]
            rows = (await db.execute(select(table).order_by(*table.primary_key.columns))).mappings().all()
            result[name] = [dict(row) for row in rows]
            if name == "projects":
                for row in result[name]:
                    row.pop("updated_at")
                    for field in project_lifecycle.LIFECYCLE_FIELDS:
                        row.pop(field, None)
    return result


async def test_complete_project_is_hidden_restored_in_place_then_purged(app, admin, client, make_client, fake_llm):
    owner = await register(client)
    project = await create_project(client)
    pid = project["id"]
    ids = await _seed(app, pid)
    chapter_id = ids["chapters"][0]
    assert (await client.put(f"{PROJECTS}/{pid}/cover", files={"file": ("cover.png", PNG)})).status_code == 200
    key = (await client.post("/api/v1/api-keys", json={"name": "开放读取", "project_ids": [pid]})).json()["key"]
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=pid, title="智能体历史")
        db.add(conversation)
        await db.flush()
        db.add(AgentMessage(conversation_id=conversation.id, role="assistant", content="历史消息"))
        history = Task(
            user_id=owner["id"],
            project_id=pid,
            chapter_id=chapter_id,
            kind="chapter.generate",
            status="succeeded",
            output_text="历史输出",
            input={},
        )
        db.add(history)
        await db.commit()
        history_id = history.id
    other = make_client()
    await register(other, "other")
    other_project = await create_project(other, title="其他作品")
    before = await _project_snapshot(app)
    assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
    recycled = await item(admin, pid)
    assert recycled["owner_user_id"] == recycled["deleted_by_user_id"] == owner["id"]
    assert recycled["restorable"] and recycled["chapter_count"] == 3
    assert (await client.get(PROJECTS)).json() == []
    assert (await admin.get("/api/v1/admin/projects")).json()["total"] == 1
    assert (await other.get(f"{PROJECTS}/{other_project['id']}")).status_code == 200
    for path in [
        "",
        "/world",
        "/characters",
        "/outlines/tree",
        "/organizations",
        "/careers",
        "/relationships",
        "/foreshadows",
        "/kb/settings",
        "/kb/documents",
        "/memories",
        "/reader/toc",
        "/cover",
        "/export",
        "/backup",
        f"/chapters/{chapter_id}",
        f"/chapters/{chapter_id}/versions",
    ]:
        for viewer in (client, admin, other):
            response = await viewer.get(f"{PROJECTS}/{pid}{path}")
            assert response.status_code == 404, (path, response.text)
    for suffix in ("", "/output", "/stream"):
        assert (await client.get(f"/api/v1/tasks/{history_id}{suffix}")).status_code == 404
    assert (await client.get("/api/v1/tasks")).json() == []
    assert (await client.delete("/api/v1/tasks")).json()["deleted"] == 0
    headers = {"X-API-Key": key}
    assert (await client.get("/api/open/v1/novels", headers=headers)).json()["total"] == 0
    assert (await client.get(f"/api/open/v1/novels/{pid}", headers=headers)).status_code == 404
    assert (await client.patch(f"{PROJECTS}/{pid}", json={"title": "禁止修改"})).status_code == 404
    assert (await client.post(f"{PROJECTS}/{pid}/chapters", json={"content": "禁止新增"})).status_code == 404
    assert (await client.post(f"/api/v1/tasks/{history_id}/retry")).status_code == 404
    assert (
        await admin.post(f"{RECYCLE}/{pid}/restore", json={"deleted_at": recycled["deleted_at"], "confirm": True})
    ).status_code == 204
    assert await _project_snapshot(app) == before
    async with app.state.sessionmaker() as db:
        assert project_lifecycle.as_utc((await db.get(Project, pid)).last_deleted_at).isoformat() == recycled[
            "deleted_at"
        ].replace("Z", "+00:00")
    assert (await client.get(f"{PROJECTS}/{pid}/cover")).content == PNG
    assert (await client.get(f"/api/open/v1/novels/{pid}", headers=headers)).status_code == 200
    assert not fake_llm.calls and not fake_llm.embed_calls and not fake_llm.image_calls
    assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
    latest = await item(admin, pid)
    assert (
        await admin.post(
            f"{RECYCLE}/{pid}/purge",
            json={"deleted_at": latest["deleted_at"], "title": latest["title"], "confirm": True},
        )
    ).status_code == 204
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, pid) is None
        assert await db.get(Chapter, chapter_id) is None
        assert await db.get(ProjectCover, pid) is None
        assert await db.get(Task, history_id) is None
        sessions = list(await db.scalars(select(InspirationSession).where(InspirationSession.user_id == owner["id"])))
        assert sessions and all(s.project_id is None for s in sessions)
        assert await db.get(Project, other_project["id"]) is not None
        assert await db.get(User, owner["id"]) is not None


async def test_permissions_record_generation_and_exact_confirmation(app, admin, client, make_client):
    assert (await client.get(RECYCLE)).status_code == 401
    await register(client)
    project = await create_project(client, title="精确名称")
    pid = project["id"]
    assert (await client.get(RECYCLE)).status_code == 403
    assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
    old = await item(admin, pid)
    body = {"deleted_at": old["deleted_at"], "confirm": True}
    for suffix in ("restore", "purge"):
        assert (
            await client.post(
                f"{RECYCLE}/{pid}/{suffix}", json={**body, **({"title": old["title"]} if suffix == "purge" else {})}
            )
        ).status_code == 403
    for confirm in (False, 1, "true", None):
        assert (await admin.post(f"{RECYCLE}/{pid}/restore", json={**body, "confirm": confirm})).status_code == 422
    assert (await admin.post(f"{RECYCLE}/{pid}/purge", json={**body, "title": "精确名称 "})).status_code == 400
    assert (await admin.post(f"{RECYCLE}/{pid}/restore", json=body)).status_code == 204
    assert (await admin.post(f"{RECYCLE}/{pid}/restore", json=body)).status_code == 409
    assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
    current = await item(admin, pid)
    assert current["deleted_at"] != old["deleted_at"]
    assert (await admin.post(f"{RECYCLE}/{pid}/purge", json={**body, "title": old["title"]})).status_code == 409
    assert (
        await admin.post(
            f"{RECYCLE}/{pid}/purge",
            json={"deleted_at": current["deleted_at"], "title": current["title"], "confirm": True},
        )
    ).status_code == 204
    assert (await admin.post(f"{RECYCLE}/{pid}/restore", json=body)).status_code == 404
    admin_id = (await admin.get("/api/v1/auth/me")).json()["id"]
    async with app.state.sessionmaker() as db:
        (await db.get(User, admin_id)).is_admin = False
        await db.commit()
    assert (await admin.get(RECYCLE)).status_code == 403


async def test_list_literal_filters_pagination_and_disabled_owner_restore(app, admin, client):
    owner = await register(client)
    first = await create_project(client, title="字面%_\\作品")
    second = await create_project(client, title="另一本")
    for p in (first, second):
        assert (await client.delete(f"{PROJECTS}/{p['id']}")).status_code == 204
    assert (await admin.get(RECYCLE, params={"q": "%_\\"})).json()["total"] == 1
    assert (await admin.get(RECYCLE, params={"q": "ALICE", "owner_user_id": owner["id"]})).json()["total"] == 2
    assert len((await admin.get(RECYCLE, params={"page_size": 1, "page": 2})).json()["items"]) == 1
    assert (await admin.get(RECYCLE, params={"page_size": 101})).status_code == 422
    assert (await admin.patch(f"/api/v1/admin/users/{owner['id']}", json={"is_active": False})).status_code == 200
    row = await item(admin, first["id"])
    assert (
        await admin.post(f"{RECYCLE}/{first['id']}/restore", json={"deleted_at": row["deleted_at"], "confirm": True})
    ).status_code == 204
    async with app.state.sessionmaker() as db:
        assert not (await db.get(User, owner["id"])).is_active
    assert (await client.get(PROJECTS)).status_code == 401


async def test_retention_boundary_system_expiry_and_independent_failures(app, admin, client, monkeypatch):
    await register(client)
    fixed = datetime(2026, 10, 31, 12, 0, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr(project_lifecycle, "now_utc", lambda: fixed)
    expired = await create_project(client, title="刚好到期")
    recent = await create_project(client, title="还差一微秒")
    for p in (expired, recent):
        assert (await client.delete(f"{PROJECTS}/{p['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        (await db.get(Project, expired["id"])).deleted_at = fixed - timedelta(days=30)
        (await db.get(Project, recent["id"])).deleted_at = fixed - timedelta(days=30) + timedelta(microseconds=1)
        await db.commit()
    old = await item(admin, expired["id"])
    fresh = await item(admin, recent["id"])
    assert not old["restorable"] and fresh["restorable"]
    assert (
        await admin.post(f"{RECYCLE}/{expired['id']}/restore", json={"deleted_at": old["deleted_at"], "confirm": True})
    ).status_code == 409
    assert await recycle_bin_service.purge_expired(app.state.task_engine, now=fixed) == 1
    assert await recycle_bin_service.purge_expired(app.state.task_engine, now=fixed) == 0
    assert (await item(admin, recent["id"]))["restorable"]
    async with app.state.sessionmaker() as db:
        event = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "project.expire"))
        assert event.actor_user_id is None and event.actor_username == "系统自动清理"
        assert event.result == "success" and event.summary["lifecycle_action"] == "expire"
        assert "正文" not in json.dumps(event.summary, ensure_ascii=False)
    logs = await admin.get("/api/v1/admin/audit-logs", params={"action": "project.expire"})
    assert logs.status_code == 200 and logs.json()["items"][0]["actor_user_id"] is None


async def test_user_delete_is_blocked_for_active_trashed_and_expired_projects(app, admin, client):
    owner = await register(client)
    project = await create_project(client)
    user_url = f"/api/v1/admin/users/{owner['id']}"
    for phase in ("active", "trashed", "expired"):
        if phase == "trashed":
            assert (await client.delete(f"{PROJECTS}/{project['id']}")).status_code == 204
        if phase == "expired":
            async with app.state.sessionmaker() as db:
                (await db.get(Project, project["id"])).deleted_at = project_lifecycle.now_utc() - timedelta(days=31)
                await db.commit()
        response = await admin.delete(user_url)
        assert response.status_code == 409, response.text
        assert response.json()["project_count"] == 1 and response.json()["code"] == "user_has_projects"
    assert await recycle_bin_service.purge_expired(app.state.task_engine) == 1
    assert (await admin.delete(user_url)).status_code == 204


async def test_business_and_lifecycle_audit_are_atomic(app, admin, client, monkeypatch):
    await register(client)
    project = await create_project(client)
    pid = project["id"]
    original = AuditOperation._event
    fail_action = "project.delete"

    def event(operation, result, **kwargs):
        if result == "success" and operation.values["action"] == fail_action:
            raise RuntimeError("测试审计写入失败")
        return original(operation, result, **kwargs)

    monkeypatch.setattr(AuditOperation, "_event", event)
    assert (await admin.delete(f"{PROJECTS}/{pid}")).status_code == 500
    assert (await client.get(f"{PROJECTS}/{pid}")).status_code == 200
    assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
    row = await item(admin, pid)
    for action, suffix in (("project.restore", "restore"), ("project.purge", "purge")):
        fail_action = action
        payload = {"deleted_at": row["deleted_at"], "confirm": True}
        if suffix == "purge":
            payload["title"] = row["title"]
        assert (await admin.post(f"{RECYCLE}/{pid}/{suffix}", json=payload)).status_code == 500
        assert (await item(admin, pid))["deleted_at"] == row["deleted_at"]
    async with app.state.sessionmaker() as db:
        events = list(await db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))
        assert len(events) == 3 and all(e.result == "failure" for e in events)
        assert all(e.summary["lifecycle_action"] is None for e in events)


async def test_cleanup_failure_does_not_starve_other_projects(app, client, monkeypatch):
    await register(client)
    projects = [await create_project(client, title=title) for title in ("失败项", "可清理项")]
    for project in projects:
        assert (await client.delete(f"{PROJECTS}/{project['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        for project in await db.scalars(select(Project)):
            project.deleted_at = project_lifecycle.now_utc() - timedelta(days=31)
        await db.commit()
    original = recycle_bin_service.purge

    async def fail_one(db, engine, actor_id, project_id, *args, **kwargs):
        if project_id == projects[0]["id"]:
            raise RuntimeError("临时故障")
        return await original(db, engine, actor_id, project_id, *args, **kwargs)

    monkeypatch.setattr(recycle_bin_service, "purge", fail_one)
    assert await recycle_bin_service.purge_expired(app.state.task_engine) == 1
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, projects[0]["id"]) is not None
        assert await db.get(Project, projects[1]["id"]) is None
    monkeypatch.setattr(recycle_bin_service, "purge", original)
    assert await recycle_bin_service.purge_expired(app.state.task_engine) == 1


async def test_cleaner_runs_without_page_access_and_retries_then_stops(app, monkeypatch):
    called = asyncio.Event()
    count = 0

    async def pass_once(engine):
        nonlocal count
        count += 1
        if count == 1:
            raise RuntimeError("启动时临时故障")
        called.set()
        return 0

    monkeypatch.setattr(recycle_bin_service, "purge_expired", pass_once)
    cleaner = recycle_bin_service.RecycleBinCleaner(app.state.task_engine, interval=0.01)
    await cleaner.start()
    await asyncio.wait_for(called.wait(), 2)
    await cleaner.stop()
    assert count >= 2 and cleaner._task is None


async def test_restore_and_purge_race_only_one_can_win(app, admin, client):
    await register(client)
    project = await create_project(client)
    assert (await client.delete(f"{PROJECTS}/{project['id']}")).status_code == 204
    row = await item(admin, project["id"])
    body = {"deleted_at": row["deleted_at"], "confirm": True}
    restored, purged = await asyncio.gather(
        admin.post(f"{RECYCLE}/{project['id']}/restore", json=body),
        admin.post(f"{RECYCLE}/{project['id']}/purge", json={**body, "title": row["title"]}),
    )
    assert sorted([restored.status_code, purged.status_code]) in ([204, 409], [204, 404])
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        if restored.status_code == 204:
            assert project_row is not None and project_row.deleted_at is None
        else:
            assert project_row is None


async def test_backup_never_imports_lifecycle_or_coordinator_state(app, client):
    await register(client)
    project = await create_project(client)
    backup = await _backup(client, project["id"])
    fields = set(project_lifecycle.LIFECYCLE_FIELDS)
    assert not fields.intersection(backup["project"])
    backup["project"].update(
        deleted_at="2026-01-01T00:00:00Z",
        last_deleted_at="2026-01-01T00:00:00Z",
        deleted_by_user_id=999,
        deleted_by_username="伪造",
        deletion_started_at="2026-01-01T00:00:00Z",
        deletion_token="伪造",
    )
    imported = await _import(client, backup)
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, imported["project"]["id"])
        assert all(getattr(row, name) is None for name in fields)


async def test_deletion_generation_is_monotonic_through_frozen_clock_backwards_clock_and_restart(
    app, admin, client, monkeypatch
):
    await register(client)
    fixed = datetime(2026, 10, 31, 12, 0, 0, 123456, tzinfo=UTC)
    clock = fixed
    monkeypatch.setattr(project_lifecycle, "now_utc", lambda: clock)
    project = await create_project(client)
    pid = project["id"]
    old_rows = []
    for index in range(3):
        if index == 2:
            await app.state.task_engine.stop()
            await recycle_bin_service.recover_pending_deletions(app.state.sessionmaker)
            await app.state.task_engine.start()
            clock = fixed - timedelta(days=2)
        assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
        current = await item(admin, pid)
        stamp = datetime.fromisoformat(current["deleted_at"].replace("Z", "+00:00"))
        assert stamp == fixed + timedelta(microseconds=index)
        for old in old_rows:
            assert (
                await admin.post(
                    f"{RECYCLE}/{pid}/purge",
                    json={
                        "deleted_at": old["deleted_at"],
                        "title": old["title"],
                        "confirm": True,
                    },
                )
            ).status_code == 409
        assert (
            await admin.post(
                f"{RECYCLE}/{pid}/restore",
                json={
                    "deleted_at": current["deleted_at"],
                    "confirm": True,
                },
            )
        ).status_code == 204
        async with app.state.sessionmaker() as db:
            row = await db.get(Project, pid)
            assert row.deleted_at is None and project_lifecycle.as_utc(row.last_deleted_at) == stamp
        old_rows.append(current)


async def test_old_purge_never_matches_a_new_same_named_project_after_highest_id_purge(app, admin, client, monkeypatch):
    await register(client)
    fixed = datetime(2026, 10, 31, 12, 0, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr(project_lifecycle, "now_utc", lambda: fixed)
    first = await create_project(client, title="同名作品")
    assert (await client.delete(f"{PROJECTS}/{first['id']}")).status_code == 204
    row = await item(admin, first["id"])
    payload = {"deleted_at": row["deleted_at"], "title": row["title"], "confirm": True}
    assert (await admin.post(f"{RECYCLE}/{first['id']}/purge", json=payload)).status_code == 204
    second = await create_project(client, title="同名作品")
    assert second["id"] > first["id"]
    assert (await client.delete(f"{PROJECTS}/{second['id']}")).status_code == 204
    assert (await admin.post(f"{RECYCLE}/{first['id']}/purge", json=payload)).status_code == 404
    assert (await item(admin, second["id"]))["id"] == second["id"]
