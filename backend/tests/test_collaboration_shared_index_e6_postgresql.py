"""协作关键词写入与返回在真实 PostgreSQL 跨连接撤权时保持原授权。"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import func, select, text

from app.models import KbChunk, KbDocument, Project, RetrievalChunk, Task
from app.services import collaboration_service, kb_index_service, kb_service, project_access, retrieval_service
from tests.test_collaboration_access_postgresql import _assert_project_row_locked, _setup, _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_revoke_commits_before_keyword_upload(postgres_collaboration_apps, monkeypatch, fake_llm):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = collaboration_service.add_success

        async def gated(db, snapshot):
            event = await original(db, snapshot)
            if snapshot.action == "revoke":
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return event

        monkeypatch.setattr(collaboration_service, "add_success", gated)
        revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
            "expected_revision": 2,
        }))
        upload = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            await _assert_project_row_locked(guest_app.state.sessionmaker, project["id"])
            upload = asyncio.create_task(guest.post(f"{base}/kb/documents", files={"file": ("晚到.txt", b"late")}))
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
            assert (await asyncio.wait_for(upload, 8)).status_code == 404
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(KbDocument.id))) == 0
                assert await db.scalar(select(func.count(RetrievalChunk.id))) == 0
                assert await db.scalar(select(func.count(Task.id))) == 0
            assert not fake_llm.embed_calls
        finally:
            release.set()
            for task in (revoke, upload):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_keyword_upload_commits_before_revoke_but_rechecks_response(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        written, release_write = asyncio.Event(), asyncio.Event()
        final_check, release_final = asyncio.Event(), asyncio.Event()
        holder = {}
        original_chunk, original_lock = kb_index_service.chunk_document, kb_service.lock_content_write_actor
        checks = 0

        async def gated_chunk(db, document):
            count = await original_chunk(db, document)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            written.set()
            await release_write.wait()
            return count

        async def gated_lock(db, identity, **kwargs):
            nonlocal checks
            checks += 1
            if checks == 2:
                final_check.set()
                await release_final.wait()
            return await original_lock(db, identity, **kwargs)

        monkeypatch.setattr(kb_index_service, "chunk_document", gated_chunk)
        monkeypatch.setattr(kb_service, "lock_content_write_actor", gated_lock)
        upload = asyncio.create_task(guest.post(
            f"{base}/kb/documents", files={"file": ("先提交.txt", "先提交的关键词".encode())},
        ))
        revoke = None
        try:
            await asyncio.wait_for(written.wait(), 8)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project["id"])
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": 2,
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 8)
            release_write.set()
            await asyncio.wait_for(final_check.wait(), 8)
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
            release_final.set()
            assert (await asyncio.wait_for(upload, 8)).status_code == 404
            async with owner_app.state.sessionmaker() as db:
                document = await db.scalar(select(KbDocument))
                assert document.status == "ready_keyword_only" and document.chunk_count > 0
                indexed = (await db.scalars(select(RetrievalChunk))).all()
                assert indexed and all(row.embedding is None and row.embedding_key is None for row in indexed)
                assert await db.scalar(select(func.count(Task.id))) == 0
            assert not fake_llm.embed_calls
        finally:
            release_write.set()
            release_final.set()
            for task in (upload, revoke):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


async def test_owner_search_rechecks_source_after_concurrent_edit(postgres_collaboration_apps, monkeypatch, fake_llm):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _, base = await _setup(owner, guest)
        async with owner_app.state.sessionmaker() as db:
            doc = KbDocument(project_id=project["id"], filename="资料.txt", sha256="c" * 64,
                             text="旧检索正文", status="ready_keyword_only", chunk_count=1)
            db.add(doc)
            await db.flush()
            chunk = KbChunk(document_id=doc.id, seq=1, content=doc.text)
            db.add(chunk)
            await db.flush()
            await retrieval_service.upsert_project_sources(db, project["id"], "kb", [
                retrieval_service.ChunkInput(ref_id=chunk.id, text=chunk.content),
            ])
            doc_id, chunk_id = doc.id, chunk.id
            await db.commit()
        checked, release = asyncio.Event(), asyncio.Event()
        original = kb_service._lock_search_result

        async def gated(db, user_id, project_id, grant):
            checked.set()
            await release.wait()
            return await original(db, user_id, project_id, grant)

        monkeypatch.setattr(kb_service, "_lock_search_result", gated)
        search = asyncio.create_task(owner.post(f"{base}/kb/search", json={"query": "旧检索正文", "min_score": 0}))
        try:
            await asyncio.wait_for(checked.wait(), 8)
            async with guest_app.state.sessionmaker() as db:
                owner_id = await db.scalar(select(Project.user_id).where(Project.id == project["id"]))
                await project_access.require_project(db, owner_id, project["id"], "content.edit", for_update=True)
                doc, chunk = await db.get(KbDocument, doc_id), await db.get(KbChunk, chunk_id)
                doc.text = chunk.content = "新检索正文"
                await db.flush()
                await retrieval_service.upsert_project_sources(db, project["id"], "kb", [
                    retrieval_service.ChunkInput(ref_id=chunk_id, text=chunk.content),
                ])
                await db.commit()
            release.set()
            response = await asyncio.wait_for(search, 8)
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "retrieval_reference_invalid"
            assert "旧检索正文" not in response.text and "新检索正文" not in response.text
            assert not fake_llm.embed_calls
        finally:
            release.set()
            if not search.done():
                search.cancel()
                with suppress(asyncio.CancelledError):
                    await search


async def test_keyword_search_finishes_under_original_grant_before_waiting_revoke(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        uploaded = await guest.post(f"{base}/kb/documents", files={"file": ("查询.txt", "协作检索正文".encode())})
        assert uploaded.status_code == 202, uploaded.text
        assert uploaded.json()["task"] is None
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = kb_service.verify_retrieval_hits

        async def gated(db, project_id, source, result, **kwargs):
            await original(db, project_id, source, result, **kwargs)
            if source == "kb" and not reached.is_set():
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()

        monkeypatch.setattr(kb_service, "verify_retrieval_hits", gated)
        search = asyncio.create_task(guest.post(f"{base}/kb/search", json={"query": "协作检索", "min_score": 0}))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project["id"])
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": 2,
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            response = await asyncio.wait_for(search, 8)
            assert response.status_code == 200, response.text
            assert response.json()["mode"] == "keyword"
            assert any("协作检索正文" in hit["content"] for hit in response.json()["hits"])
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
            assert (await guest.post(f"{base}/kb/search", json={"query": "协作检索"})).status_code == 404
            assert not fake_llm.embed_calls
        finally:
            release.set()
            for task in (search, revoke):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
