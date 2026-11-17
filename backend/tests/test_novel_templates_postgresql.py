"""真实 PostgreSQL 模板锁序、额度、去重与来源风格竞争。"""

import asyncio
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.models import (
    NovelTemplate,
    NovelTemplateCreationReceipt,
    NovelTemplateImage,
    Project,
    User,
    UserQuota,
    WritingStyle,
)
from app.services import (
    novel_template_apply,
    novel_template_service,
    novel_template_snapshot,
    project_lifecycle,
    recycle_bin_service,
    series_service,
)
from tests.helpers import create_project, register
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_novel_templates import BASE, _save
from tests.test_series_transfer import _join_books
from tests.test_setting_thumbnails import image_data

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def _login(client, username):
    result = await client.post("/api/v1/auth/login", json={"username": username, "password": "secret123"})
    assert result.status_code == 200, result.text


async def test_postgresql_unknown_commit_result_replays_receipt(postgres_collaboration_apps, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    app, _ = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        owner = await register(client, "pg-template-unknown-commit")
        source = await create_project(client, style="")
        template = await _save(client, source["id"])
        body = {"expected_revision": 1, "intent_key": str(uuid4()), "title": "提交确认丢失"}
        apply = novel_template_apply.apply_template
        commit = AsyncSession.commit
        failed = False

        async def mark_creation(db, project, snapshot, images):
            await apply(db, project, snapshot, images)
            db.info["simulate_template_commit_unknown"] = True

        async def commit_then_lose_ack(db):
            nonlocal failed
            marked = db.info.pop("simulate_template_commit_unknown", False)
            await commit(db)
            if marked and not failed:
                failed = True
                raise ConnectionError("模拟数据库提交确认丢失")

        monkeypatch.setattr(novel_template_apply, "apply_template", mark_creation)
        monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_ack)
        path = f"{BASE}/{template['id']}/projects"
        with pytest.raises(ConnectionError, match="提交确认丢失"):
            await client.post(path, json=body)
        async with app.state.sessionmaker() as db:
            receipt = await db.scalar(select(NovelTemplateCreationReceipt).where(
                NovelTemplateCreationReceipt.owner_id == owner["id"],
                NovelTemplateCreationReceipt.intent_key == body["intent_key"],
            ))
            assert receipt is not None and receipt.result_project_id is not None
            created_id = receipt.result_project_id
        replay = await client.post(path, json=body)
        assert replay.status_code == 201 and replay.json()["id"] == created_id
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 2


async def test_postgresql_corrupt_snapshot_is_rejected_without_creating_project(postgres_collaboration_apps):
    app, _ = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        owner = await register(client, "pg-template-corrupt-snapshot")
        source = await create_project(client, style="")
        template = await _save(client, source["id"])
        async with app.state.sessionmaker() as db:
            # PostgreSQL JSON 列限制语法，仍可能存储不符合模板协议的有效 JSON。
            await db.execute(update(NovelTemplate).where(NovelTemplate.id == template["id"]).values(snapshot=[]))
            await db.commit()
        path = f"{BASE}/{template['id']}"
        detail = await client.get(path)
        assert detail.status_code == 400 and detail.json()["code"] == "invalid_novel_template"
        created = await client.post(f"{path}/projects", json={
            "expected_revision": 1, "intent_key": str(uuid4()), "title": "不能创建",
        })
        assert created.status_code == 400 and created.json()["code"] == "invalid_novel_template"
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == owner["id"])) == 1
            assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id))) == 0


@pytest.mark.parametrize("kind", ["same", "conflicting", "quota"])
async def test_template_creation_user_gate_and_intent(postgres_collaboration_apps, monkeypatch, kind):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        user = await register(owner, "pg-template-intent")
        await _login(peer, "pg-template-intent")
        source = await create_project(owner, style="")
        template = await _save(owner, source["id"])
        if kind == "quota":
            async with app1.state.sessionmaker() as db:
                db.add(UserQuota(user_id=user["id"], project_mode="limited", project_value=2))
                await db.commit()
        reached, release = asyncio.Event(), asyncio.Event()
        holder: dict[str, int] = {}
        original = novel_template_apply.apply_template

        async def hold_apply(db, project, snapshot, images):
            if not reached.is_set():
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            await original(db, project, snapshot, images)

        monkeypatch.setattr(novel_template_apply, "apply_template", hold_apply)
        first_body = {"expected_revision": 1, "intent_key": str(uuid4()), "title": "并发新书"}
        other = dict(first_body)
        if kind == "conflicting":
            other["title"] = "不同请求"
        if kind == "quota":
            other["intent_key"] = str(uuid4())
        path = f"{BASE}/{template['id']}/projects"
        first = asyncio.create_task(owner.post(path, json=first_body))
        second = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            second = asyncio.create_task(peer.post(path, json=other))
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            created = await asyncio.wait_for(first, 15)
            result = await asyncio.wait_for(second, 15)
            assert created.status_code == 201, created.text
            if kind == "same":
                assert result.status_code == 201 and result.json()["id"] == created.json()["id"]
            else:
                assert result.status_code == 409
                expected = "creation_intent_conflict" if kind == "conflicting" else "quota_exceeded"
                assert result.json()["code"] == expected
            async with app1.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == user["id"])) == 2
                assert await db.scalar(select(func.count(NovelTemplateCreationReceipt.id)).where(
                    NovelTemplateCreationReceipt.owner_id == user["id"],
                )) == 1
        finally:
            release.set()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("mutation", ["update", "delete"])
