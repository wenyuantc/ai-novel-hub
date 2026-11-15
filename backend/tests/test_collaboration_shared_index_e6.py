"""协作者知识库的真实 HTTP 关键词路径和共享向量隔离。"""

import asyncio
import socket
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select

from app.ai.providers import create_provider
from app.models import Chapter, ChapterAnalysis, KbChunk, KbDocument, RetrievalChunk, StoryMemory, Task, User
from app.services import kb_service, memory_service, retrieval_service
from tests.helpers import create_ai_config, create_project, register, wait_until
from tests.test_ai_visibility_dispatch import _server


@asynccontextmanager
async def _real_clients(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started, within=10)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        async with (
            httpx.AsyncClient(base_url=url, timeout=10) as owner,
            httpx.AsyncClient(base_url=url, timeout=10) as editor,
        ):
            yield owner, editor
    finally:
        server.should_exit = True
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(runner, 10)
        sock.close()


async def _world(app, owner, editor, provider_url):
    await app.state.task_engine.stop()
    app.state.provider_factory = create_provider
    app.state.settings.allow_private_ai_endpoints = True
    await register(owner, "e6-owner")
    editor_user = await register(editor, "e6-editor")
    project = await create_project(owner)
    foreign = await create_project(owner, title="其他作品")
    base = f"/api/v1/projects/{project['id']}"
    invited = await owner.post(f"{base}/collaborators", json={"username": "e6-editor", "role": "editor"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    for actor, model in ((owner, "owner-embedding"), (editor, "editor-embedding")):
        await create_ai_config(actor, name=model, model=model, model_type="embedding", base_url=f"{provider_url}/v1")

    async with app.state.sessionmaker() as db:
        docs = [
            KbDocument(project_id=project["id"], filename="原有文档.txt", sha256="a" * 64,
                       text="旧有向量保留", status="ready", chunk_count=1),
            KbDocument(project_id=foreign["id"], filename="秘密.txt", sha256="b" * 64,
                       text="跨书秘密正文", status="ready", chunk_count=1),
        ]
        db.add_all(docs)
        await db.flush()
        chunks = [KbChunk(document_id=doc.id, seq=1, content=doc.text) for doc in docs]
        db.add_all(chunks)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project["id"], "kb", [
            retrieval_service.ChunkInput(ref_id=chunks[0].id, text=chunks[0].content),
        ])
        indexed = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.ref_id == chunks[0].id))
        indexed.embedding = b"\x00\x00\x80?\x00\x00\x00\x00"
        indexed.embedding_key = "openai_compat:owner-embedding:2"
        await db.commit()
        return {"project": project["id"], "foreign": foreign["id"], "editor": editor_user["id"],
                "member": accepted.json(), "doc": docs[0].id, "chunk": chunks[0].id,
                "foreign_chunk": chunks[1].id, "index": indexed.id, "base": base}


async def test_editor_without_owner_authorization_uploads_and_searches_keywords_only(app):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            base = world["base"] + "/kb"
            response = await editor.post(f"{base}/documents", files={"file": ("新文档.txt", "新知识天剑宗".encode())})
            assert response.status_code == 202, response.text
            uploaded = response.json()
            assert uploaded["task"] is None
            assert uploaded["document"]["status"] == "ready_keyword_only"
            assert uploaded["document"]["chunk_count"] > 0
            assert any("关键词" in warning for warning in uploaded["document"]["warnings"])
            found = await editor.post(f"{base}/search", json={"query": "天剑宗", "min_score": 0})
            assert found.status_code == 200, found.text
            assert found.json()["mode"] == "keyword"
            assert any("新知识天剑宗" in hit["content"] for hit in found.json()["hits"])
            for path in (f"{base}/documents/{world['doc']}/reindex", f"{base}/rebuild"):
                rejected = await editor.post(path)
                assert rejected.status_code == 403, rejected.text
                assert rejected.json()["code"] == "owner_embedding_not_allowed"
            assert requests == []
            async with app.state.sessionmaker() as db:
                old = await db.get(RetrievalChunk, world["index"])
                assert old.embedding == b"\x00\x00\x80?\x00\x00\x00\x00"
                assert old.embedding_key == "openai_compat:owner-embedding:2"
                rows = (await db.scalars(select(RetrievalChunk).where(
                    RetrievalChunk.project_id == world["project"], RetrievalChunk.ref_id != world["chunk"],
                ))).all()
                assert rows and all(row.embedding is None and row.embedding_key is None for row in rows)
                assert await db.scalar(select(func.count(Task.id))) == 0


