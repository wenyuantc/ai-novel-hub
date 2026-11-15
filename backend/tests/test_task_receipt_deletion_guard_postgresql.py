"""跨连接提交收据与账号/作品删除竞争，仅允许隔离 PostgreSQL 专库。"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text, update

from app.core.errors import AppError
from app.models import AgentChange, AgentConversation, KbDocument, Project, Task, User
from app.services import admin_service, authorization_service, project_access, project_lifecycle, recycle_bin_service
from app.services.authorization_service import AuthenticatedActor
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_user_deletion_postgresql import _prepare

pytest_plugins = ("tests.test_collaboration_user_deletion_postgresql",)


@pytest.mark.parametrize("document_status", ["pending", "ready_keyword_only"])
async def test_unproven_pending_document_blocks_cross_connection_project_delete(
    postgres_user_delete_apps, document_status,
):
    # E4-e：旧无占用文档任务在文档仍 pending 时可证明无执行体，删除排空经受限清理后可删除；
    # 文档已非 pending 时无法证明，仍以 project_tasks_not_stopped 失败关闭。
    provable = document_status == "pending"
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        deletion = None
        async with guest_app.state.sessionmaker() as writer:
            await project_lifecycle.lock_record(writer, project["id"])
            document = KbDocument(
                project_id=project["id"], filename="历史.md", mime="text/markdown", sha256="0" * 64,
                text="仍需保留的设定", status=document_status,
            )
            writer.add(document)
            await writer.flush()
            task = Task(
                user_id=user["id"], project_id=project["id"], kind="kb.index", status="queued",
                resource_key=f"kbdoc:{document.id}",
                input={"request": {"project_id": project["id"], "mode": "document", "document_id": document.id}},
            )
            writer.add(task)
            await writer.flush()
            document_id, task_id, task_key = document.id, task.id, task.statistics_key
            holder = await writer.scalar(text("SELECT pg_backend_pid()"))
            deletion = asyncio.create_task(owner.delete(f"/api/v1/projects/{project['id']}"))
            try:
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder), 15)
                assert not deletion.done()
                await writer.commit()
                response = await asyncio.wait_for(deletion, 15)
            finally:
                if not deletion.done():
                    deletion.cancel()
                    with suppress(asyncio.CancelledError):
                        await deletion

        if provable:
            assert response.status_code == 204, response.text
        else:
            assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", \
                response.text
        async with owner_app.state.sessionmaker() as verify:
            saved_project = await verify.get(Project, project["id"])
            saved_document, saved_task = await verify.get(KbDocument, document_id), await verify.get(Task, task_id)
            assert saved_project.deletion_started_at is None and saved_project.deletion_token is None
            assert saved_document.text == "仍需保留的设定"
            assert saved_document.index_task_key is None and saved_document.index_claim_key is None
            assert saved_task.statistics_key == task_key and saved_task.resource_key == f"kbdoc:{document_id}"
            if provable:
                assert saved_project.deleted_at is not None
                assert (saved_document.status, saved_task.status) == ("failed", "cancelled")
            else:
                assert saved_project.deleted_at is None
                assert (saved_document.status, saved_task.status) == ("ready_keyword_only", "queued")


@pytest.mark.parametrize("target", ["user", "project", "purge"])
@pytest.mark.parametrize("marker", [
    "analysis_pending", "external_execution", "external_snapshot_mac", "external_snapshot_mac_task",
    "batch_outcome_unknown",
])
async def test_receipt_commits_before_waiting_lifecycle_delete(postgres_user_delete_apps, target, marker):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        chapter = await owner.post(
            f"/api/v1/projects/{project['id']}/chapters", json={"title": "章", "content": "原稿"},
        )
        assert chapter.status_code == 201, chapter.text
        stamp = datetime.now(UTC)
        async with owner_app.state.sessionmaker() as db:
            task = Task(
                user_id=user["id"], project_id=project["id"], chapter_id=chapter.json()["id"],
                kind={"analysis_pending": "chapter.analyze", "external_execution": "agent.turn",
                      "external_snapshot_mac": "agent.turn", "external_snapshot_mac_task": "agent.turn",
                      "batch_outcome_unknown": "chapter.batch"}[marker],
                status="failed", checkpoint=None,
            )
            db.add(task)
            change = None
            if marker in ("external_execution", "external_snapshot_mac"):
                conversation = AgentConversation(user_id=user["id"], project_id=project["id"])
                db.add(conversation)
                await db.flush()
                change = AgentChange(
                    project_id=project["id"], conversation_id=conversation.id, turn_task_id=task.id,
                    turn_task_key=task.statistics_key, tool_call_id="external-receipt", tool_name="external",
                    kind="external", status="failed", arguments={}, result={"error": "明确失败"},
                )
                db.add(change)
            if target == "purge":
                (await db.get(Project, project["id"])).deleted_at = stamp
            await db.commit()
            task_id, task_key = task.id, task.statistics_key
            change_id = change.id if change is not None else None

        # 模拟系统收据写入的 User → Project → Task 锁顺序；并非开放协作者模型任务受理。
        async with guest_app.state.sessionmaker() as writer:
            writer.info["authenticated_actor"] = AuthenticatedActor(user["id"], 0)
            if target == "purge":
                await authorization_service.require_authenticated(writer, user["id"], write=True)
                await project_lifecycle.lock_record(writer, project["id"])
            else:
                await project_access.require_project(writer, user["id"], project["id"], "ai.use", for_update=True)
            change_marker = marker in ("external_execution", "external_snapshot_mac")
            receipt_key = "_external_snapshot_mac" if marker == "external_snapshot_mac_task" else f"_{marker}"
            values = {"id": task_id} if change_marker else {"checkpoint": {receipt_key: None}}
            changed = await writer.execute(update(Task).where(
                Task.id == task_id, Task.statistics_key == task_key,
            ).values(**values))
            assert changed.rowcount == 1
            if change_marker:
                changed = await writer.execute(update(AgentChange).where(AgentChange.id == change_id).values(
                    result={"error": "明确失败", f"_{marker}": None},
                ))
                assert changed.rowcount == 1
            holder = await writer.scalar(text("SELECT pg_backend_pid()"))
            if target == "user":
                request = admin.delete(f"/api/v1/admin/users/{user['id']}")
            elif target == "project":
                request = owner.delete(f"/api/v1/projects/{project['id']}")
            else:
                request = admin.post(
                    f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge",
                    json={"deleted_at": stamp.isoformat(), "title": project["title"], "confirm": True},
                )
            deletion = asyncio.create_task(request)
            try:
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder), 15)
                await writer.commit()
                response = await asyncio.wait_for(deletion, 15)
            finally:
                if not deletion.done():
                    deletion.cancel()
                    with suppress(asyncio.CancelledError):
                        await deletion
        expected = "user_has_pending_tasks" if target == "user" else "project_tasks_not_stopped"
        assert response.status_code == 409 and response.json()["code"] == expected, response.text
        assert "不能泄露的作品" not in response.text
        async with owner_app.state.sessionmaker() as verify:
            assert await verify.get(User, user["id"]) is not None
            assert await verify.get(Project, project["id"]) is not None
            task = await verify.get(Task, task_id)
            assert task is not None and task.statistics_key == task_key
            if change_marker:
                change = await verify.get(AgentChange, change_id)
                assert change is not None and change.result == {"error": "明确失败", f"_{marker}": None}
            else:
                assert task.checkpoint == {receipt_key: None}


@pytest.mark.parametrize("target", ["user", "project"])
async def test_lifecycle_delete_commits_before_waiting_receipt_writer(postgres_user_delete_apps, monkeypatch, target):
    owner_app, guest_app = postgres_user_delete_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as admin,
    ):
        user, project, _ = await _prepare(owner, guest, admin, owner_app)
        async with owner_app.state.sessionmaker() as db:
            task = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="succeeded")
            db.add(task)
            await db.commit()
            task_id, task_key = task.id, task.statistics_key

        entered, release = asyncio.Event(), asyncio.Event()
        holder = None
        if target == "user":
            original_flush = admin_service._flush_user_delete

            async def pause_flush(db):
                nonlocal holder
                holder = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
                await release.wait()
                await original_flush(db)

            monkeypatch.setattr(admin_service, "_flush_user_delete", pause_flush)
            deletion = asyncio.create_task(admin.delete(f"/api/v1/admin/users/{user['id']}"))
        else:
            original_idle = recycle_bin_service._ensure_idle

            async def pause_idle(db, engine, project_id):
                nonlocal holder
                await original_idle(db, engine, project_id)
                holder = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
                await release.wait()

            monkeypatch.setattr(recycle_bin_service, "_ensure_idle", pause_idle)
            deletion = asyncio.create_task(owner.delete(f"/api/v1/projects/{project['id']}"))

        async def write_receipt():
            async with guest_app.state.sessionmaker() as db:
                db.info["authenticated_actor"] = AuthenticatedActor(user["id"], 0)
                try:
                    await project_access.require_project(db, user["id"], project["id"], "ai.use", for_update=True)
                    await db.execute(update(Task).where(
                        Task.id == task_id, Task.statistics_key == task_key,
                    ).values(checkpoint={"_analysis_pending": None}))
                    await db.commit()
                    return True
                except AppError:
                    await db.rollback()
                    return False

        writer = None
        try:
            await asyncio.wait_for(entered.wait(), 15)
            writer = asyncio.create_task(write_receipt())
            assert holder is not None
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder), 15)
            release.set()
            response = await asyncio.wait_for(deletion, 15)
            assert response.status_code == 204, response.text
            assert not await asyncio.wait_for(writer, 15)
        finally:
            release.set()
            for pending in (writer, deletion):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
        async with owner_app.state.sessionmaker() as db:
            saved = await db.get(Task, task_id)
            if target == "user":
                assert saved is None and await db.get(User, user["id"]) is None
            else:
                assert saved is not None and saved.statistics_key == task_key and saved.checkpoint is None
                assert (await db.get(Project, project["id"])).deleted_at is not None