@pytest.mark.parametrize("first", ["save", "style"])
async def test_source_style_and_template_save_lock_order(postgres_collaboration_apps, monkeypatch, mutation, first):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        await register(owner, "pg-template-style")
        await _login(peer, "pg-template-style")
        source = await create_project(owner)
        style_id = source["default_style_id"]
        preview = await owner.post(f"/api/v1/projects/{source['id']}/novel-template-preview", json={
            "include_config": True,
        })
        assert preview.status_code == 200, preview.text
        body = {"name": "风格快照", "source_project_id": source["id"], "selection": {"include_config": True},
                "source_stamp": preview.json()["source_stamp"]}
        if first == "save":
            reached, release = asyncio.Event(), asyncio.Event()
            holder: dict[str, int] = {}
            original = novel_template_service._capture_for_save

            async def hold_capture(db, source, selection, stamp):
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
                return await original(db, source, selection, stamp)

            monkeypatch.setattr(novel_template_service, "_capture_for_save", hold_capture)
            saving = asyncio.create_task(owner.post(BASE, json=body))
            changing = None
            try:
                await asyncio.wait_for(reached.wait(), 8)
                changing = asyncio.create_task(
                    peer.patch(f"/api/v1/styles/{style_id}", json={"description": "风格已更改"})
                    if mutation == "update" else peer.delete(f"/api/v1/styles/{style_id}?force=true")
                )
                await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
                release.set()
                saved, changed = await asyncio.gather(saving, changing)
                assert saved.status_code == 201, saved.text
                assert changed.status_code == (200 if mutation == "update" else 204), changed.text
            finally:
                release.set()
                for task in (saving, changing):
                    if task is not None and not task.done():
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
        else:
            async with app1.state.sessionmaker() as db:
                style = await db.scalar(select(WritingStyle).where(WritingStyle.id == style_id).with_for_update())
                holder = await db.scalar(text("SELECT pg_backend_pid()"))
                if mutation == "update":
                    style.description = "风格已更改"
                    await db.flush()
                else:
                    await db.delete(style)
                    await db.flush()
                saving = asyncio.create_task(peer.post(BASE, json=body))
                try:
                    await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder), 8)
                    await db.commit()
                    response = await asyncio.wait_for(saving, 15)
                    assert response.status_code == 409 and response.json()["code"] == "template_source_changed"
                finally:
                    if not saving.done():
                        saving.cancel()
                        with suppress(asyncio.CancelledError):
                            await saving


async def test_postgresql_template_image_length_and_hash_are_verified(postgres_collaboration_apps):
    app, _ = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client, "pg-template-image")
        source = await create_project(client, style="")
        base = f"/api/v1/projects/{source['id']}"
        character = await client.post(f"{base}/characters", json={"name": "带图角色"})
        cid = character.json()["id"]
        upload = await client.put(f"{base}/settings/character/{cid}/thumbnail", data={"expected_version": ""},
                                  files={"file": ("a.png", image_data(), "image/png")})
        assert upload.status_code == 200, upload.text
        template = await _save(client, source["id"], selection={"include_config": True, "character_ids": [cid]})
        detail = await client.get(f"{BASE}/{template['id']}")
        ref = detail.json()["contents"]["characters"][0]["ref"]
        path = f"{BASE}/{template['id']}/images/{ref}?revision=1"
        assert (await client.get(path)).status_code == 200
        valid = await client.post(f"{BASE}/{template['id']}/projects", json={
            "expected_revision": 1, "intent_key": str(uuid4()), "title": "有效图片可建书",
        })
        assert valid.status_code == 201, valid.text
        async with app.state.sessionmaker() as db:
            image = await db.scalar(select(NovelTemplateImage).where(NovelTemplateImage.template_id == template["id"]))
            image.data = b"x" * (512 * 1024 + 1)
            await db.commit()
        invalid = await client.get(path)
        assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_novel_template"
        create = await client.post(f"{BASE}/{template['id']}/projects", json={
            "expected_revision": 1, "intent_key": str(uuid4()), "title": "损坏不可建书",
        })
        assert create.status_code == 400 and create.json()["code"] == "invalid_novel_template"


