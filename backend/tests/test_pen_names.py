"""笔名仅属于作者；署名按作品授权投影，备份和 EPUB 保留当前署名。"""

import io
import json
import zipfile
from xml.etree import ElementTree

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFoundError
from app.models import Chapter, PenName, Project, ProjectPenName, User, UserQuota
from app.services import pen_name_service
from app.services.pen_name_service import set_project_pen_name
from tests.helpers import create_project, register
from tests.test_open_api import new_key
from tests.test_project_transfer import _backup, _import

API = "/api/v1"


async def make_pen(client, name):
    response = await client.post(f"{API}/pen-names", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def test_private_crud_project_visibility_and_collaborator_boundary(client, make_client):
    await register(client, "author")
    guest = make_client()
    await register(guest, "editor")
    first = await make_pen(client, "  远山  ")
    other = await make_pen(client, "另一个笔名")
    assert first["name"] == "远山"
    assert (await client.post(f"{API}/pen-names", json={"name": "远山"})).status_code == 409
    assert (await client.post(f"{API}/pen-names", json={"name": "   "})).status_code == 422
    assert (await client.post(f"{API}/pen-names", json={"name": "a" * 101})).status_code == 422
    project = await create_project(client, pen_name_id=first["id"])
    pid = project["id"]
    assert project["pen_name"] == {"id": first["id"], "name": "远山"}
    invitation = await client.post(f"{API}/projects/{pid}/collaborators", json={"username": "editor", "role": "editor"})
    assert invitation.status_code == 201, invitation.text
    invitation_id = invitation.json()["invitation_id"]
    assert (await guest.post(f"{API}/collaboration/invitations/project/{invitation_id}/accept",
                             json={"expected_revision": 1})).status_code == 200
    guest_project = (await guest.get(f"{API}/projects/{pid}")).json()
    assert guest_project["pen_name"] == {"id": None, "name": "远山"}
    assert guest_project["owner"]["username"] == "author"
    assert (await guest.get(f"{API}/pen-names")).json() == []
    assert (await guest.put(f"{API}/projects/{pid}/pen-name", json={"pen_name_id": None})).status_code == 404
    assert (await guest.patch(f"{API}/projects/{pid}", json={"pen_name_id": None})).status_code == 422
    assert (await guest.patch(f"{API}/pen-names/{other['id']}",
                              json={"name": "窃改", "expected_revision": 1})).status_code == 404
    assert (await guest.delete(f"{API}/pen-names/{first['id']}?expected_revision=1")).status_code == 404
    renamed = await client.patch(f"{API}/pen-names/{first['id']}", json={"name": "新远山", "expected_revision": 1})
    assert renamed.status_code == 200 and renamed.json()["revision"] == 2
    assert (await guest.get(f"{API}/projects")).json()[0]["pen_name"] == {"id": None, "name": "新远山"}
    stale = await client.delete(f"{API}/pen-names/{first['id']}?expected_revision=1")
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await client.delete(f"{API}/pen-names/{first['id']}?expected_revision=2")).status_code == 204
    assert (await client.get(f"{API}/projects/{pid}")).json()["pen_name"] is None
    assert (await guest.get(f"{API}/projects/{pid}")).json()["pen_name"] is None


async def test_admin_and_anonymous_cannot_manage_another_users_pen_names(client, make_client, app):
    author = await register(client, "pen-owner-admin-test")
    manager = make_client()
    manager_user = await register(manager, "pen-manager-test")
    async with app.state.sessionmaker() as db:
        (await db.get(User, manager_user["id"])).is_admin = True
        await db.commit()
    pen = await make_pen(client, "作者私人笔名")
    project = await create_project(client, pen_name_id=pen["id"])
    path = f"{API}/projects/{project['id']}"

    # 管理员的人工作品权限只暴露该书名称，不授予作者笔名库或关联修改权限。
    visible = await manager.get(path)
    assert visible.status_code == 200
    assert visible.json()["owner"]["id"] == author["id"]
    assert visible.json()["pen_name"] == {"id": None, "name": "作者私人笔名"}
    assert (await manager.get(f"{API}/pen-names")).json() == []
    assert (await manager.put(f"{path}/pen-name", json={"pen_name_id": None})).status_code == 404
    assert (await manager.patch(f"{API}/pen-names/{pen['id']}", json={
        "name": "不可修改", "expected_revision": 1,
    })).status_code == 404
    assert (await manager.delete(f"{API}/pen-names/{pen['id']}?expected_revision=1")).status_code == 404
    assert (await client.get(path)).json()["pen_name"] == {"id": pen["id"], "name": "作者私人笔名"}

    anonymous = make_client()
    for result in (
        await anonymous.get(f"{API}/pen-names"),
        await anonymous.post(f"{API}/pen-names", json={"name": "匿名"}),
        await anonymous.put(f"{path}/pen-name", json={"pen_name_id": None}),
    ):
        assert result.status_code == 401 and isinstance(result.json()["detail"], str)


