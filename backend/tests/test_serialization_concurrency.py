"""用独立连接和授权运行时验证连载写入跨事务的一致性。"""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.database import create_sessionmaker
from app.core.deps import get_db
from app.models import Base
from app.services import authorization_service
from tests.helpers import create_project, register
from tests.schema_helpers import create_isolated_schema


@pytest.fixture(params=["sqlite", "postgresql"])
async def settings_overrides(request, monkeypatch):
    """复用常规应用夹具，PostgreSQL 为每例单建 schema，连接池的所有连接均锁定该 schema。"""
    if request.param == "sqlite":
        yield {}
        return
    raw = os.getenv("AINOVEL_POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("缺少隔离 PostgreSQL 测试库 AINOVEL_POSTGRES_TEST_URL")
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.database != "ainovel_test" or url.query):
        raise ValueError("AINOVEL_POSTGRES_TEST_URL 必须指向本机专用 ainovel_test 测试库")
    schema = f"serialization_{uuid4().hex}"
    admin_engine = create_async_engine(url)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(CreateSchema(schema))
        try:
            def make_scoped_engine(database_url):
                assert make_url(database_url) == url
                return create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})

            monkeypatch.setattr("app.main.create_engine", make_scoped_engine)
            monkeypatch.setattr(Base.metadata, "create_all", create_isolated_schema)
            yield {"database_url": raw}
        finally:
            async with admin_engine.begin() as conn:
                await conn.execute(DropSchema(schema, cascade=True))
    finally:
        await admin_engine.dispose()


def successful(response):
    assert response.is_success, response.text
    return response.json()


def finalize_body(preview):
    return {
        "expected_revision": preview["chapter"]["revision"],
        "expected_content_revision": preview["draft"]["content_revision"],
        "expected_scene_id": preview["scene_id"],
        "expected_fingerprint": preview["draft"]["fingerprint"],
    }


def publish_body(preview, channel="起点", published_at="2025-03-02T00:03:00+08:00"):
    return {
        "expected_revision": preview["chapter"]["revision"],
        "edition_id": preview["edition"]["id"],
        "channel": channel, "published_at": published_at, "request_id": str(uuid4()),
    }


