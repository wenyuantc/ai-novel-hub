"""共享档案跨越书内 API、开放读取及单书快照时仍保持作品边界。"""

import asyncio
import sqlite3
import threading
import time
from contextlib import closing

import pytest
from sqlalchemy import func, select

from app.ai.providers import OpenAICompatProvider
from app.models import SeriesBook, SeriesEntryUse
from app.services.ai_dispatch import VisibilityChangedError, guard_provider
from app.services.backups.database import snapshot_database
from app.services.setting_ai_context import build_scope
from tests.helpers import create_project, register
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_open_api import OPEN, new_key
from tests.test_project_transfer import _backup, _import

BASE = "/api/v1"


async def _post(client, path, body):
    response = await client.post(path, json=body)
    assert response.is_success, response.text
    return response.json()


async def _join_books(client, *books):
    series = await _post(client, f"{BASE}/series", {"name": "只供作者浏览的系列"})
    route = f"{BASE}/series/{series['id']}"
    for book in books:
        series = await _post(
            client, route + "/books", {"project_id": book["id"], "expected_revision": series["revision"]}
        )
    return route, series


async def test_series_backup_materializes_only_the_book_and_remaps_cast(
    client, app, recycle_admin, make_client, tmp_path,
):
    await register(client)
    a = await create_project(client, title="甲书")
    b = await create_project(client, title="乙书")
    route, series = await _join_books(client, a, b)
    series_brief = {"id": series["id"], "name": series["name"], "revision": series["revision"]}
    project = (await client.get(f"{BASE}/projects/{b['id']}")).json()
    assert project["series"] == series_brief
    assert {p["id"]: p["series"] for p in (await client.get(f"{BASE}/projects")).json()} == {
        a["id"]: series_brief,
        b["id"]: series_brief,
    }

    shared = await _post(client, route + "/entries", {
        "kind": "character",
        "data": {"name": "林青", "profile": "共同基础"},
        "expected_revision": series["revision"],
    })
    unused = await _post(client, route + "/entries", {
        "kind": "character",
        "data": {"name": "尚未采用的绝密角色"},
        "expected_revision": shared["revision"],
    })
    entry_id = shared["item"]["id"]
    picked = await _post(client, f"{route}/entries/{entry_id}/uses", {
        "project_id": b["id"],
        "expected_revision": unused["revision"],
    })
    local_id = picked["ref"]["id"]
    place = await _post(client, route + "/entries", {
        "kind": "world",
        "data": {"title": "白塔", "category": "place", "content": "观测站旧址"},
        "expected_revision": picked["revision"],
    })
    picked_place = await _post(client, f"{route}/entries/{place['item']['id']}/uses", {
        "project_id": b["id"], "expected_revision": place["revision"],
    })
    a_base, b_base = (f"{BASE}/projects/{p['id']}" for p in (a, b))
    await _post(client, a_base + "/characters", {"name": "甲书专属角色"})
    await _post(client, a_base + "/chapters", {"title": "甲书私密章", "content": "林青的秘密在甲书。"})
    chapter = await _post(client, b_base + "/chapters", {"title": "乙书初章", "content": "林青来到白塔。"})
    cast = await client.put(f"{b_base}/chapters/{chapter['id']}/cast", json={"character_ids": [local_id]})
    assert cast.is_success, cast.text
    mentions = await client.get(b_base + "/mentions", params={
        "entity_kind": "character", "entity_id": local_id, "source_type": "chapter",
    })
    assert mentions.is_success, mentions.text
    assert mentions.json()["total"] == 1
    assert "甲书私密章" not in mentions.text

    public = (await client.get(b_base + "/characters")).json()
    assert [(item["id"], item["origin"]) for item in public] == [
        (local_id, {"scope": "series", "series_id": series["id"], "series_entry_id": entry_id}),
    ]
    assert (await recycle_admin.get(b_base)).json()["series"] is None
    admin_settings = (await recycle_admin.get(b_base + "/characters")).json()
    assert [item["origin"] for item in admin_settings] == [
        {"scope": "series", "series_id": None, "series_entry_id": None},
    ]
    assert (await recycle_admin.get(route)).status_code == 404
    outsider = make_client()
    await register(outsider, "outsider")
    assert (await outsider.get(route)).status_code == 404
    assert (await outsider.get(b_base + "/characters")).status_code == 404
    key = await new_key(client, [b["id"]])
    response = await client.get(f"{OPEN}/novels/{b['id']}/characters", headers=key)
    assert response.is_success, response.text
    assert [(item["id"], item["name"]) for item in response.json()["items"]] == [(local_id, "林青")]
    assert "origin" not in response.json()["items"][0]
    assert (await client.get(f"{OPEN}/novels/{a['id']}/characters", headers=key)).status_code == 404

    file = await _backup(client, b["id"])
    assert file["version"] == 9
    assert file["app_schema"] == "0041"
    assert not {"series_books", "series_entry_uses", "series_entries"} & set(file["tables"])
    assert [(r["id"], r["name"]) for r in file["tables"]["characters"]] == [(local_id, "林青")]
    assert [(r["id"], r["title"]) for r in file["tables"]["world_settings"]] == [
        (picked_place["ref"]["id"], "白塔"),
    ]
    # 即使文件冒充运行时绑定，也不会把导入作品接入既有系列。
    file["tables"]["series_books"] = [{"project_id": b["id"], "series_id": series["id"]}]
    file["tables"]["series_entry_uses"] = [{"project_id": b["id"], "series_entry_id": entry_id}]
    copy = (await _import(client, file, title="乙书独立恢复"))["project"]
    assert copy["series"] is None
    copy_base = f"{BASE}/projects/{copy['id']}"
    copy_settings = (await client.get(copy_base + "/characters")).json()
    assert [(item["name"], item["profile"], item["origin"]["scope"]) for item in copy_settings] == [
        ("林青", "共同基础", "book"),
    ]
    assert copy_settings[0]["id"] != local_id
    copy_world = (await client.get(copy_base + "/world")).json()
    assert [(row["title"], row["content"], row["origin"]["scope"]) for row in copy_world] == [
        ("白塔", "观测站旧址", "book"),
    ]
    copy_chapter = (await client.get(copy_base + "/chapters")).json()[0]
    restored = await client.get(f"{copy_base}/chapters/{copy_chapter['id']}/cast")
    assert restored.json() == {"character_ids": [copy_settings[0]["id"]]}
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesBook)) == 2
        assert await db.scalar(select(func.count()).select_from(SeriesEntryUse)) == 2

    # 整库快照是恢复实例的入口，不等同于刻意去掉绑定的单书备份。
    snapshot = tmp_path / "restored.sqlite3"
    await asyncio.to_thread(
        snapshot_database, app.state.settings.database_url, snapshot,
        deadline=time.monotonic() + 5, stop_event=threading.Event(),
    )
    with closing(sqlite3.connect(snapshot)) as restored_db:
        assert restored_db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert restored_db.execute(
            "SELECT project_id, series_entry_id, character_id FROM series_entry_uses WHERE character_id IS NOT NULL"
        ).fetchall() == [(b["id"], entry_id, local_id)]


