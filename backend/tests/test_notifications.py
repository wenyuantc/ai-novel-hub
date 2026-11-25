"""从真实邀请入口验证个人收件、身份隔离、分页和事务语义。"""

import asyncio

import pytest
from alembic import command
from sqlalchemy import func, select

from app.models import Notification, NotificationPreference, Project, ProjectCollaborator, User
from app.services import notification_service
from tests.helpers import create_project, register
from tests.test_task_worker_migration import migration_db as migration_db

BASE = "/api/v1/notifications"


async def _invite(client, url):
    body = {"username": "inbox-guest"}
    if "/projects/" in url:
        body["role"] = "editor"
    result = await client.post(url + "/collaborators", json=body)
    assert result.status_code == 201, result.text
    return result.json()


async def _page(client, **params):
    result = await client.get(BASE, params=params)
    assert result.status_code == 200, result.text
    return result.json()


async def _count(client):
    result = await client.get(BASE + "/unread-count")
    assert result.status_code == 200, result.text
    return result.json()["count"]


async def test_project_series_invites_paging_read_and_reinvite(client, make_client):
    await register(client, "inbox-owner")
    guest_client = make_client()
    guest = await register(guest_client, "inbox-guest")
    stranger = make_client()
    await register(stranger, "inbox-stranger")
    project = await create_project(client)
    project_url = f"/api/v1/projects/{project['id']}"
    first = await _invite(client, project_url)
    series = (await client.post("/api/v1/series", json={"name": "系列"})).json()
    second = await _invite(client, f"/api/v1/series/{series['id']}")
    assert await _count(guest_client) == 2
    assert await _count(client) == await _count(stranger) == 0
    newest = await _page(guest_client, limit=1)
    (series_item,) = newest["items"]
    assert series_item["title"] == "收到系列协作邀请"
    assert newest["next_before"]
    oldest = await _page(guest_client, limit=1, before=newest["next_before"])
    (project_item,) = oldest["items"]
    assert oldest["next_before"] is None and project_item["id"] != series_item["id"]
    for action in ("open", "read"):
        assert (await stranger.post(f"{BASE}/{project_item['id']}/{action}")).status_code == 404
    for item, scope, invitation in ((series_item, "series", second), (project_item, "project", first)):
        result = await guest_client.post(f"{BASE}/{item['id']}/open")
        assert result.json() == {"kind": "invitation", "scope": scope, "invitation_id": invitation["invitation_id"]}
        assert (await guest_client.post(f"{BASE}/{item['id']}/read")).status_code == 204
    assert await _count(guest_client) == 0
    assert all(row["read_at"] for row in (await _page(guest_client))["items"])
    revoke = await client.delete(f"{project_url}/collaborators/{guest['id']}", params={"expected_revision": 1})
    assert revoke.status_code == 204
    again = await _invite(client, project_url)
    assert again["invitation_id"] != first["invitation_id"]
    assert (await guest_client.post(f"{BASE}/{project_item['id']}/open")).status_code == 404
    fresh, old = (await _page(guest_client))["items"]
    assert fresh["read_at"] is None and old["id"] == series_item["id"]
    assert await _count(guest_client) == 1


async def test_preference_suppression_survives_replay_and_invite_rolls_back(app, client, make_client, monkeypatch):
    await register(client, "inbox-owner")
    guest_client = make_client()
    guest = await register(guest_client, "inbox-guest")
    project = await create_project(client)
    project_url = f"/api/v1/projects/{project['id']}"
    prefs_url = BASE + "/preferences"
    assert (await guest_client.get(prefs_url)).json() == dict.fromkeys(
        ("invitation", "task", "budget", "consistency"), True
    )
    changed = (await guest_client.put(prefs_url + "/invitation", json={"enabled": False})).json()
    assert changed == dict(invitation=False, task=True, budget=True, consistency=True)
    invitation = await _invite(client, project_url)
    assert await _count(guest_client) == 0
    assert (await guest_client.get("/api/v1/collaboration/invitations")).json()[0]["invitation_id"] == invitation[
        "invitation_id"
    ]
    assert (await guest_client.put(prefs_url + "/invitation", json={"enabled": True})).status_code == 200
    async with app.state.sessionmaker.begin() as db:
        await notification_service.deliver(
            db,
            recipient_id=guest["id"],
            kind="invitation",
            event_key=f"invitation:{invitation['invitation_id']}",
            grant_id=invitation["invitation_id"],
            project_id=project["id"],
        )
    assert (await _page(guest_client))["items"] == []
    other = await create_project(client)
    original = notification_service.deliver

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("模拟业务回滚")

    monkeypatch.setattr(notification_service, "deliver", interrupted)
    response = await client.post(
        f"/api/v1/projects/{other['id']}/collaborators", json={"username": "inbox-guest", "role": "editor"}
    )
    assert response.status_code == 500
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Notification)) == 1
        assert await db.scalar(select(func.count()).select_from(ProjectCollaborator)) == 1