@pytest.fixture
async def manuscript(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = successful(await client.post(f"{base}/chapters", json={"title": "灯塔", "content": "原稿 A"}))
    workbench = f"{base}/serialization"
    entry = f"{workbench}/chapters/{chapter['id']}"
    preview = successful(await client.get(entry))
    successful(await client.post(f"{entry}/editions", json=finalize_body(preview)))
    ready = successful(await client.get(entry))
    assert ready["edition"]["content"] == "原稿 A"
    return workbench, entry, ready


async def together(app, first_request, second_request):
    """两个请求先各取一个真实连接，再同时进入认证和路由。各自使用独立进程级锁。"""
    factories = iter((app.state.sessionmaker, create_sessionmaker(app.state.engine)))
    barrier = asyncio.Barrier(2)

    async def separate_session():
        async with next(factories)() as session:
            await session.connection()
            async with asyncio.timeout(10):
                await barrier.wait()
            yield session

    app.dependency_overrides[get_db] = separate_session
    try:
        async with asyncio.timeout(20), asyncio.TaskGroup() as group:
            first = group.create_task(first_request)
            second = group.create_task(second_request)
        return first.result(), second.result()
    finally:
        del app.dependency_overrides[get_db]


async def test_same_publication_replayed_concurrently_returns_one_receipt(app, client, manuscript):
    workbench, entry, ready = manuscript
    url = f"{entry}/publications"
    request = publish_body(ready)
    left, right = await together(app, client.post(url, json=request), client.post(url, json=request))
    assert successful(left)["id"] == successful(right)["id"]
    conflict = await client.post(url, json={**request, "channel": "不同渠道"})
    assert conflict.status_code == 409, conflict.text
    page = successful(await client.get(f"{workbench}/publications"))
    assert page["total"] == 1
    assert page["items"][0]["channel"] == request["channel"]


async def test_simultaneous_first_platforms_and_earlier_backfill_count_one_chapter(app, client, manuscript):
    workbench, entry, ready = manuscript
    goal = successful(await client.put(f"{workbench}/goal", json={"expected_revision": 0, "daily_goal": 1}))
    assert goal["daily_goal"] == 1
    overview = successful(await client.get(f"{workbench}/overview", params={"month": "2025-03"}))
    assert (overview["stock_count"], overview["support_days"]) == (1, 1)

    url = f"{entry}/publications"
    march1 = publish_body(ready, "起点", "2025-02-28T16:01:00Z")
    march2 = publish_body(ready, "晋江", "2025-03-01T16:10:00Z")
    left, right = await together(app, client.post(url, json=march1), client.post(url, json=march2))
    first_id, late_id = successful(left)["id"], successful(right)["id"]
    march = successful(await client.get(f"{workbench}/overview", params={"month": "2025-03"}))
    assert (march["stock_count"], march["support_days"]) == (0, 0)
    assert [(d["date"], d["first_published_count"]) for d in march["days"] if d["first_published_count"]] == [
        ("2025-03-01", 1),
    ]

    # 补录更早的实际发布时间。首发从北京时间三月一日移动到二月二十八日。
    earlier = publish_body(ready, "番茄", "2025-02-28T15:59:00Z")
    early_id = successful(await client.post(url, json=earlier))["id"]
    assert len({first_id, early_id, late_id}) == 3
    history = successful(await client.get(f"{workbench}/publications"))
    assert history["total"] == 3
    assert {item["channel"] for item in history["items"]} == {"起点", "番茄", "晋江"}

    for month, expected in (("2025-02", {"2025-02-28": 1}), ("2025-03", {})):
        overview = successful(await client.get(f"{workbench}/overview", params={"month": month}))
        assert overview["stock_count"] == 0
        firsts = {d["date"]: d["first_published_count"] for d in overview["days"] if d["first_published_count"]}
        assert firsts == expected
    day = successful(await client.get(f"{workbench}/days/2025-02-28"))
    assert day["first_published"]["total"] == 1
    assert day["first_published"]["items"][0]["id"] == early_id


async def test_refinalize_racing_with_publish_never_substitutes_edition(app, client, manuscript):
    workbench, entry, ready = manuscript
    ordinary_chapter = entry.replace("/serialization", "")
    successful(await client.patch(ordinary_chapter, json={"content": "重新校对 B"}))
    revised = successful(await client.get(entry))
    assert revised["draft_changed"] is True
    stale_final = await client.post(f"{entry}/editions", json=finalize_body(ready))
    assert stale_final.status_code == 409, stale_final.text
    publication = publish_body(ready)
    frozen, marked = await together(
        app, client.post(f"{entry}/editions", json=finalize_body(revised)),
        client.post(f"{entry}/publications", json=publication),
    )
    next_edition = successful(frozen)["id"]
    assert next_edition != publication["edition_id"]
    assert marked.status_code in {200, 409}, marked.text
    after = successful(await client.get(entry))
    assert after["edition"]["id"] == next_edition
    assert after["edition"]["content"] == "重新校对 B"

    stale_publish = await client.post(f"{entry}/publications", json={**publication, "request_id": str(uuid4())})
    assert stale_publish.status_code == 409, stale_publish.text
    history = successful(await client.get(f"{workbench}/publications"))
    assert history["total"] == (1 if marked.is_success else 0)
    if marked.is_success:
        original = successful(await client.get(f"{workbench}/publications/{marked.json()['id']}"))
        assert original["edition"]["id"] == publication["edition_id"]
        assert original["edition"]["content"] == "原稿 A"
        assert successful(await client.post(f"{entry}/publications", json=publication)) == marked.json()
    else:
        # 失败的旧预览没有将新版记作旧版；重新预览后可以确实发布 B。
        marked = await client.post(f"{entry}/publications", json=publish_body(after))
        actual = successful(await client.get(f"{workbench}/publications/{successful(marked)['id']}"))
        assert actual["edition"]["content"] == "重新校对 B"


async def test_revoked_editor_cannot_replay_or_read_a_publication(client, make_client, manuscript, monkeypatch):
    workbench, entry, ready = manuscript
    guest = make_client()
    editor = await register(guest, "publication-editor")
    members = workbench.removesuffix("/serialization") + "/collaborators"
    invite = successful(await client.post(members, json={"username": editor["username"], "role": "editor"}))
    accept = f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept"
    accepted = successful(await guest.post(accept, json={"expected_revision": invite["revision"]}))
    body = publish_body(ready)
    receipt = successful(await guest.post(f"{entry}/publications", json=body))
    reached, release = asyncio.Event(), asyncio.Event()
    original_gate = authorization_service.gate

    async def pause_at_gate(db):
        if asyncio.current_task() is replay:
            reached.set()
            await release.wait()
        await original_gate(db)

    monkeypatch.setattr(authorization_service, "gate", pause_at_gate)
    async with asyncio.timeout(15), asyncio.TaskGroup() as group:
        replay = group.create_task(guest.post(f"{entry}/publications", json=body))
        try:
            await reached.wait()
            result = await client.delete(
                f"{members}/{editor['id']}", params={"expected_revision": accepted["revision"]},
            )
            assert result.status_code == 204, result.text
        finally:
            release.set()
    assert replay.result().status_code == 404, replay.result().text

    for response in (
        await guest.post(f"{entry}/publications", json=body),
        await guest.get(f"{workbench}/publications/{receipt['id']}"),
        await guest.get(f"{workbench}/publications/{receipt['id']}/export", params={"format": "txt"}),
    ):
        assert response.status_code == 404, response.text
    assert successful(await client.get(f"{workbench}/publications"))["total"] == 1
