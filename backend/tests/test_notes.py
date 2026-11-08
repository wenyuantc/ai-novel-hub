"""作品笔记：独立原文、修订冲突、权限隔离和备份兼容。"""

import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import update

from app.models import ProjectNote
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def _setup(client):
    await register(client)
    project = await create_project(client)
    return f"/api/v1/projects/{project['id']}"


async def test_note_crud_search_pagination_and_revision(client):
    base = await _setup(client)
    response = await client.post(f"{base}/notes", json={"title": "  线索  ", "content": "😀林远\n抵达青云山。"})
    assert response.status_code == 201, response.text
    note = response.json()
    assert note["title"] == "线索" and note["revision"] == 1
    assert note["content"] == "😀林远\n抵达青云山。"
    await client.post(f"{base}/notes", json={"title": "字面%_\\", "content": "其他想法"})
    page = (await client.get(f"{base}/notes", params={"page_size": 1})).json()
    assert page["total"] == 2 and page["page"] == 1 and len(page["items"]) == 1
    assert "content" not in page["items"][0] and "excerpt" in page["items"][0]
    found = (await client.get(f"{base}/notes", params={"q": "青云山"})).json()
    assert found["total"] == 1 and found["items"][0]["id"] == note["id"]
    assert (await client.get(f"{base}/notes", params={"q": "%_\\"})).json()["total"] == 1
    path = f"{base}/notes/{note['id']}"
    updated = await client.patch(path, json={"expected_revision": 1, "title": "新的线索"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2 and updated.json()["content"] == note["content"]
    conflict = await client.patch(path, json={"expected_revision": 1, "content": "过期草稿"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "stale_revision"
    assert (await client.get(path)).json()["content"] == note["content"]
    assert (await client.delete(path, params={"expected_revision": 1})).status_code == 409
    assert (await client.delete(path, params={"expected_revision": 2})).status_code == 204
    assert (await client.get(path)).status_code == 404
    replacement = (await client.post(f"{base}/notes", json={"title": "另一个笔记"})).json()
    assert replacement["id"] > note["id"]


async def test_note_validation_and_cross_project_isolation(client, make_client):
    base = await _setup(client)
    for body in ({"title": "  "}, {"title": "长" * 201}, {"title": "长文", "content": "文" * 200001}):
        assert (await client.post(f"{base}/notes", json=body)).status_code == 422
    note = (await client.post(f"{base}/notes", json={"title": "私有笔记", "content": ""})).json()
    path = f"{base}/notes/{note['id']}"
    assert (await client.patch(path, json={"content": "无版本"})).status_code == 422
    assert (await client.patch(path, json={"expected_revision": 1, "content": None})).status_code == 422
    assert (await client.delete(path)).status_code == 422
    assert (await client.get(f"{base}/notes", params={"page_size": 101})).status_code == 422
    second = await create_project(client, title="另一本")
    other_path = f"/api/v1/projects/{second['id']}/notes/{note['id']}"
    assert (await client.get(other_path)).status_code == 404
    assert (await client.patch(other_path, json={"expected_revision": 1, "content": "错书"})).status_code == 404
    bob = make_client()
    await register(bob, "bob")
    assert (await bob.get(f"{base}/notes")).status_code == 404
    assert (await bob.get(path)).status_code == 404
    assert (await bob.post(f"{base}/notes", json={"title": "越权"})).status_code == 404
    assert (await bob.delete(path, params={"expected_revision": 1})).status_code == 404


async def test_note_backup_roundtrip_and_old_backup(client):
    base = await _setup(client)
    note = (await client.post(f"{base}/notes", json={"title": "资料", "content": "😀青云山\r\n逐字保留"})).json()
    backup = (await client.get(f"{base}/backup")).json()
    assert backup["tables"]["project_notes"][0]["content"] == note["content"]
    restored = await client.post(
        "/api/v1/projects/import",
        files={"file": ("notes.json", json.dumps(backup, ensure_ascii=False).encode(), "application/json")},
    )
    assert restored.status_code == 201, restored.text
    imported_base = f"/api/v1/projects/{restored.json()['project']['id']}"
    imported = (await client.get(f"{imported_base}/notes")).json()["items"]
    assert len(imported) == 1 and imported[0]["title"] == "资料"
    full = (await client.get(f"{imported_base}/notes/{imported[0]['id']}")).json()
    assert full["content"] == note["content"] and full["project_id"] != note["project_id"]
    del backup["tables"]["project_notes"]
    backup["app_schema"] = "0031"
    old = await client.post(
        "/api/v1/projects/import",
        files={"file": ("old.json", json.dumps(backup).encode(), "application/json")},
    )
    assert old.status_code == 201, old.text
    old_base = f"/api/v1/projects/{old.json()['project']['id']}"
    assert (await client.get(f"{old_base}/notes")).json()["total"] == 0


async def test_note_concurrent_saves_do_not_overwrite_each_other(client):
    base = await _setup(client)
    note = (await client.post(f"{base}/notes", json={"title": "并发", "content": "原文"})).json()
    path = f"{base}/notes/{note['id']}"
    responses = await asyncio.gather(
        client.patch(path, json={"expected_revision": 1, "content": "版本甲"}),
        client.patch(path, json={"expected_revision": 1, "content": "版本乙"}),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    current = (await client.get(path)).json()
    assert current["revision"] == 2 and current["content"] == winner["content"]


async def test_note_management_capabilities_and_recycle_lifecycle(client, system_admin, make_client, fake_llm):
    base = await _setup(client)
    note = (await client.post(f"{base}/notes", json={"title": "作者资料", "content": "最初"})).json()
    path = f"{base}/notes/{note['id']}"
    reader = make_client()
    user = await register(reader, "reader")
    read_role = await create_role(system_admin, "只读作品笔记", ["projects.read"])
    await assign_roles(system_admin, user["id"], [read_role["id"]])
    assert (await reader.get(path)).status_code == 200
    assert (await reader.get(f"{base}/notes")).json()["total"] == 1
    assert (await reader.post(f"{base}/notes", json={"title": "不能新建"})).status_code == 403
    assert (await reader.patch(path, json={"expected_revision": 1, "content": "不能写"})).status_code == 403
    assert (await reader.delete(path, params={"expected_revision": 1})).status_code == 403
    edit_role = await create_role(system_admin, "可维护作品笔记", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, user["id"], [edit_role["id"]])
    updated = await reader.patch(path, json={"expected_revision": 1, "content": "人工整理"})
    assert updated.status_code == 200, updated.text
    assert (await client.get(path)).json()["content"] == "人工整理"
    await assign_roles(system_admin, user["id"], [])
    assert (await reader.get(path)).status_code == 404
    assert (await client.delete(base)).status_code == 204
    for account in (client, system_admin):
        assert (await account.get(path)).status_code == 404
        assert (await account.get(f"{base}/notes")).status_code == 404
        assert (await account.post(f"{base}/notes", json={"title": "已删除项目"})).status_code == 404
    assert not fake_llm.calls and not fake_llm.embed_calls


async def test_note_flags_filter_sort_and_keep_revision(client, app):
    base = await _setup(client)
    old = (await client.post(f"{base}/notes", json={"title": "旧线索", "content": "青云山伏笔"})).json()
    mid = (await client.post(f"{base}/notes", json={"title": "中间", "content": "青云山地图"})).json()
    new = (await client.post(f"{base}/notes", json={"title": "最新", "content": "无关"})).json()
    assert old["is_favorite"] is False and old["is_pinned"] is False
    # 明确的更新时间顺序：old < mid < new，避免同秒时间戳导致排序不确定。
    async with app.state.sessionmaker() as db:
        for offset, note in enumerate((old, mid, new)):
            await db.execute(update(ProjectNote).where(ProjectNote.id == note["id"])
                             .values(updated_at=datetime(2026, 1, offset + 1, tzinfo=UTC)))
        await db.commit()

    def ids(page):
        return [item["id"] for item in page["items"]]

    listing = (await client.get(f"{base}/notes")).json()
    assert ids(listing) == [new["id"], mid["id"], old["id"]]
    before = (await client.get(f"{base}/notes/{old['id']}")).json()

    pinned = await client.put(f"{base}/notes/{old['id']}/flags", json={"is_pinned": True})
    assert pinned.status_code == 200, pinned.text
    body = pinned.json()
    assert body["is_pinned"] is True and body["is_favorite"] is False
    assert body["revision"] == before["revision"] and body["updated_at"] == before["updated_at"]
    listing = (await client.get(f"{base}/notes")).json()
    assert ids(listing) == [old["id"], new["id"], mid["id"]]
    assert listing["items"][0]["is_pinned"] is True
    only_pinned = (await client.get(f"{base}/notes", params={"pinned": "true"})).json()
    assert ids(only_pinned) == [old["id"]] and only_pinned["total"] == 1
    assert (await client.get(f"{base}/notes", params={"pinned": "false"})).json()["total"] == 3

    favorite = await client.put(f"{base}/notes/{mid['id']}/flags", json={"is_favorite": True})
    assert favorite.status_code == 200 and favorite.json()["is_favorite"] is True
    await client.put(f"{base}/notes/{new['id']}/flags", json={"is_favorite": True, "is_pinned": False})
    favorites = (await client.get(f"{base}/notes", params={"favorite": "true"})).json()
    assert ids(favorites) == [new["id"], mid["id"]] and favorites["total"] == 2
    combined = (await client.get(f"{base}/notes", params={"favorite": "true", "q": "青云山"})).json()
    assert ids(combined) == [mid["id"]] and combined["total"] == 1
    assert (await client.get(f"{base}/notes", params={"favorite": "true", "pinned": "true"})).json()["total"] == 0

    # 标记不进入修订：基于标记前 revision 的草稿仍可保存。
    saved = await client.patch(f"{base}/notes/{old['id']}",
                               json={"expected_revision": before["revision"], "content": "草稿保存"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == before["revision"] + 1 and saved.json()["is_pinned"] is True

    unpinned = (await client.put(f"{base}/notes/{old['id']}/flags", json={"is_pinned": False})).json()
    assert unpinned["is_pinned"] is False and unpinned["updated_at"] == saved.json()["updated_at"]
    again = (await client.put(f"{base}/notes/{old['id']}/flags", json={"is_pinned": False})).json()
    assert again["revision"] == unpinned["revision"]
    listing = (await client.get(f"{base}/notes")).json()
    assert ids(listing)[0] == old["id"]  # PATCH 后 old 是最新更新的笔记


async def test_note_flags_validation_permissions_and_isolation(client, system_admin, make_client):
    base = await _setup(client)
    note = (await client.post(f"{base}/notes", json={"title": "标记"})).json()
    path = f"{base}/notes/{note['id']}/flags"
    for body in ({}, {"is_pinned": None}, {"is_favorite": "true"}, {"is_pinned": 1},
                 {"is_pinned": True, "revision": 2}, {"is_favorite": True, "title": "改名"}):
        assert (await client.put(path, json=body)).status_code == 422, body
    patched = await client.patch(f"{base}/notes/{note['id']}", json={"expected_revision": 1, "is_pinned": True})
    assert patched.status_code == 422
    assert (await client.get(f"{base}/notes", params={"favorite": "maybe"})).status_code == 422
    assert (await client.put(f"{base}/notes/999999/flags", json={"is_pinned": True})).status_code == 404
    second = await create_project(client, title="另一本")
    other = f"/api/v1/projects/{second['id']}/notes/{note['id']}/flags"
    assert (await client.put(other, json={"is_pinned": True})).status_code == 404
    bob = make_client()
    await register(bob, "bob")
    assert (await bob.put(path, json={"is_pinned": True})).status_code == 404
    reader = make_client()
    user = await register(reader, "reader")
    role = await create_role(system_admin, "只读笔记标记", ["projects.read"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    assert (await reader.get(f"{base}/notes")).json()["items"][0]["is_pinned"] is False
    assert (await reader.put(path, json={"is_pinned": True})).status_code == 403
    assert (await client.get(f"{base}/notes/{note['id']}")).json()["is_pinned"] is False
    edit_role = await create_role(system_admin, "可标记笔记", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, user["id"], [edit_role["id"]])
    assert (await reader.put(path, json={"is_favorite": True})).json()["is_favorite"] is True


async def _import_raw(client, backup):
    return await client.post(
        "/api/v1/projects/import",
        files={"file": ("notes.json", json.dumps(backup, ensure_ascii=False).encode(), "application/json")},
    )


async def test_note_flags_backup_v9_roundtrip_v8_defaults_and_rejections(client):
    base = await _setup(client)
    pinned = (await client.post(f"{base}/notes", json={"title": "固定", "content": "甲"})).json()
    favorite = (await client.post(f"{base}/notes", json={"title": "收藏", "content": "乙"})).json()
    await client.post(f"{base}/notes", json={"title": "普通", "content": "丙"})
    await client.put(f"{base}/notes/{pinned['id']}/flags", json={"is_pinned": True})
    await client.put(f"{base}/notes/{favorite['id']}/flags", json={"is_favorite": True})
    backup = (await client.get(f"{base}/backup")).json()
    assert (backup["version"], backup["app_schema"]) == (9, "0041")
    flags = {row["title"]: (row["is_favorite"], row["is_pinned"]) for row in backup["tables"]["project_notes"]}
    assert flags == {"固定": (False, True), "收藏": (True, False), "普通": (False, False)}

    restored = await _import_raw(client, backup)
    assert restored.status_code == 201, restored.text
    again = (await client.get(f"/api/v1/projects/{restored.json()['project']['id']}/backup")).json()
    assert {row["title"]: (row["is_favorite"], row["is_pinned"])
            for row in again["tables"]["project_notes"]} == flags

    for value in ("true", 1, None):
        bad = json.loads(json.dumps(backup))
        bad["tables"]["project_notes"][0]["is_pinned"] = value
        preview = await client.post("/api/v1/projects/import/preview", files={
            "file": ("bad.json", json.dumps(bad).encode(), "application/json")})
        assert preview.status_code == 400 and "is_pinned" in json.dumps(preview.json(), ensure_ascii=False), value

    v8 = json.loads(json.dumps(backup))
    v8.update(version=8, app_schema="0040")
    for row in v8["tables"]["project_notes"]:
        del row["is_favorite"], row["is_pinned"]
    old = await _import_raw(client, v8)
    assert old.status_code == 201, old.text
    old_notes = (await client.get(f"/api/v1/projects/{old.json()['project']['id']}/notes")).json()
    assert old_notes["total"] == 3
    assert all(not item["is_favorite"] and not item["is_pinned"] for item in old_notes["items"])

    for key in ("is_pinned", "is_favorite"):
        smuggled = json.loads(json.dumps(v8))
        smuggled["tables"]["project_notes"][0][key] = False
        preview = await client.post("/api/v1/projects/import/preview", files={
            "file": ("smuggled.json", json.dumps(smuggled).encode(), "application/json")})
        assert preview.status_code == 400 and "旧版备份不支持笔记收藏与固定" in preview.json()["detail"]