@pytest.mark.parametrize("damage", ["foreign", "text"])
async def test_keyword_search_rejects_unverified_source_or_text(app, damage):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            async with app.state.sessionmaker() as db:
                if damage == "foreign":
                    await retrieval_service.upsert_many(db, world["project"], "kb", [
                        retrieval_service.ChunkInput(ref_id=world["foreign_chunk"], text="跨书秘密正文"),
                    ])
                    query = "跨书秘密"
                else:
                    index = await db.get(RetrievalChunk, world["index"])
                    index.text = "伪造正文泄漏"
                    index.tokens = retrieval_service.term_freq(index.text)
                    index.length = len(retrieval_service.tokenize(index.text))
                    index.version += 1
                    query = "伪造正文"
                await db.commit()
            response = await editor.post(world["base"] + "/kb/search", json={"query": query, "min_score": 0})
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "retrieval_reference_invalid"
            assert "秘密" not in response.text and "伪造" not in response.text
            assert requests == []


async def test_authorized_editor_upload_queues_owner_vector_task_as_editor(app):
    """E6-c 翻转（原「授权编辑者上传 409」）：已授权编辑者上传 → 建归编辑者的 kb.index 任务，
    冻结原邀请轮次与作者向量选择（引擎已停，无请求）；不调用编辑者个人向量模型。"""
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            changed = await owner.patch(f"{world['base']}/collaborators/{world['editor']}", json={
                "expected_revision": world["member"]["revision"], "allow_owner_ai": True,
            })
            assert changed.status_code == 200, changed.text
            response = await editor.post(world["base"] + "/kb/documents", files={"file": ("授权.txt", b"new text")})
            assert response.status_code == 202, response.text
            uploaded = response.json()
            assert uploaded["task"] is not None and uploaded["document"]["status"] == "pending"
            for secret in ("_shared_embedding", "_collaboration_grant", "owner-embedding", "editor-embedding"):
                assert secret not in response.text
            assert requests == []
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, uploaded["task"]["id"])
                assert task.user_id == world["editor"] and task.status == "queued"
                assert task.input["_collaboration_grant"]["revision"] == changed.json()["revision"]
                selection = task.input["_shared_embedding"]
                assert selection["mode"] == "vector"
                document = await db.get(KbDocument, uploaded["document"]["id"])
                assert document.index_task_key == task.statistics_key


async def test_editor_memory_edit_preserves_other_author_vectors_and_searches_keywords(app):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            async with app.state.sessionmaker() as db:
                memories = [StoryMemory(project_id=world["project"], chapter_no=1, kind="manual", origin="manual",
                                        content=text) for text in ("原本记忆", "不变记忆")]
                db.add_all(memories)
                await db.flush()
                await retrieval_service.upsert_project_sources(db, world["project"], "memory", [
                    memory_service._chunk(item) for item in memories
                ])
                for item in memories:
                    index = await db.scalar(select(RetrievalChunk).where(
                        RetrievalChunk.source == "memory", RetrievalChunk.ref_id == item.id,
                    ))
                    index.embedding = b"\x00\x00\x80?\x00\x00\x00\x00"
                    index.embedding_key = "openai_compat:owner-embedding:2"
                ids = [item.id for item in memories]
                await db.commit()
            response = await editor.patch(f"{world['base']}/memories/{ids[0]}", json={"content": "修订天剑宗记忆"})
            assert response.status_code == 200, response.text
            found = await editor.post(f"{world['base']}/memories/search", json={"query": "天剑宗", "min_score": 0})
            assert found.status_code == 200, found.text
            assert found.json()["mode"] == "keyword"
            assert any(hit["memory"]["id"] == ids[0] for hit in found.json()["hits"])
            assert requests == []
            async with app.state.sessionmaker() as db:
                rows = {row.ref_id: row for row in (await db.scalars(select(RetrievalChunk).where(
                    RetrievalChunk.source == "memory", RetrievalChunk.ref_id.in_(ids),
                ))).all()}
                assert rows[ids[0]].embedding is None and rows[ids[0]].embedding_key is None
                assert rows[ids[1]].embedding == b"\x00\x00\x80?\x00\x00\x00\x00"
                assert rows[ids[1]].embedding_key == "openai_compat:owner-embedding:2"
                assert await db.scalar(select(func.count(Task.id))) == 0


