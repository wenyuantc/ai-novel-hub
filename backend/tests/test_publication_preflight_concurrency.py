"""独立连接和授权运行时，覆盖词表维护与定稿、登记在两种提交顺序下的行为。"""

import asyncio

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_sessionmaker
from app.core.deps import get_db
from tests.helpers import create_project, register
from tests.test_serialization_concurrency import (
    finalize_body,
    publish_body,
    successful,
)
from tests.test_serialization_concurrency import (
    settings_overrides as settings_overrides,
)


async def _ordered_requests(app, monkeypatch, first, second):
    # 两个请求各用独立的授权运行时，排除进程内互斥掩盖缺少数据库锁的情况。
    factories = iter((app.state.sessionmaker, create_sessionmaker(app.state.engine)))
    first_committing, second_sql, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def sessions():
        position = len(order)
        async with next(factories)() as session:
            order.append(session)
            if position == 1:
                connection = await session.connection()

                def on_execute(conn, cursor, statement, parameters, context, executemany):
                    if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "LOCK")):
                        second_sql.set()

                event.listen(connection.sync_connection, "before_cursor_execute", on_execute)
            yield session

    commit = AsyncSession.commit

    async def pause_commit(session):
        if order and session is order[0]:
            first_committing.set()
            await release.wait()
        return await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", pause_commit)
    app.dependency_overrides[get_db] = sessions
    try:
        async with asyncio.timeout(20), asyncio.TaskGroup() as group:
            a = group.create_task(first())
            await first_committing.wait()
            b = group.create_task(second())
            try:
                await second_sql.wait()
            finally:
                release.set()
        return a.result(), b.result()
    finally:
        app.dependency_overrides.pop(get_db)


@pytest.mark.parametrize("global_scope", [False, True], ids=["author", "site"])
@pytest.mark.parametrize("action", ["editions", "publications"])
@pytest.mark.parametrize("terms_first", [False, True], ids=["confirm-first", "change-first"])
async def test_term_change_racing_confirm(app, client, system_admin, monkeypatch, global_scope, action, terms_first):
    await register(client)
    pid = (await create_project(client))["id"]
    project = f"/api/v1/projects/{pid}"
    chapter = successful(await client.post(f"{project}/chapters", json={"title": "桥", "content": "灯影重重"}))
    path = f"{project}/serialization/chapters/{chapter['id']}"
    preview = successful(await client.get(path))
    body = finalize_body(preview)
    if action == "publications":
        successful(await client.post(f"{path}/editions", json=body))
        body = publish_body(successful(await client.get(path)))

    editor = system_admin if global_scope else client
    terms_path = "/api/v1/admin/publication-terms" if global_scope else "/api/v1/publication-terms"
    successful(await editor.put(terms_path, json={"terms": ["灯"], "expected_revision": 0}))
    check = await client.post(f"{path}/{action}", json=body)
    assert check.status_code == 409, check.text
    body["confirmation_token"] = check.json()["preflight"]["confirmation_token"]

    async def confirm():
        return await client.post(f"{path}/{action}", json=body)

    async def change():
        return await editor.put(terms_path, json={"terms": ["影"], "expected_revision": 1})

    first, second = (change, confirm) if terms_first else (confirm, change)
    left, right = await _ordered_requests(app, monkeypatch, first, second)
    confirmed, changed = (right, left) if terms_first else (left, right)
    assert successful(changed)["revision"] == 2
    if terms_first:
        assert confirmed.status_code == 409, confirmed.text
        assert confirmed.json()["code"] == "stale_serialization_preflight"
        assert [hit["term"] for hit in confirmed.json()["preflight"]["hits"]] == ["影"]
    else:
        successful(confirmed)
    if action == "publications":
        history = successful(await client.get(f"{project}/serialization/publications"))
        assert history["total"] == int(not terms_first)
        if not terms_first:
            assert successful(await confirm()) == confirmed.json()
    else:
        assert (successful(await client.get(path))["edition"] is None) == terms_first