async def test_series_restriction_rejects_stale_real_dispatch_in_every_adopting_book(client, app):
    await register(client)
    books = [await create_project(client, title=title) for title in ("前传", "正传")]
    route, series = await _join_books(client, *books)
    mutation = await _post(client, route + "/entries", {
        "kind": "character",
        "data": {"name": "林青", "profile": "只在旧上下文可见"},
        "expected_revision": series["revision"],
    })
    entry = f"{route}/entries/{mutation['item']['id']}"
    refs = []
    for book in books:
        mutation = await _post(client, entry + "/uses", {
            "project_id": book["id"], "expected_revision": mutation["revision"],
        })
        refs.append(("character", mutation["ref"]["id"]))

    maker = app.state.sessionmaker
    async with maker() as db:
        scopes = [
            await build_scope(db, book["id"], explicit=[ref]) for book, ref in zip(books, refs, strict=True)
        ]
        assert all(scope.includes(*ref) for scope, ref in zip(scopes, refs, strict=True))

    result = await client.patch(entry, json={
        "expected_revision": mutation["revision"],
        "ai_settings": {"ai_mode": "never"},
    })
    assert result.is_success, result.text
    async with maker() as db:
        for book, ref, old_scope in zip(books, refs, scopes, strict=True):
            scope = await build_scope(db, book["id"], explicit=[ref])
            assert not scope.includes(*ref)
            assert scope.stamp.epoch > old_scope.stamp.epoch

    async with _server(payload=_sse("openai")) as (url, _, headers, body, requests):
        headers.set()
        body.set()

        async def send(scope):
            provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), scope.stamp, maker)
            with pytest.raises(VisibilityChangedError):
                await _collect(provider.generate_stream([{"role": "user", "content": "只在旧上下文可见"}], "test"))

        await asyncio.gather(*(send(scope) for scope in scopes))
        assert requests == []