async def test_binding_checks_owner_database_and_rollback(client, make_client, app):
    owner = await register(client, "owner-pen")
    guest = make_client()
    guest_user = await register(guest, "guest-pen")
    foreign = await make_pen(guest, "远山")
    own = await make_pen(client, "远山")
    p = await create_project(client)
    pid = p["id"]
    bad = await client.post(f"{API}/projects", json={"title": "不能留下", "pen_name_id": foreign["id"]})
    assert bad.status_code == 404
    assert all(item["title"] != "不能留下" for item in (await client.get(f"{API}/projects")).json())
    assert (await client.put(f"{API}/projects/{pid}/pen-name", json={"pen_name_id": foreign["id"]})).status_code == 404
    assert (await client.put(f"{API}/projects/{pid}/pen-name", json={"pen_name_id": own["id"]})).status_code == 200
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(ProjectPenName, pid))
        await db.flush()
        db.add(ProjectPenName(project_id=pid, owner_id=owner["id"], pen_name_id=foreign["id"]))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert (await db.get(ProjectPenName, pid)).pen_name_id == own["id"]
        count = select(func.count()).select_from(PenName).where(PenName.owner_id == guest_user["id"])
        assert await db.scalar(count) == 1
    assert (await client.put(f"{API}/projects/{pid}/pen-name", json={"pen_name_id": None})).status_code == 200
    assert (await client.put(f"{API}/projects/{pid}/pen-name", json={"pen_name_id": None})).status_code == 200


async def test_denied_binding_does_not_flush_pending_data(client, make_client, app):
    await register(client, "flush-owner")
    guest = make_client()
    actor = await register(guest, "flush-guest")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        pending = Chapter(project_id=project["id"], title="不得自动保存")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(NotFoundError):
            await set_project_pen_name(db, actor["id"], project["id"], None)
        assert pending in db.new and pending.id is None and not flushes
        await db.rollback()


async def test_project_list_pen_name_projection_uses_fixed_query_count(client, app):
    await register(client, "query-owner")
    pen = await make_pen(client, "固定查询")
    await create_project(client, pen_name_id=pen["id"])
    queries = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        first = await client.get(f"{API}/projects")
        assert first.status_code == 200 and len(first.json()) == 1
        first_count = len(queries)
        for index in range(4):
            await create_project(client, title=f"作品{index}", pen_name_id=pen["id"])
        queries.clear()
        second = await client.get(f"{API}/projects")
        assert second.status_code == 200 and len(second.json()) == 5
        assert len(queries) == first_count
        assert all(item["pen_name"]["name"] == "固定查询" for item in second.json())
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_delete_pen_name_unbinds_recycled_project_without_deleting_it(client, app):
    await register(client, "trash-owner")
    pen = await make_pen(client, "回收站笔名")
    project = await create_project(client, pen_name_id=pen["id"])
    assert (await client.delete(f"{API}/projects/{project['id']}")).status_code == 204
    assert (await client.delete(f"{API}/pen-names/{pen['id']}?expected_revision=1")).status_code == 204
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).deleted_at is not None
        assert await db.get(ProjectPenName, project["id"]) is None