@pytest.mark.parametrize("first", ["purge", "replay"])
async def test_expired_project_purge_and_intent_replay_lock_order(postgres_collaboration_apps, monkeypatch, first):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        await register(owner, "pg-template-purge")
        await _login(peer, "pg-template-purge")
        source = await create_project(owner, style="")
        template = await _save(owner, source["id"])
        body = {"expected_revision": 1, "intent_key": str(uuid4()), "title": "待清理结果"}
        path = f"{BASE}/{template['id']}/projects"
        created = await owner.post(path, json=body)
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        assert (await owner.delete(f"/api/v1/projects/{project_id}")).status_code == 204
        async with app1.state.sessionmaker() as db:
            deleted_at = await db.scalar(select(Project.deleted_at).where(Project.id == project_id))
        assert deleted_at is not None
        reached, release = asyncio.Event(), asyncio.Event()
        holder: dict[str, int] = {}
        first_task = None
        original = project_lifecycle.lock_record

        async def hold_first_project(db, locked_id, *, active_only=False):
            project = await original(db, locked_id, active_only=active_only)
            if locked_id == project_id and asyncio.current_task() is first_task:
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return project

        monkeypatch.setattr(project_lifecycle, "lock_record", hold_first_project)

        async def purge():
            async with app1.state.sessionmaker() as db:
                await recycle_bin_service.purge(
                    db, app1.state.task_engine, None, project_id, deleted_at,
                    cutoff=project_lifecycle.as_utc(deleted_at) + timedelta(days=31),
                )

        async def replay():
            return await peer.post(path, json=body)

        first_task = asyncio.create_task(purge() if first == "purge" else replay())
        other_task = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            other_task = asyncio.create_task(replay() if first == "purge" else purge())
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            first_result, other_result = await asyncio.wait_for(asyncio.gather(first_task, other_task), 15)
            result = other_result if first == "purge" else first_result
            assert result.status_code == 409 and result.json()["code"] == "creation_result_unavailable"
            async with app1.state.sessionmaker() as db:
                assert await db.get(Project, project_id) is None
                receipt = await db.scalar(select(NovelTemplateCreationReceipt).where(
                    NovelTemplateCreationReceipt.intent_key == body["intent_key"],
                ))
                assert receipt is not None and receipt.result_project_id is None
        finally:
            release.set()
            for task in (first_task, other_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("change", ["character", "image"])
async def test_postgresql_source_content_change_invalidates_preview(postgres_collaboration_apps, change):
    app, _ = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client, "pg-template-source-change")
        source = await create_project(client, style="")
        base = f"/api/v1/projects/{source['id']}"
        character = await client.post(f"{base}/characters", json={"name": "原始人物", "profile": "原设定"})
        assert character.status_code == 201, character.text
        cid = character.json()["id"]
        thumbnail = f"{base}/settings/character/{cid}/thumbnail"
        initial = await client.put(thumbnail, data={"expected_version": ""},
                                   files={"file": ("a.png", image_data(), "image/png")})
        assert initial.status_code == 200, initial.text
        selection = {"include_config": True, "character_ids": [cid]}
        preview = await client.post(f"{base}/novel-template-preview", json=selection)
        assert preview.status_code == 200, preview.text
        if change == "character":
            changed = await client.patch(f"{base}/characters/{cid}", json={"profile": "修改后"})
        else:
            changed = await client.put(thumbnail, data={"expected_version": initial.json()["version"]},
                                       files={"file": ("b.png", image_data("blue"), "image/png")})
        assert changed.status_code == 200, changed.text
        saved = await client.post(BASE, json={"name": "过时快照", "source_project_id": source["id"],
                                              "selection": selection,
                                              "source_stamp": preview.json()["source_stamp"]})
        assert saved.status_code == 409 and saved.json()["code"] == "template_source_changed"


