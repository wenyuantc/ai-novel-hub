"""真实 PostgreSQL 两个事务的累计入账、重复终态，以及撤回后精确目标的可见性。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models import Task
from app.models.ai_usage import NANOS_PER_UNIT
from app.services import ai_usage_storage as storage
from app.services import task_statistics_service
from tests.helpers import create_project, register
from tests.schema_helpers import isolated_worker_database

INBOX = "/api/v1/notifications"


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch, required=True) as overrides:
        yield overrides


async def test_two_commits_jointly_cross_budget(app, client):
    owner = await register(client)
    project = await create_project(client)
    for scope in ("", f"/projects/{project['id']}"):
        path = f"/api/v1{scope}/ai-usage/budgets/CNY"
        response = await client.put(path, json={"limit": "100", "threshold_percent": 80})
        assert response.status_code == 200, response.text

    calls = [str(uuid4()), str(uuid4())]
    now = datetime.now(UTC)
    identity = storage.UsageCallIdentity(
        actor_id=owner["id"], payer_id=owner["id"], project_id=project["id"],
        provider="openai_compat", model="text", model_type="text", kind="chapter.generate",
    )
    async with app.state.sessionmaker.begin() as db:
        for call_id in calls:
            await storage.reserve_call(
                db, identity, call_id=call_id, started_at=now,
                price=storage.UsagePriceSnapshot(currency="CNY", input_price_nano=40 * NANOS_PER_UNIT,
                                                output_price_nano=0),
            )

    barrier = asyncio.Barrier(2)

    async def finish(call_id):
        async with app.state.sessionmaker.begin() as db:
            await barrier.wait()
            return await storage.finish_call(
                db, call_id, status="succeeded", sent=True, finished_at=now,
                measurement=storage.UsageMeasurement(input_tokens=1_000_000, output_tokens=0,
                                                     input_source="provider", output_source="provider"),
            )

    assert await asyncio.wait_for(asyncio.gather(*map(finish, calls)), timeout=20) == [True, True]
    assert await asyncio.wait_for(asyncio.gather(*map(finish, calls)), timeout=20) == [False, False]
    items = (await client.get(INBOX)).json()["items"]
    assert [item["kind"] for item in items] == ["budget", "budget"]
    scopes = []
    for item in items:
        target = (await client.post(f"{INBOX}/{item['id']}/open")).json()
        assert target["currency"] == "CNY"
        scopes.append(target.get("project_id"))
    assert set(scopes) == {None, project["id"]}


async def test_duplicate_terminal_transaction(app, client):
    owner = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker.begin() as db:
        task = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.generate", status="succeeded")
        db.add(task)
        await db.flush()
        await task_statistics_service.record_created(db, task)
        tid = task.id
        key = task.statistics_key

    barrier = asyncio.Barrier(2)

    async def finish():
        async with app.state.sessionmaker.begin() as db:
            row = await db.get(Task, tid)
            await barrier.wait()
            await task_statistics_service.record_state_change(db, row, "succeeded")

    await asyncio.wait_for(asyncio.gather(finish(), finish()), timeout=20)
    item, = (await client.get(INBOX)).json()["items"]
    opened = await client.post(f"{INBOX}/{item['id']}/open")
    assert opened.json() == {"kind": "task", "task_id": tid, "task_key": key}


async def test_revoke_racing_with_open(client, make_client):
    await register(client)
    guest_client = make_client()
    guest = await register(guest_client, "pg-notify-guest")
    project = await create_project(client)
    members = f"/api/v1/projects/{project['id']}/collaborators"
    invite = await client.post(members, json={"username": "pg-notify-guest", "role": "editor"})
    assert invite.status_code == 201, invite.text
    item, = (await guest_client.get(INBOX)).json()["items"]
    target = f"{INBOX}/{item['id']}/open"
    revoked, opened = await asyncio.wait_for(asyncio.gather(
        client.delete(f"{members}/{guest['id']}", params={"expected_revision": invite.json()["revision"]}),
        guest_client.post(target),
    ), timeout=20)
    assert revoked.status_code == 204
    assert opened.status_code in {200, 404}
    assert (await guest_client.post(target)).status_code == 404
    assert (await guest_client.get(INBOX)).json()["items"] == []
    assert (await guest_client.get(f"{INBOX}/unread-count")).json() == {"count": 0}