async def test_upgrade_with_old_invite_and_downgrade_guard(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0071")
    async with engine.begin() as conn:
        for user_id in (1, 2):
            await conn.execute(
                User.__table__.insert().values(id=user_id, username=f"old{user_id}", password_hash="test")
            )
        await conn.execute(Project.__table__.insert().values(id=1, user_id=1, title="旧作品"))
        await conn.execute(ProjectCollaborator.__table__.insert().values(project_id=1, user_id=2, role="viewer"))
    await asyncio.to_thread(command.upgrade, cfg, "0072")
    async with engine.begin() as conn:
        assert await conn.scalar(select(func.count()).select_from(Notification)) == 0
        await conn.execute(NotificationPreference.__table__.insert().values(user_id=2, kind="budget", enabled=False))
    with pytest.raises(RuntimeError, match="请先备份"):
        await asyncio.to_thread(command.downgrade, cfg, "0071")
    async with engine.begin() as conn:
        assert await conn.scalar(select(func.count()).select_from(NotificationPreference)) == 1
        await conn.execute(NotificationPreference.__table__.delete())
    await asyncio.to_thread(command.downgrade, cfg, "0071")


async def test_deleted_cursor_and_out_of_order_timestamps(app, client, make_client):
    from datetime import UTC, datetime

    await register(client, "inbox-owner")
    guest_client = make_client()
    await register(guest_client, "inbox-guest")
    for _ in range(3):
        project = await create_project(client)
        await _invite(client, f"/api/v1/projects/{project['id']}")
    # 最小 id 对应最新日期，模拟独立事务分配 id 与提交时间顺序不一致。
    async with app.state.sessionmaker.begin() as db:
        rows = (await db.scalars(select(Notification).order_by(Notification.id))).all()
        for day, row in zip((3, 1, 2), rows, strict=True):
            row.created_at = datetime(2026, 1, day, tzinfo=UTC)
        newest_db_id = rows[0].id
        newest_id, oldest_id, middle_id = [row.public_id for row in rows]
    page = await _page(guest_client, limit=1)
    assert page["items"][0]["id"] == newest_id
    async with app.state.sessionmaker.begin() as db:
        await db.delete(await db.get(Notification, newest_db_id))
    remaining = await _page(guest_client, before=page["next_before"])
    assert [item["id"] for item in remaining["items"]] == [middle_id, oldest_id]
    assert (await guest_client.get(BASE, params={"before": "bad"})).status_code == 400


async def test_restored_sequence_cannot_open_new_delivery_with_old_public_id(app, client, make_client):
    from sqlalchemy import delete, update

    await register(client, "inbox-owner")
    guest_client = make_client()
    await register(guest_client, "inbox-guest")
    first = await create_project(client)
    await _invite(client, f"/api/v1/projects/{first['id']}")
    (old,) = (await _page(guest_client))["items"]
    async with app.state.sessionmaker.begin() as db:
        sequence = await db.scalar(select(Notification.id))
        await db.execute(delete(Notification))
    second = await create_project(client)
    await _invite(client, f"/api/v1/projects/{second['id']}")
    async with app.state.sessionmaker.begin() as db:
        await db.execute(update(Notification).values(id=sequence))
    (fresh,) = (await _page(guest_client))["items"]
    assert fresh["id"] != old["id"]
    for action in ("read", "open"):
        assert (await guest_client.post(f"{BASE}/{old['id']}/{action}")).status_code == 404
    assert await _count(guest_client) == 1