async def test_backup_only_current_pen_name_and_restores_per_importer(client, make_client):
    await register(client, "backup-author")
    pen = await make_pen(client, "当前署名")
    await make_pen(client, "绝不能导出")
    project = await create_project(client, pen_name_id=pen["id"])
    backup = await _backup(client, project["id"])
    assert backup["version"] == 18 and backup["app_schema"] == "0079"
    assert backup["pen_name"] == "当前署名" and "绝不能导出" not in json.dumps(backup, ensure_ascii=False)
    guest = make_client()
    await register(guest, "backup-guest")
    existing = await make_pen(guest, "当前署名")
    restored = await _import(guest, backup)
    assert restored["project"]["pen_name"] == {"id": existing["id"], "name": "当前署名"}
    assert len((await guest.get(f"{API}/pen-names")).json()) == 1
    third = make_client()
    await register(third, "backup-third")
    new_restored = await _import(third, backup)
    assert new_restored["project"]["pen_name"]["name"] == "当前署名"
    assert len((await third.get(f"{API}/pen-names")).json()) == 1
    old = dict(backup, version=9)
    old.pop("pen_name")
    assert (await _import(guest, old))["project"]["pen_name"] is None
    bad = dict(backup, pen_name={"name": "非法"})
    response = await guest.post(f"{API}/projects/import/preview", files={
        "file": ("bad.json", json.dumps(bad, ensure_ascii=False).encode(), "application/json"),
    })
    assert response.status_code == 400
    assert len((await guest.get(f"{API}/pen-names")).json()) == 1
    forged_old = dict(backup, version=9)
    response = await guest.post(f"{API}/projects/import/preview", files={
        "file": ("bad-old.json", json.dumps(forged_old, ensure_ascii=False).encode(), "application/json"),
    })
    assert response.status_code == 400


@pytest.mark.parametrize("failure", ["quota", "after_pen_created"])
async def test_failed_backup_import_leaves_no_project_or_pen_name(client, make_client, app, monkeypatch, failure):
    await register(client, "rollback-author")
    pen = await make_pen(client, "待恢复笔名")
    project = await create_project(client, pen_name_id=pen["id"])
    backup = (await client.get(f"{API}/projects/{project['id']}/backup")).content
    target = make_client()
    user = await register(target, "rollback-target")
    preview = await target.post(f"{API}/projects/import/preview", files={
        "file": ("backup.json", backup, "application/json"),
    })
    assert preview.status_code == 200, preview.text
    if failure == "quota":
        async with app.state.sessionmaker() as db:
            db.add(UserQuota(user_id=user["id"], project_mode="limited", project_value=0))
            await db.commit()
    else:
        original = pen_name_service.find_or_create_without_commit

        async def fail_after_insert(db, owner_id, name):
            await original(db, owner_id, name)
            raise ValueError("模拟笔名保存失败")

        monkeypatch.setattr(pen_name_service, "find_or_create_without_commit", fail_after_insert)
    result = await target.post(f"{API}/projects/import", json={"token": preview.json()["token"]})
    assert result.status_code == (409 if failure == "quota" else 400)
    assert (await target.get(f"{API}/projects")).json() == []
    assert (await target.get(f"{API}/pen-names")).json() == []


def _epub_author(body):
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        root = ElementTree.fromstring(zf.read("OEBPS/content.opf"))
    return root.findtext("{http://www.idpf.org/2007/opf}metadata/{http://purl.org/dc/elements/1.1/}creator")


async def test_both_epub_paths_use_pen_name_and_explicit_override(client):
    await register(client, "epub-author")
    pen = await make_pen(client, "作品笔名")
    project = await create_project(client, pen_name_id=pen["id"])
    base = f"{API}/projects/{project['id']}"
    assert (await client.post(f"{base}/chapters", json={"title": "第一章", "content": "正文"})).status_code == 201
    default = await client.get(f"{base}/export", params={"format": "epub"})
    assert default.status_code == 200 and _epub_author(default.content) == "作品笔名"
    custom = await client.get(f"{base}/export", params={"format": "epub", "author": "手写作者"})
    assert custom.status_code == 200 and _epub_author(custom.content) == "手写作者"
    empty = await client.get(f"{base}/export", params={"format": "epub", "author": ""})
    assert empty.status_code == 200 and _epub_author(empty.content) is None
    key = await new_key(client, [project["id"]])
    opened = await client.get(f"/api/open/v1/novels/{project['id']}/export", params={"format": "epub"}, headers=key)
    assert opened.status_code == 200, opened.text
    assert _epub_author(opened.content) == "作品笔名"
    assert (await client.put(f"{base}/pen-name", json={"pen_name_id": None})).status_code == 200
    fallback = await client.get(f"{base}/export", params={"format": "epub"})
    assert fallback.status_code == 200 and _epub_author(fallback.content) == "epub-author"
