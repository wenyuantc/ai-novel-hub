"""PostgreSQL 文档 claim 发送 lease 持锁直至实际响应头。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text, update

from app.ai.providers import AIProviderError, create_provider
from app.core.errors import UnauthorizedError
from app.models import KbDocument
from app.services import collaboration_ai_guard, kb_index_service
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.retrieval_service import EMBED_TEXT_CHARS
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_kb_document_guard_integration_e6 import _document_snapshots, _editor_execution

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_document_claim_is_held_through_real_http_headers(postgres_collaboration_apps, monkeypatch):
    owner_app, editor_app = postgres_collaboration_apps
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
        _server(mime="application/json", payload=payload) as (url, received, headers, body, requests),
    ):
        _, _, _, execution, claim, selection, stamp = await _editor_execution(owner_app, owner, editor, url)
        snapshots = await _document_snapshots(owner_app, claim)
        text_value = snapshots[0].embed_text[:EMBED_TEXT_CHARS]
        guard = collaboration_ai_guard.for_task(
            execution, selection, stamp, cipher=owner_app.state.cipher, allow_private=True, document_claim=claim,
            embedding_inputs=snapshots,
        )
        bound = await guard.prepare_provider(owner_app.state.sessionmaker, create_provider, request_timeout=8)
        verified, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = kb_index_service.require_sending_document_claim

        async def gated(db, source, **kwargs):
            await original(db, source, **kwargs)
            if not kwargs["preflight"] and not verified.is_set():
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                verified.set()
                await release.wait()

        async def replace_claim():
            async with editor_app.state.sessionmaker() as db:
                await db.scalar(select(KbDocument.id).where(KbDocument.id == claim.document_id).with_for_update())
                await db.execute(update(KbDocument).where(KbDocument.id == claim.document_id).values(
                    index_claim_key=str(uuid4()),
                ))
                await db.commit()

        monkeypatch.setattr(kb_index_service, "require_sending_document_claim", gated)
        sending = asyncio.create_task(bound.provider.embed([text_value], bound.model))
        change = None
        try:
            await asyncio.wait_for(verified.wait(), 8)
            change = asyncio.create_task(replace_claim())
            await asyncio.wait_for(_wait_for_blocker(editor_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            await asyncio.wait_for(received.wait(), 8)
            assert len(requests) == 1 and not change.done()
            headers.set()
            await asyncio.wait_for(change, 8)
            body.set()
            assert await asyncio.wait_for(sending, 8) == [[1.0, 0.0]]
            with pytest.raises(VisibilityPolicyError):
                await bound.provider.preflight()
            with pytest.raises((AIProviderError, VisibilityPolicyError, UnauthorizedError)):
                await bound.provider.embed([text_value], bound.model)
            async with owner_app.state.sessionmaker() as db:
                with pytest.raises(VisibilityPolicyError):
                    await guard.require_task_write(db)
            assert len(requests) == 1
        finally:
            release.set()
            headers.set()
            body.set()
            for task in (sending, change):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
