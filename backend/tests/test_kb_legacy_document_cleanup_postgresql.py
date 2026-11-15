"""E4-e 真实 PostgreSQL：旧无占用文档任务清理与新受理（重新索引）、引擎领取的提交顺序。

两个应用实例共享同一隔离 schema；不论哪一方先持锁，最终只有一次清理生效，文档要么 failed 无占用，
要么被新受理的任务以完整三列占用绑定，绝不会出现无主 pending 或旧任务被执行。
"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import select, text

from app.models import KbChunk, KbDocument, Task, User
from app.services import kb_index_service, kb_service, task_statistics_service
from tests.helpers import create_project, register, task_params
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_legacy_apps = _postgres_fixture


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _seed(app, client):
    user = await register(client, "pg-legacy-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        document = KbDocument(project_id=project["id"], filename="历史.md", mime="text/markdown", sha256="0" * 64,
                              text="旧文档正文", status="pending", chunk_count=1)
        db.add(document)
        await db.flush()
        db.add(KbChunk(document_id=document.id, seq=0, content="旧分块正文"))
        token_version = (await db.get(User, user["id"])).token_version
        task = Task(user_id=user["id"], project_id=project["id"], kind="kb.index", status="queued",
                    resource_key=f"kbdoc:{document.id}", serial_key=kb_index_service.serial_key(project["id"]),
                    input={"request": {"project_id": project["id"], "mode": "document",
                                       "document_id": document.id}, "_task_actor_token_version": token_version})
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        return {"project": project, "document_id": document.id, "task_id": task.id,
                "task_key": task.statistics_key, "queued_at": task.queued_at}


def _ref(legacy):
    return {"id": legacy["task_id"], "task_key": legacy["task_key"]}


async def _rows(maker, legacy):
    async with maker() as db:
        task = await db.get(Task, legacy["task_id"])
        doc = await db.get(KbDocument, legacy["document_id"])
        chunks = [c.content for c in await db.scalars(select(KbChunk).where(
            KbChunk.document_id == legacy["document_id"]))]
        return task, doc, chunks


def _gate_cleanup(monkeypatch, status):
    """在清理两次 CAS 完成、提交之前停住指定调用方，暴露其持锁的后端 pid。"""
    reached, release, holder = asyncio.Event(), asyncio.Event(), {}
    original = kb_index_service.cleanup_legacy_unclaimed_document_task

    async def gated(db, legacy, *, status: str):
        result = await original(db, legacy, status=status)
        if status == wanted and not reached.is_set():
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
        return result

    wanted = status
    monkeypatch.setattr(kb_index_service, "cleanup_legacy_unclaimed_document_task", gated)
    return reached, release, holder


async def _claim(app, legacy):
    engine = app.state.task_engine
    engine._stopping = False
    engine._running_rounds[legacy["task_id"]] = legacy["queued_at"]
    try:
        await engine._claim_and_run(legacy["task_id"], legacy["task_key"])
    finally:
        engine._running_rounds.pop(legacy["task_id"], None)
        engine._stopping = True


async def _cancel(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def test_postgres_cleanup_commits_before_waiting_reindex(postgres_legacy_apps, monkeypatch):
    first, second = postgres_legacy_apps
    async with _client(first) as owner, _client(second) as other:
        legacy = await _seed(first, owner)
        await other.post("/api/v1/auth/login", json={"username": "pg-legacy-owner", "password": "secret123"})
        maker = first.state.sessionmaker
        reached, release, holder = _gate_cleanup(monkeypatch, "cancelled")
        cancel = asyncio.create_task(owner.post(f"/api/v1/tasks/{legacy['task_id']}/cancel",
                                                params=task_params(_ref(legacy))))
        reindex = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            pid = legacy["project"]["id"]
            reindex = asyncio.create_task(other.post(
                f"/api/v1/projects/{pid}/kb/documents/{legacy['document_id']}/reindex"))
            # 清理持锁未提交：新受理必须等待，不能看到旧任务仍排队就清空分块或抢占文档。
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            release.set()
            cancelled = await asyncio.wait_for(cancel, 10)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
            admitted = await asyncio.wait_for(reindex, 10)
            assert admitted.status_code == 202, admitted.text
            task, doc, _ = await _rows(maker, legacy)
            assert task.status == "cancelled" and task.error == kb_index_service.LEGACY_TASK_MESSAGES["cancelled"]
            async with maker() as db:
                new_task = await db.get(Task, admitted.json()["id"])
            assert (doc.status, doc.index_task_key) == ("pending", new_task.statistics_key) and doc.index_claim_key
            assert doc.index_task_queued_at == new_task.queued_at
        finally:
            release.set()
            await _cancel(cancel, reindex)


async def test_postgres_reindex_holds_lock_before_waiting_cleanup(postgres_legacy_apps, monkeypatch):
    first, second = postgres_legacy_apps
    async with _client(first) as owner, _client(second) as other:
        legacy = await _seed(first, owner)
        await other.post("/api/v1/auth/login", json={"username": "pg-legacy-owner", "password": "secret123"})
        maker = first.state.sessionmaker
        reached, release, holder = asyncio.Event(), asyncio.Event(), {}
        original = kb_service._active_doc_task

        async def gated(db, document_id):
            found = await original(db, document_id)
            if not reached.is_set():
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return found

        monkeypatch.setattr(kb_service, "_active_doc_task", gated)
        pid = legacy["project"]["id"]
        reindex = asyncio.create_task(other.post(
            f"/api/v1/projects/{pid}/kb/documents/{legacy['document_id']}/reindex"))
        cancel = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            cancel = asyncio.create_task(owner.post(f"/api/v1/tasks/{legacy['task_id']}/cancel",
                                                    params=task_params(_ref(legacy))))
            # 新受理已持作品锁且看到旧任务仍占用资源：清理须等待；受理随后 409 且不动分块。
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            release.set()
            admitted = await asyncio.wait_for(reindex, 10)
            assert admitted.status_code == 409, admitted.text
            cancelled = await asyncio.wait_for(cancel, 10)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
            task, doc, chunks = await _rows(maker, legacy)
            assert task.status == "cancelled"
            assert (doc.status, doc.index_task_key, doc.index_claim_key) == ("failed", None, None)
            assert chunks == ["旧分块正文"] and doc.text == "旧文档正文"
            again = await other.post(f"/api/v1/projects/{pid}/kb/documents/{legacy['document_id']}/reindex")
            assert again.status_code == 202, again.text
        finally:
            release.set()
            await _cancel(cancel, reindex)


async def test_postgres_user_cleanup_before_waiting_engine_claim(postgres_legacy_apps, monkeypatch):
    first, second = postgres_legacy_apps
    async with _client(first) as owner:
        legacy = await _seed(first, owner)
        maker = first.state.sessionmaker
        reached, release, holder = _gate_cleanup(monkeypatch, "cancelled")
        cancel = asyncio.create_task(owner.post(f"/api/v1/tasks/{legacy['task_id']}/cancel",
                                                params=task_params(_ref(legacy))))
        claim = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            claim = asyncio.create_task(_claim(second, legacy))
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            release.set()
            assert (await asyncio.wait_for(cancel, 10)).status_code == 200
            await asyncio.wait_for(claim, 10)
            task, doc, _ = await _rows(maker, legacy)
            # 引擎在锁后看到已取消的任务，不再清理或执行。
            assert task.status == "cancelled" and task.error == kb_index_service.LEGACY_TASK_MESSAGES["cancelled"]
            assert task.execution_key is None and task.started_at is None
            assert (doc.status, doc.index_claim_key) == ("failed", None)
        finally:
            release.set()
            await _cancel(cancel, claim)


async def test_postgres_engine_cleanup_before_waiting_user_cancel(postgres_legacy_apps, monkeypatch):
    first, second = postgres_legacy_apps
    async with _client(first) as owner:
        legacy = await _seed(first, owner)
        maker = first.state.sessionmaker
        reached, release, holder = _gate_cleanup(monkeypatch, "failed")
        claim = asyncio.create_task(_claim(second, legacy))
        cancel = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            cancel = asyncio.create_task(owner.post(f"/api/v1/tasks/{legacy['task_id']}/cancel",
                                                    params=task_params(_ref(legacy))))
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            release.set()
            await asyncio.wait_for(claim, 10)
            cancelled = await asyncio.wait_for(cancel, 10)
            assert cancelled.status_code == 409, cancelled.text
            task, doc, chunks = await _rows(maker, legacy)
            assert task.status == "failed" and task.error == kb_index_service.LEGACY_TASK_MESSAGES["failed"]
            assert task.execution_key is None and task.started_at is None
            assert (doc.status, doc.index_claim_key, chunks) == ("failed", None, ["旧分块正文"])
        finally:
            release.set()
            await _cancel(claim, cancel)
