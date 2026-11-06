"""真实 SDK + 本地 HTTP；不用 MockTransport 代替响应头及后续发送顺序。"""

import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai.providers import AIProviderError, AnthropicProvider, GeminiProvider, OpenAICompatProvider
from app.ai.providers.dispatch_hooks import DispatchHooks
from app.models import Project
from app.services import ai_dispatch, retrieval_service
from app.services.ai_dispatch import (
    VisibilityBusyError,
    VisibilityChangedError,
    guard_provider,
    lock_visibility_write,
    read_visibility_stamp,
)
from tests.helpers import create_project, register
from tests.test_agent_providers import TOOLS, anthropic_stream, gemini_chunk, openai_chunk

MESSAGES = [{"role": "user", "content": "settings-dispatch-marker"}]
PROVIDERS = {
    "openai": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def _sse(kind):
    if kind == "anthropic":
        events = anthropic_stream(
            [({"type": "text", "text": ""}, [{"type": "text_delta", "text": "ok"}])],
            reason="end_turn",
        )
    elif kind == "gemini":
        events = [gemini_chunk([{"text": "ok"}], "STOP")]
    else:
        events = [openai_chunk({"content": "ok"}, "stop")]
    frames = []
    for event in events:
        if kind == "anthropic":
            frames.append(f"event: {event['type']}\n")
        frames.append(f"data: {json.dumps(event)}\n\n")
    if kind == "openai":
        frames.append("data: [DONE]\n\n")
    return "".join(frames).encode()


@asynccontextmanager
async def _server(*, status="200 OK", mime="text/event-stream", payload=b"", headers=None):
    received = asyncio.Event()
    send_headers = asyncio.Event()
    send_body = asyncio.Event()
    requests = []
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            raw = (await reader.readuntil(b"\r\n\r\n")).decode()
            request_headers = dict(line.lower().split(": ", 1) for line in raw.split("\r\n")[1:] if line)
            request = await reader.readexactly(int(request_headers.get("content-length", 0)))
            requests.append(request)
            received.set()
            await send_headers.wait()
            if status is None:
                return  # 关闭已经收到请求的连接，不发送任何 HTTP 响应头。
            other_headers = "".join(f"{key}: {value}\r\n" for key, value in (headers or {}).items())
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Length: {len(payload)}\r\nContent-Type: {mime}\r\n"
                f"{other_headers}Connection: close\r\n\r\n".encode()
            )
            await writer.drain()
            await send_body.wait()
            writer.write(payload)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", received, send_headers, send_body, requests
    finally:
        server.close()
        send_headers.set()
        send_body.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()


async def _collect(stream):
    return [item async for item in stream]


async def _tighten(maker, project_id):
    async with maker() as db:
        await lock_visibility_write(db, project_id)
        await db.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(
                ai_visibility_revision=Project.ai_visibility_revision + 1,
                ai_history_epoch=Project.ai_history_epoch + 1,
            )
        )
        await db.commit()


@pytest.fixture
async def project_scope(app, client):
    await register(client)
    project = await create_project(client)
    maker = app.state.sessionmaker
    async with maker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    return maker, stamp


