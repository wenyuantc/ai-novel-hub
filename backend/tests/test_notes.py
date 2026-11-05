"""作品笔记：独立原文、修订冲突、权限隔离和备份兼容。"""

import asyncio
import json

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
