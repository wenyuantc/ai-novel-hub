"""真实 PostgreSQL 上验证笔名 User 屏障和双外键。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import PenName, Project, ProjectPenName, User
from app.services import authorization_service, pen_name_service, project_service, quota_service, recycle_bin_service
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("first", ["delete", "bind"])
async def test_delete_and_binding_serialize_on_user(postgres_collaboration_apps, monkeypatch, first):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        user = await register(owner, "pg-pen-owner")
        login = await peer.post("/api/v1/auth/login", json={"username": "pg-pen-owner", "password": "secret123"})
        assert login.status_code == 200, login.text
        project = await create_project(owner)
        pen = (await owner.post("/api/v1/pen-names", json={"name": "并发笔名"})).json()
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        if first == "delete":
            original = pen_name_service._owned

            async def hold_owned(db, owner_id, pen_name_id, *, lock=False):
                row = await original(db, owner_id, pen_name_id, lock=lock)
                if lock:
                    holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    reached.set()
                    await release.wait()
                return row

            monkeypatch.setattr(pen_name_service, "_owned", hold_owned)
        else:
            original_bind = pen_name_service.bind_without_commit

            async def hold_bind(db, owner_id, project_id, pen_name_id):
                await original_bind(db, owner_id, project_id, pen_name_id)
                await db.flush()
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()

            monkeypatch.setattr(pen_name_service, "bind_without_commit", hold_bind)
        delete_path = f"/api/v1/pen-names/{pen['id']}?expected_revision=1"
        bind_path = f"/api/v1/projects/{project['id']}/pen-name"
        first_task = asyncio.create_task(
            owner.delete(delete_path) if first == "delete" else owner.put(bind_path, json={"pen_name_id": pen["id"]})
        )
        second_task = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            second_task = asyncio.create_task(
                peer.put(bind_path, json={"pen_name_id": pen["id"]}) if first == "delete" else peer.delete(delete_path)
            )
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            assert (await asyncio.wait_for(first_task, 8)).status_code == (204 if first == "delete" else 200)
            assert (await asyncio.wait_for(second_task, 8)).status_code == (404 if first == "delete" else 204)
            async with app1.state.sessionmaker() as db:
                assert await db.get(PenName, pen["id"]) is None
                assert await db.get(ProjectPenName, project["id"]) is None
            assert (await owner.get(f"/api/v1/projects/{project['id']}")).json()["owner"]["id"] == user["id"]
        finally:
            release.set()
            for task in (first_task, second_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("operation", ["create", "restore"])
@pytest.mark.parametrize("first", ["delete", "operation"])
async def test_delete_and_project_creation_serialize_on_user(
    postgres_collaboration_apps, monkeypatch, operation, first,
):
    """两边真的等待另一事务：创建按旧 ID 失败，恢复则按名称重建已删笔名。"""
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        user = await register(owner, "pg-create-pen")
        login = await peer.post("/api/v1/auth/login", json={"username": "pg-create-pen", "password": "secret123"})
        assert login.status_code == 200, login.text
        pen = (await owner.post("/api/v1/pen-names", json={"name": "竞争恢复笔名"})).json()
        source = await create_project(owner, pen_name_id=pen["id"])
        token = None
        if operation == "restore":
            backup = await owner.get(f"/api/v1/projects/{source['id']}/backup")
            assert backup.status_code == 200, backup.text
            preview = await peer.post("/api/v1/projects/import/preview", files={
                "file": ("backup.json", backup.content, "application/json"),
            })
            assert preview.status_code == 200, preview.text
            token = preview.json()["token"]

        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        if first == "delete":
            original = pen_name_service._owned

            async def hold_delete(db, owner_id, pen_name_id, *, lock=False):
                row = await original(db, owner_id, pen_name_id, lock=lock)
                if lock:
                    holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    reached.set()
                    await release.wait()
                return row

            monkeypatch.setattr(pen_name_service, "_owned", hold_delete)
        else:
            original_bind = pen_name_service.bind_without_commit

            async def hold_created_binding(db, owner_id, project_id, pen_name_id):
                await original_bind(db, owner_id, project_id, pen_name_id)
                await db.flush()
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()

            monkeypatch.setattr(pen_name_service, "bind_without_commit", hold_created_binding)

        def delete_request(client):
            return client.delete(f"/api/v1/pen-names/{pen['id']}?expected_revision=1")

        def operation_request(client):
            if operation == "create":
                return client.post("/api/v1/projects", json={"title": "竞争新书", "pen_name_id": pen["id"]})
            return client.post("/api/v1/projects/import", json={"token": token, "title": "竞争恢复"})

        first_task = asyncio.create_task(delete_request(owner) if first == "delete" else operation_request(owner))
        second_task = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            second_task = asyncio.create_task(operation_request(peer) if first == "delete" else delete_request(peer))
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            first_response = await asyncio.wait_for(first_task, 12)
            second_response = await asyncio.wait_for(second_task, 12)
            assert first_response.status_code == (204 if first == "delete" else 201), first_response.text
            expected_second = (
                404 if first == "delete" and operation == "create" else (201 if first == "delete" else 204)
            )
            assert second_response.status_code == expected_second, second_response.text
            async with app1.state.sessionmaker() as db:
                projects = list(await db.scalars(select(Project).where(Project.user_id == user["id"])))
                pens = list(await db.scalars(select(PenName).where(PenName.owner_id == user["id"])))
                bindings = list(await db.scalars(select(ProjectPenName).where(ProjectPenName.owner_id == user["id"])))
                assert len(projects) == (1 if expected_second == 404 else 2)
                if first == "delete" and operation == "restore":
                    assert len(pens) == len(bindings) == 1
                    assert pens[0].id != pen["id"] and pens[0].name == "竞争恢复笔名"
                    assert bindings[0].project_id != source["id"] and bindings[0].pen_name_id == pens[0].id
                else:
                    assert pens == [] and bindings == []
        finally:
            release.set()
            for task in (first_task, second_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("first", ["recycle", "bind"])
async def test_recycle_and_binding_serialize(postgres_collaboration_apps, monkeypatch, first):
    """回收意图先提交时不得再绑定；已提交的关联随回收站作品保留。"""
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        await register(owner, "pg-recycle-pen")
        login = await peer.post("/api/v1/auth/login", json={"username": "pg-recycle-pen", "password": "secret123"})
        assert login.status_code == 200, login.text
        project = await create_project(owner)
        pen = (await owner.post("/api/v1/pen-names", json={"name": "回收竞争"})).json()
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        if first == "recycle":
            original = recycle_bin_service.get_manageable_project

            async def hold_recycle(db, actor_id, project_id, *, lock=False, permission="projects.read"):
                row = await original(db, actor_id, project_id, lock=lock, permission=permission)
                if lock:
                    holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    reached.set()
                    await release.wait()
                return row

            monkeypatch.setattr(recycle_bin_service, "get_manageable_project", hold_recycle)
        else:
            original_bind = pen_name_service.bind_without_commit

            async def hold_binding(db, owner_id, project_id, pen_name_id):
                await original_bind(db, owner_id, project_id, pen_name_id)
                await db.flush()
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()

            monkeypatch.setattr(pen_name_service, "bind_without_commit", hold_binding)

        path = f"/api/v1/projects/{project['id']}"
        first_task = asyncio.create_task(
            owner.delete(path) if first == "recycle" else owner.put(f"{path}/pen-name", json={"pen_name_id": pen["id"]})
        )
        second_task = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            second_task = asyncio.create_task(
                peer.put(f"{path}/pen-name", json={"pen_name_id": pen["id"]})
                if first == "recycle" else peer.delete(path)
            )
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            first_response = await asyncio.wait_for(first_task, 12)
            second_response = await asyncio.wait_for(second_task, 12)
            assert first_response.status_code == (204 if first == "recycle" else 200), first_response.text
            assert second_response.status_code in ({404, 409} if first == "recycle" else {204}), second_response.text
            async with app1.state.sessionmaker() as db:
                recycled = await db.get(Project, project["id"])
                binding = await db.get(ProjectPenName, project["id"])
                assert recycled is not None and recycled.deleted_at is not None
                assert (binding is None) == (first == "recycle")
                if binding is not None:
                    assert binding.pen_name_id == pen["id"]
                assert await db.get(PenName, pen["id"]) is not None
        finally:
            release.set()
            for task in (first_task, second_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_postgres_same_name_cas_and_cross_owner_fk(postgres_collaboration_apps):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        user = await register(owner, "pg-owner2")
        login = await peer.post("/api/v1/auth/login", json={"username": "pg-owner2", "password": "secret123"})
        assert login.status_code == 200
        made = await asyncio.gather(*[
            c.post("/api/v1/pen-names", json={"name": "重复笔名"}) for c in (owner, peer)
        ])
        assert sorted(r.status_code for r in made) == [201, 409]
        pen = next(r.json() for r in made if r.status_code == 201)
        changed = await asyncio.gather(*[
            c.patch(f"/api/v1/pen-names/{pen['id']}", json={"name": name, "expected_revision": 1})
            for c, name in ((owner, "甲"), (peer, "乙"))
        ])
        assert sorted(r.status_code for r in changed) == [200, 409]
        assert next(r for r in changed if r.status_code == 409).json()["code"] == "stale_revision"
        project = await create_project(owner)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as other:
            await register(other, "pg-other2")
            foreign = (await other.post("/api/v1/pen-names", json={"name": "他人"})).json()
        async with app1.state.sessionmaker() as db:
            db.add(ProjectPenName(project_id=project["id"], owner_id=user["id"], pen_name_id=foreign["id"]))
            with pytest.raises(IntegrityError):
                await db.flush()
            await db.rollback()
            assert await db.scalar(select(ProjectPenName).where(ProjectPenName.project_id == project["id"])) is None


@pytest.mark.parametrize("operation", ["bind", "create", "restore"])
async def test_disabled_actor_cannot_complete_older_pen_name_write(postgres_collaboration_apps, monkeypatch, operation):
    app1, app2 = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner:
        user = await register(owner, "pg-stale-owner")
        pen = (await owner.post("/api/v1/pen-names", json={"name": "笔名"})).json()
        project = await create_project(owner)
        token = None
        if operation == "restore":
            assert (await owner.put(f"/api/v1/projects/{project['id']}/pen-name",
                                    json={"pen_name_id": pen["id"]})).status_code == 200
            backup = await owner.get(f"/api/v1/projects/{project['id']}/backup")
            preview = await owner.post("/api/v1/projects/import/preview", files={
                "file": ("backup.json", backup.content, "application/json"),
            })
            assert preview.status_code == 200, preview.text
            token = preview.json()["token"]
        reached, release = asyncio.Event(), asyncio.Event()
        if operation == "bind":
            original = authorization_service.require_authenticated

            async def delayed_auth(db, actor, *, write=False, targets=()):
                if write and actor == user["id"] and not reached.is_set():
                    reached.set()
                    await release.wait()
                return await original(db, actor, write=write, targets=targets)

            monkeypatch.setattr(authorization_service, "require_authenticated", delayed_auth)
        else:
            original_quota = quota_service.check_project_creation

            async def delayed_quota(db, actor_id):
                if actor_id == user["id"] and not reached.is_set():
                    reached.set()
                    await release.wait()
                return await original_quota(db, actor_id)

            monkeypatch.setattr(quota_service, "check_project_creation", delayed_quota)
        if operation == "bind":
            request = owner.put(f"/api/v1/projects/{project['id']}/pen-name", json={"pen_name_id": pen["id"]})
        elif operation == "create":
            request = owner.post("/api/v1/projects", json={"title": "旧登录不能创建", "pen_name_id": pen["id"]})
        else:
            request = owner.post("/api/v1/projects/import", json={"token": token})
        pending = asyncio.create_task(request)
        try:
            await asyncio.wait_for(reached.wait(), 8)
            async with app2.state.sessionmaker() as db:
                actor = await db.get(User, user["id"])
                actor.is_active = False
                await db.commit()
            release.set()
            assert (await asyncio.wait_for(pending, 8)).status_code == 401
            async with app2.state.sessionmaker() as db:
                projects = list(await db.scalars(select(Project).where(Project.user_id == user["id"])))
                assert [row.id for row in projects] == [project["id"]]
                binding = await db.get(ProjectPenName, project["id"])
                assert (binding is not None) == (operation == "restore")
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_revocation_before_final_projection_does_not_leak_pen_name(postgres_collaboration_apps, monkeypatch):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as guest,
    ):
        await register(owner, "pg-share-owner")
        member = await register(guest, "pg-share-editor")
        pen = (await owner.post("/api/v1/pen-names", json={"name": "不再可见"})).json()
        project = await create_project(owner, pen_name_id=pen["id"])
        base = f"/api/v1/projects/{project['id']}"
        invitation = await owner.post(f"{base}/collaborators", json={"username": "pg-share-editor", "role": "editor"})
        assert invitation.status_code == 201
        path = f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept"
        assert (await guest.post(path, json={"expected_revision": 1})).status_code == 200
        reached, release = asyncio.Event(), asyncio.Event()
        original = project_service._final_project_projections

        async def delayed(db, user_id, *, project_id=None):
            if user_id == member["id"]:
                reached.set()
                await release.wait()
            return await original(db, user_id, project_id=project_id)

        monkeypatch.setattr(project_service, "_final_project_projections", delayed)
        pending = asyncio.create_task(guest.get("/api/v1/projects"))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            revoked = await owner.delete(f"{base}/collaborators/{member['id']}", params={"expected_revision": 2})
            assert revoked.status_code == 204, revoked.text
            release.set()
            result = await asyncio.wait_for(pending, 8)
            assert result.status_code == 200 and result.json() == []
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