@pytest.mark.parametrize(
    ("kind", "method"),
    [
        ("openai", "text"), ("anthropic", "text"), ("gemini", "text"),
        ("openai", "tools"), ("anthropic", "tools"), ("gemini", "tools"),
        ("openai", "embed"), ("gemini", "embed"),
    ],
)
async def test_real_dispatch_is_fenced_until_headers_not_body(project_scope, monkeypatch, kind, method):
    maker, stamp = project_scope
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.15)
    payload = _sse(kind)
    mime = "text/event-stream"
    if method == "embed":
        mime = "application/json"
        response = (
            {"object": "list", "model": "test-model", "data": [{"index": 0, "embedding": [1.0]}]}
            if kind == "openai"
            else {"embeddings": [{"values": [1.0]}]}
        )
        payload = json.dumps(response).encode()
    async with _server(payload=payload, mime=mime) as (url, received, headers, body, requests):
        base_url = f"{url}/v1/" if kind == "openai" else url
        provider = guard_provider(PROVIDERS[kind]("local-key", base_url, 5), stamp, maker)

        async def call():
            if method == "embed":
                return await provider.embed([MESSAGES[0]["content"]], "test-model")
            if method == "tools":
                return await _collect(provider.chat_tools_stream(MESSAGES, "test-model", TOOLS))
            return await _collect(provider.generate_stream(MESSAGES, "test-model"))

        sending = asyncio.create_task(call())
        try:
            await asyncio.wait_for(received.wait(), 5)
            assert b"settings-dispatch-marker" in requests[0]
            # 真实服务器已收到负载；响应头仍被故意阻塞，策略保存不能假成功。
            with pytest.raises(VisibilityBusyError):
                await _tighten(maker, stamp.project_id)
            headers.set()
            await asyncio.wait_for(_tighten(maker, stamp.project_id), 3)
            assert not sending.done()  # 响应体尚未发送；写端无需等待它。
            body.set()
            result = await asyncio.wait_for(sending, 5)
            if method == "embed":
                assert result == [[1.0]]
            elif method == "tools":
                assert ("text", {"text": "ok"}) in result
                assert result[-1][0] == "finish"
            else:
                assert result == ["ok"]
            # 已提交收紧；同一 SDK 再尝试时，服务器看不到第二次负载。
            with pytest.raises(VisibilityChangedError) as error:
                await call()
            assert error.value.extra == {"code": "ai_visibility_changed"}
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.parametrize("transition", ["redirect", "compatibility"])
async def test_new_attempt_of_same_sdk_operation_rechecks_stamp(project_scope, monkeypatch, transition):
    maker, stamp = project_scope
    original = DispatchHooks._after_headers
    saved = False

    async def after(self, response):
        nonlocal saved
        await original(self, response)
        if not saved:
            await _tighten(maker, stamp.project_id)
            saved = True

    monkeypatch.setattr(DispatchHooks, "_after_headers", after)
    payload = b'{"error":{"message":"unsupported stream_options","type":"invalid_request"}}'
    status = "307 Temporary Redirect" if transition == "redirect" else "400 Bad Request"
    headers = {"Location": "/fallback"} if transition == "redirect" else None
    async with _server(payload=payload, status=status, mime="application/json", headers=headers) as (
        url, _, send_headers, send_body, requests,
    ):
        send_headers.set()
        send_body.set()
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, maker)
        with pytest.raises(VisibilityChangedError):
            await _collect(provider.chat_tools_stream(MESSAGES, "test-model", TOOLS))
        assert saved and len(requests) == 1


async def test_cancel_before_headers_releases_sqlite_transaction(project_scope):
    maker, stamp = project_scope
    async with _server(payload=_sse("openai")) as (url, received, _, _, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, maker)
        pending = asyncio.create_task(_collect(provider.generate_stream(MESSAGES, "test-model")))
        try:
            await asyncio.wait_for(received.wait(), 5)
        finally:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await asyncio.wait_for(_tighten(maker, stamp.project_id), 3)
        assert len(requests) == 1