@pytest.mark.parametrize("change", ["content", "token"])
async def test_preview_uses_consistent_snapshot_then_rechecks_actor(postgres_collaboration_apps, monkeypatch, change):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        user = await register(owner, "pg-template-preview-snapshot")
        await _login(peer, "pg-template-preview-snapshot")
        source = await create_project(owner, style="", genre="预览之前")
        base = f"/api/v1/projects/{source['id']}"
        before = await owner.post(f"{base}/novel-template-preview", json={"include_config": True})
        assert before.status_code == 200, before.text
        reached, release = asyncio.Event(), asyncio.Event()
        original = novel_template_snapshot.capture

        async def hold_snapshot(db, project_id, selection):
            reached.set()
            await release.wait()
            return await original(db, project_id, selection)

        monkeypatch.setattr(novel_template_snapshot, "capture", hold_snapshot)
        reading = asyncio.create_task(owner.post(f"{base}/novel-template-preview", json={"include_config": True}))
        try:
            await asyncio.wait_for(reached.wait(), 8)
            if change == "content":
                modified = await peer.patch(base, json={"genre": "预览期间"})
                assert modified.status_code == 200, modified.text
            else:
                async with app2.state.sessionmaker() as db:
                    row = await db.get(User, user["id"])
                    row.token_version += 1
                    await db.commit()
            release.set()
            result = await asyncio.wait_for(reading, 15)
            if change == "token":
                assert result.status_code == 401
            else:
                assert result.status_code == 200, result.text
                assert result.json()["source_stamp"] == before.json()["source_stamp"]
        finally:
            release.set()
            if not reading.done():
                reading.cancel()
                with suppress(asyncio.CancelledError):
                    await reading


@pytest.mark.parametrize("first", ["save", "series"])
async def test_series_update_and_template_save_serialize(postgres_collaboration_apps, monkeypatch, first):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        await register(owner, "pg-template-series")
        await _login(peer, "pg-template-series")
        source = await create_project(owner, style="")
        series_path, series = await _join_books(owner, source)
        entry = await owner.post(f"{series_path}/entries", json={
            "kind": "character", "data": {"name": "共享角色", "profile": "初始值"},
            "expected_revision": series["revision"],
        })
        assert entry.status_code == 201, entry.text
        entry_id = entry.json()["item"]["id"]
        used = await owner.post(f"{series_path}/entries/{entry_id}/uses", json={
            "project_id": source["id"], "expected_revision": entry.json()["revision"],
        })
        assert used.status_code == 200, used.text
        selection = {"include_config": True, "character_ids": [used.json()["ref"]["id"]]}
        preview = await owner.post(f"/api/v1/projects/{source['id']}/novel-template-preview", json=selection)
        assert preview.status_code == 200, preview.text
        save_body = {"name": "系列快照", "source_project_id": source["id"], "selection": selection,
                     "source_stamp": preview.json()["source_stamp"]}
        reached, release = asyncio.Event(), asyncio.Event()
        holder: dict[str, int] = {}
        original_capture = novel_template_service._capture_for_save
        original_lock = series_service._lock_projects

        async def hold_capture(db, source, selection, stamp):
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return await original_capture(db, source, selection, stamp)

        async def hold_series(db, *args, **kwargs):
            projects = await original_lock(db, *args, **kwargs)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return projects

        monkeypatch.setattr(novel_template_service, "_capture_for_save", hold_capture if first == "save"
                            else original_capture)
        monkeypatch.setattr(series_service, "_lock_projects", hold_series if first == "series" else original_lock)

        async def save():
            return await owner.post(BASE, json=save_body)

        async def change():
            return await peer.patch(f"{series_path}/entries/{entry_id}", json={
                "expected_revision": used.json()["revision"], "data": {"profile": "系列新值"},
            })

        first_task = asyncio.create_task(save() if first == "save" else change())
        other_task = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            other_task = asyncio.create_task(change() if first == "save" else save())
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            first_result, other_result = await asyncio.wait_for(asyncio.gather(first_task, other_task), 15)
            saved, changed = (first_result, other_result) if first == "save" else (other_result, first_result)
            assert changed.status_code == 200, changed.text
            if first == "series":
                assert saved.status_code == 409 and saved.json()["code"] == "template_source_changed"
            else:
                assert saved.status_code == 201, saved.text
                book = await owner.post(f"{BASE}/{saved.json()['id']}/projects", json={
                    "expected_revision": 1, "intent_key": str(uuid4()), "title": "冻结的系列资料",
                })
                assert book.status_code == 201, book.text
                characters = await owner.get(f"/api/v1/projects/{book.json()['id']}/characters")
                assert characters.status_code == 200 and characters.json()[0]["profile"] == "初始值"
        finally:
            release.set()
            for task in (first_task, other_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_same_creation_intent_is_scoped_to_user(postgres_collaboration_apps):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as a,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as b,
    ):
        first_user = await register(a, "pg-template-user-a")
        second_user = await register(b, "pg-template-user-b")
        first_source = await create_project(a, style="")
        second_source = await create_project(b, style="")
        first_template = await _save(a, first_source["id"])
        second_template = await _save(b, second_source["id"])
        key = str(uuid4())
        first, second = await asyncio.gather(*(
            client.post(f"{BASE}/{template['id']}/projects", json={
                "expected_revision": 1, "intent_key": key, "title": "同意图不同用户",
            }) for client, template in ((a, first_template), (b, second_template))
        ))
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert first.json()["id"] != second.json()["id"]
        async with app1.state.sessionmaker() as db:
            receipts = (await db.scalars(select(NovelTemplateCreationReceipt).where(
                NovelTemplateCreationReceipt.intent_key == key,
            ))).all()
            assert {receipt.owner_id for receipt in receipts} == {first_user["id"], second_user["id"]}


