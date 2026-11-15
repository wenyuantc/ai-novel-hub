"""原分析记忆的来源行在实际发送和写入时的 PostgreSQL 竞争。"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import select, text

from app.models import RetrievalChunk, StoryMemory
from app.services import collaboration_ai_guard, kb_index_service, memory_service, retrieval_service
from tests.test_ai_visibility_dispatch import _server
from tests.test_bound_memory_index_e6 import _owner_job
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_bound_memory_send_locks_sources_until_response_headers(postgres_collaboration_apps, monkeypatch):
    owner_app, other_app = postgres_collaboration_apps
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        _server(mime="application/json", payload=payload) as (url, received, headers, body, requests),
    ):
        _, project, refs, _, job = await _owner_job(owner_app, owner, url)
        reached, release = asyncio.Event(), asyncio.Event()
        original = retrieval_service.require_sending_embedding_snapshots
        holder = {}

        async def gated(db, snapshots, **kwargs):
            await original(db, snapshots, **kwargs)
            if not kwargs["preflight"] and not reached.is_set():
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()

        async def change_source():
            async with other_app.state.sessionmaker() as db:
                memory = await db.scalar(select(StoryMemory).where(StoryMemory.id == refs[0]).with_for_update())
                memory.content = "发送后变更"
                await db.flush()
                await retrieval_service.upsert_project_sources(db, project["id"], "memory", [
                    memory_service._chunk(memory),
                ])
                await db.commit()

        async def run():
            return [item async for item in kb_index_service.run_index_job(job, owner_app.state.sessionmaker)]

        monkeypatch.setattr(retrieval_service, "require_sending_embedding_snapshots", gated)
        sending = asyncio.create_task(run())
        change = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            change = asyncio.create_task(change_source())
            await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            await asyncio.wait_for(received.wait(), 8)
            assert len(requests) == 1 and not change.done()
            headers.set()
            await asyncio.wait_for(change, 8)
            body.set()
            events = await asyncio.wait_for(sending, 8)
            assert events[-1][0] == "error" and len(requests) == 1
            async with owner_app.state.sessionmaker() as db:
                memory = await db.get(StoryMemory, refs[0])
                row = await db.scalar(select(RetrievalChunk).where(
                    RetrievalChunk.source == "memory", RetrievalChunk.ref_id == refs[0],
                ))
                assert memory.content == row.text == "发送后变更"
                assert row.embedding is None and row.embedding_key is None
        finally:
            release.set()
            headers.set()
            body.set()
            for pending in (sending, change):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_bound_memory_edit_after_snapshot_blocks_actual_send(postgres_collaboration_apps, monkeypatch):
    owner_app, other_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests),
    ):
        headers.set()
        body.set()
        _, project, refs, _, job = await _owner_job(owner_app, owner, url)
        reached, release = asyncio.Event(), asyncio.Event()
        original = collaboration_ai_guard.SendGuard.prepare_provider

        async def gated(guard, *args, **kwargs):
            provider = await original(guard, *args, **kwargs)
            reached.set()
            await release.wait()
            return provider

        async def run():
            return [item async for item in kb_index_service.run_index_job(job, owner_app.state.sessionmaker)]

        monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", gated)
        sending = asyncio.create_task(run())
        try:
            await asyncio.wait_for(reached.wait(), 8)
            async with other_app.state.sessionmaker() as db:
                memory = await db.get(StoryMemory, refs[0])
                memory.content = "发送前变更"
                await db.flush()
                await retrieval_service.upsert_project_sources(db, project["id"], "memory", [
                    memory_service._chunk(memory),
                ])
                await db.commit()
            release.set()
            events = await asyncio.wait_for(sending, 8)
            assert events[-1][0] == "error" and requests == []
            async with owner_app.state.sessionmaker() as db:
                row = await db.scalar(select(RetrievalChunk).where(
                    RetrievalChunk.source == "memory", RetrievalChunk.ref_id == refs[0],
                ))
                assert row.text == "发送前变更" and row.embedding is None
        finally:
            release.set()
            if not sending.done():
                sending.cancel()
                with suppress(asyncio.CancelledError):
                    await sending