async def test_editor_keyword_search_never_uses_matching_personal_vector_model(app):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            async with app.state.sessionmaker() as db:
                row = await db.get(RetrievalChunk, world["index"])
                row.embedding_key = "openai_compat:editor-embedding:2"
                row.version += 1
                await db.commit()
            response = await editor.post(
                f"{world['base']}/kb/search", json={"query": "旧有向量保留", "min_score": 0},
            )
            assert response.status_code == 200, response.text
            assert response.json()["mode"] == "keyword" and response.json()["hits"]
            assert requests == []
            async with app.state.sessionmaker() as db:
                row = await db.get(RetrievalChunk, world["index"])
                assert row.embedding == b"\x00\x00\x80?\x00\x00\x00\x00"
                assert row.embedding_key == "openai_compat:editor-embedding:2"


async def test_keyword_upload_rechecks_original_token_after_committed_write(app, monkeypatch):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            original = kb_service.lock_content_write_actor
            checks = 0

            async def revoke_token_before_return(db, identity, **kwargs):
                nonlocal checks
                checks += 1
                if checks == 2:
                    async with app.state.sessionmaker() as other:
                        user = await other.get(User, identity.actor.id)
                        user.token_version += 1
                        await other.commit()
                return await original(db, identity, **kwargs)

            monkeypatch.setattr(kb_service, "lock_content_write_actor", revoke_token_before_return)
            response = await editor.post(
                f"{world['base']}/kb/documents", files={"file": ("已保存.txt", "提交后的资料".encode())},
            )
            assert response.status_code == 401, response.text
            assert "提交后的资料" not in response.text
            assert requests == []
            async with app.state.sessionmaker() as db:
                document = await db.scalar(select(KbDocument).where(KbDocument.filename == "已保存.txt"))
                assert document is not None and document.status == "ready_keyword_only" and document.chunk_count > 0
                assert await db.scalar(select(func.count(Task.id))) == 0


async def test_editor_memory_search_rejects_foreign_analysis_reference(app):
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            async with app.state.sessionmaker() as db:
                chapter = Chapter(project_id=world["foreign"], chapter_no=1, title="他书章节")
                db.add(chapter)
                await db.flush()
                analysis = ChapterAnalysis(project_id=world["foreign"], chapter_id=chapter.id, chapter_no=1,
                                           content_hash="f" * 64, summary="他书分析")
                db.add(analysis)
                await db.flush()
                memory = StoryMemory(project_id=world["project"], chapter_no=1, chapter_id=None,
                                     analysis_id=analysis.id, kind="summary", origin="auto", content="检索关联")
                db.add(memory)
                await db.flush()
                await retrieval_service.upsert_project_sources(db, world["project"], "memory", [
                    memory_service._chunk(memory),
                ])
                await db.commit()
            response = await editor.post(f"{world['base']}/memories/search", json={"query": "检索关联"})
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "retrieval_reference_invalid"
            assert "他书" not in response.text and "检索关联" not in response.text
            assert requests == []