async def test_author_save_is_atomic_against_real_send_window(project_scope, client, monkeypatch):
    maker, stamp = project_scope
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    base = f"/api/v1/projects/{stamp.project_id}"
    created = await client.post(f"{base}/characters", json={"name": "门禁验证"})
    assert created.status_code == 201, created.text
    path = f"{base}/setting-entries/character/{created.json()['id']}/ai-settings"
    patch = {"expected_ai_visibility_revision": stamp.revision, "ai_hidden_builtin_fields": ["background"]}
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, maker)
        pending = asyncio.create_task(_collect(provider.generate_stream(MESSAGES, "test-model")))
        try:
            await asyncio.wait_for(received.wait(), 5)
            blocked = await client.patch(path, json=patch)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            unchanged = (await client.get(path)).json()
            assert unchanged["ai_visibility_revision"] == stamp.revision and not unchanged["ai_hidden_builtin_fields"]
            headers.set()
            saved = await client.patch(path, json=patch)
            assert saved.status_code == 200 and saved.json()["ai_visibility_revision"] == stamp.revision + 1, saved.text
            assert not pending.done()
            body.set()
            assert await asyncio.wait_for(pending, 5) == ["ok"]
            with pytest.raises(VisibilityChangedError):
                await _collect(provider.generate_stream(MESSAGES, "test-model"))
            assert len(requests) == 1
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("kind", tuple(PROVIDERS))
@pytest.mark.parametrize("status", [None, "503 Service Unavailable"], ids=["disconnected", "unavailable"])
async def test_real_dispatch_failure_releases_and_does_not_retry(project_scope, kind, status):
    maker, stamp = project_scope
    payload = b'{"error":{"code":503,"message":"unavailable","status":"UNAVAILABLE"}}'
    async with _server(status=status, mime="application/json", payload=payload) as (
        url, received, headers, body, requests,
    ):
        base_url = f"{url}/v1/" if kind == "openai" else url
        provider = guard_provider(PROVIDERS[kind]("local-key", base_url, 5), stamp, maker)
        pending = asyncio.create_task(_collect(provider.generate_stream(MESSAGES, "test-model")))
        try:
            await asyncio.wait_for(received.wait(), 5)
            headers.set()
            body.set()
            with pytest.raises(AIProviderError):
                await asyncio.wait_for(pending, 5)
            await asyncio.wait_for(_tighten(maker, stamp.project_id), 3)
            with pytest.raises(VisibilityChangedError):
                await _collect(provider.generate_stream(MESSAGES, "test-model"))
            assert len(requests) == 1
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_fake_provider_and_vector_fallback_do_not_accept_old_stamp(project_scope, fake_llm):
    maker, stamp = project_scope
    raw = fake_llm.factory("openai_compat", "test", None, 5)
    guarded = guard_provider(raw, stamp, maker)
    embedder = retrieval_service.Embedder(provider=raw, provider_name="openai_compat", model="test")
    guarded_embed = retrieval_service.guard_embedder(embedder, stamp, maker)
    await _tighten(maker, stamp.project_id)
    with pytest.raises(VisibilityChangedError):
        await _collect(guarded.generate_stream(MESSAGES, "test"))
    with pytest.raises(VisibilityChangedError):
        await retrieval_service.embed_query(guarded_embed, MESSAGES[0]["content"])
    assert not fake_llm.calls and not fake_llm.embed_calls
    assert await _collect(raw.generate_stream(MESSAGES, "test")) == ["ok"]


async def test_postgres_dispatch_lock_is_shared_per_project(monkeypatch):
    database_url = os.environ.get("AINOVEL_FIELDS_POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("设置 AINOVEL_FIELDS_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 验证")
    schema = f"visibility_dispatch_test_{uuid4().hex}"
    bootstrap = create_async_engine(database_url)
    engine = create_async_engine(database_url, connect_args={"server_settings": {"search_path": schema}})
    maker = async_sessionmaker(engine)
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.15)
    try:
        async with bootstrap.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE TABLE projects (id integer PRIMARY KEY, ai_visibility_revision integer NOT NULL, "
                "ai_history_epoch integer NOT NULL, last_deleted_at timestamptz, deleted_at timestamptz, "
                "deletion_started_at timestamptz, updated_at timestamptz)"
            ))
            await conn.execute(text("INSERT INTO projects (id, ai_visibility_revision, ai_history_epoch) "
                                    "VALUES (1, 0, 0), (2, 0, 0)"))
        async with maker() as reader1, maker() as reader2:
            await ai_dispatch._lock(reader1, 1, write=False)
            await ai_dispatch._lock(reader2, 1, write=False)
            with pytest.raises(VisibilityBusyError):
                await _tighten(maker, 1)
            await asyncio.wait_for(_tighten(maker, 2), 3)
        await asyncio.wait_for(_tighten(maker, 1), 3)
    finally:
        await engine.dispose()
        async with bootstrap.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await bootstrap.dispose()
