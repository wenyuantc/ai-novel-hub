"""隔离 PostgreSQL 验证共享向量快照与真实模型出站的竞争。"""

import asyncio
import json
from contextlib import suppress

import httpx
import numpy as np
import pytest
from sqlalchemy import select, text

from app.ai.providers import OpenAICompatProvider
from app.models import RetrievalChunk, StoryMemory
from app.services import authorization_service, project_access, project_ai_config
from app.services import retrieval_service as rs
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp
from tests import pg_support
from tests.helpers import create_ai_config, create_project, register
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_access_postgresql import _wait_for_blocker


@pytest.fixture
async def postgres_retrieval_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(
        "AINOVEL_COLLAB_RETRIEVAL_POSTGRES_TEST_URL", "collab_retrieval", purpose="检索测试",
    )
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _setup(owner_app, owner, guest, *, base_url="https://embed.example.test/v1"):
    owner_user = await register(owner, "retrieval-pg-owner")
    editor = await register(guest, "retrieval-pg-editor")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invitation = await owner.post(f"{base}/collaborators", json={
        "username": "retrieval-pg-editor", "role": "editor", "allow_owner_ai": True,
    })
    assert invitation.status_code == 201, invitation.text
    invitation = invitation.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    await create_ai_config(owner, model_type="embedding", base_url=base_url, model="embedding-model")
    async with owner_app.state.sessionmaker() as db:
        memory = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", content="最初正文")
        db.add(memory)
        await db.flush()
        await rs.upsert_many(db, project["id"], "memory", [rs.ChunkInput(ref_id=memory.id, text=memory.content)])
        chunk_id = await db.scalar(select(RetrievalChunk.id).where(
            RetrievalChunk.project_id == project["id"], RetrievalChunk.ref_id == memory.id,
            RetrievalChunk.source == "memory",
        ))
        selection = await project_ai_config.resolve_shared_embedding(
            db, editor["id"], project["id"], cipher=owner_app.state.cipher, allow_private=True,
        )
        config = await rs.load_bound_shared_embedding_config(
            db, selection, cipher=owner_app.state.cipher, allow_private=True,
        )
        snapshots = await rs.snapshot_embedding_inputs(db, project["id"], [chunk_id])
        await db.commit()
    return owner_user, editor, project, base, memory.id, chunk_id, selection, config, snapshots[0]


@pytest.mark.parametrize("writer_first", [True, False])
async def test_postgres_vector_snapshot_and_source_edit_are_serialized(postgres_retrieval_apps, writer_first):
    owner_app, guest_app = postgres_retrieval_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        owner_user, _, project, _, memory_id, chunk_id, _, config, snapshot = await _setup(owner_app, owner, guest)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                if writer_first:
                    await project_access.require_project(
                        holder, owner_user["id"], project["id"], "content.edit", for_update=True,
                    )
                    memory = await holder.get(StoryMemory, memory_id)
                    memory.content = "后来的正文"
                    await rs.upsert_many(holder, project["id"], "memory", [
                        rs.ChunkInput(ref_id=memory_id, text=memory.content),
                    ])
                else:
                    assert await rs._store_embedding_snapshots(holder, [snapshot], [np.array([1.0, 0.0])], config)
                blocker = await holder.scalar(text("SELECT pg_backend_pid()"))

                async def contender():
                    async with guest_app.state.sessionmaker() as db:
                        if writer_first:
                            stored = await rs._store_embedding_snapshots(db, [snapshot], [np.array([1.0, 0.0])], config)
                            await db.commit()
                            return stored
                        await project_access.require_project(
                            db, owner_user["id"], project["id"], "content.edit", for_update=True,
                        )
                        memory = await db.get(StoryMemory, memory_id)
                        memory.content = "后来的正文"
                        await rs.upsert_many(db, project["id"], "memory", [
                            rs.ChunkInput(ref_id=memory_id, text=memory.content),
                        ])
                        await db.commit()
                        return True

                pending = asyncio.create_task(contender())
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, blocker), 10)
                assert not pending.done()
                await holder.commit()
            assert (await asyncio.wait_for(pending, 10)) is not writer_first
            async with owner_app.state.sessionmaker() as db:
                row = await db.get(RetrievalChunk, chunk_id)
                assert row.text == "后来的正文" and row.embedding is None and row.embedding_key is None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_postgres_real_embedding_lease_rechecks_revoke(postgres_retrieval_apps):
    owner_app, guest_app = postgres_retrieval_apps
    payload = json.dumps({"object": "list", "model": "embedding-model",
                          "data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, received, headers, body, requests):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        ):
            _, editor, project, base, _, _, selection, config, _ = await _setup(
                owner_app, owner, guest, base_url=f"{url}/v1/",
            )
            async with guest_app.state.sessionmaker() as db:
                stamp = await read_visibility_stamp(db, project["id"])

            send_pid = None

            async def authorize(db):
                nonlocal send_pid
                await authorization_service.require_authenticated_for_send(db, editor["id"])
                send_pid = await db.scalar(text("SELECT pg_backend_pid()"))

            async def verify(db):
                await project_ai_config.revalidate_shared_embedding(
                    db, selection, cipher=guest_app.state.cipher, allow_private=True,
                )

            provider = OpenAICompatProvider("local-key", f"{url}/v1/", 5)
            embedder = rs.Embedder(provider, config.provider, config.model)
            guarded = rs.guard_embedder(embedder, stamp, guest_app.state.sessionmaker,
                                        authorize=authorize, verify=verify)
            sending = asyncio.create_task(guarded.embed(["第一次内容"]))
            revoke = None
            try:
                await asyncio.wait_for(received.wait(), 10)
                revoke = asyncio.create_task(owner.delete(
                    f"{base}/collaborators/{editor['id']}", params={"expected_revision": 2},
                ))
                assert send_pid is not None
                # 撤权先锁 User，再取可见性写门禁；确认它确实被发送事务阻塞，
                # 不等待更后的可见性锁入口，否则会与放行响应头互相等待。
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, send_pid), 3)
                assert not revoke.done()
                headers.set()
                response = await asyncio.wait_for(revoke, 10)
                assert response.status_code == 204, response.text
                assert not sending.done()
                body.set()
                await asyncio.wait_for(sending, 10)
                with pytest.raises(VisibilityPolicyError):
                    await guarded.embed(["撤权后的另一批内容"])
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                for task in (sending, revoke):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (sending, revoke) if task is not None), return_exceptions=True)