@pytest.mark.parametrize("first", ["save", "recycle"])
async def test_source_recycle_and_template_save_serialize(postgres_collaboration_apps, monkeypatch, first):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        await register(owner, "pg-template-recycle")
        await _login(peer, "pg-template-recycle")
        source = await create_project(owner, style="")
        base = f"/api/v1/projects/{source['id']}"
        selection = {"include_config": True}
        preview = await owner.post(f"{base}/novel-template-preview", json=selection)
        assert preview.status_code == 200, preview.text
        body = {"name": "保存中的来源", "source_project_id": source["id"], "selection": selection,
                "source_stamp": preview.json()["source_stamp"]}
        reached, release = asyncio.Event(), asyncio.Event()
        holder: dict[str, int] = {}
        first_task = None
        original_capture = novel_template_service._capture_for_save
        original_lock = project_lifecycle.lock_record

        async def hold_capture(db, project, selection, stamp):
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return await original_capture(db, project, selection, stamp)

        async def hold_recycle(db, project_id, *, active_only=False):
            project = await original_lock(db, project_id, active_only=active_only)
            if project_id == source["id"] and asyncio.current_task() is first_task:
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return project

        if first == "save":
            monkeypatch.setattr(novel_template_service, "_capture_for_save", hold_capture)
        else:
            monkeypatch.setattr(project_lifecycle, "lock_record", hold_recycle)
        first_task = asyncio.create_task(owner.post(BASE, json=body) if first == "save" else peer.delete(base))
        other_task = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            other_task = asyncio.create_task(peer.delete(base) if first == "save" else owner.post(BASE, json=body))
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            first_result, other_result = await asyncio.wait_for(asyncio.gather(first_task, other_task), 15)
            saved, recycled = (first_result, other_result) if first == "save" else (other_result, first_result)
            assert recycled.status_code == 204, recycled.text
            if first == "save":
                assert saved.status_code == 201, saved.text
            else:
                assert saved.status_code in (404, 409), saved.text
                assert (await owner.get(BASE)).json()["total"] == 0
        finally:
            release.set()
            for task in (first_task, other_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_template_revision_cas_under_concurrent_updates(postgres_collaboration_apps, monkeypatch):
    app1, app2 = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app1), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://testserver") as peer,
    ):
        await register(owner, "pg-template-cas")
        await _login(peer, "pg-template-cas")
        source = await create_project(owner, style="")
        template = await _save(owner, source["id"])
        path = f"{BASE}/{template['id']}"
        reached, release = asyncio.Event(), asyncio.Event()
        holder: dict[str, int] = {}
        original = novel_template_service._owned
        first_task = None

        async def hold_first(db, owner_id, template_id, *, lock=False):
            row = await original(db, owner_id, template_id, lock=lock)
            if lock and asyncio.current_task() is first_task:
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return row

        monkeypatch.setattr(novel_template_service, "_owned", hold_first)
        first_task = asyncio.create_task(owner.patch(path, json={"expected_revision": 1, "name": "先提交"}))
        second_task = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            second_task = asyncio.create_task(peer.patch(path, json={"expected_revision": 1, "name": "过期修改"}))
            await asyncio.wait_for(_wait_for_blocker(app2.state.sessionmaker, holder["pid"]), 8)
            release.set()
            updated, stale = await asyncio.wait_for(asyncio.gather(first_task, second_task), 15)
            assert updated.status_code == 200 and updated.json()["revision"] == 2
            assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
            current = await owner.get(path)
            assert current.status_code == 200 and current.json()["name"] == "先提交"
        finally:
            release.set()
            for task in (first_task, second_task):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
