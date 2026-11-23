"""慢响应首部对照：HTTP 请求已通过真实发送门禁，响应头分别立即或延迟返回。"""

import asyncio
import json
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.ai.providers import create_provider
from app.ai.providers.dispatch_hooks import DispatchHooks
from app.models import Chapter
from app.services.ai_dispatch import _PhysicalDispatchHooks
from tests.helpers import create_ai_config, create_project, register, wait_task
from tests.test_agent_providers import openai_chunk
from tests.test_ai_visibility_dispatch import _server
from tests.test_tasks import _generate, _setup


async def probe_write(app, worker_key):
    async with app.state.sessionmaker() as db:
        sqlite = db.bind.dialect.name == "sqlite"
        if sqlite:
            await db.execute(text("PRAGMA busy_timeout = 100"))
        else:
            await db.execute(text("SET LOCAL lock_timeout = '100ms'"))
        try:
            await db.execute(text("UPDATE task_workers SET heartbeat_at = heartbeat_at WHERE worker_key=:key"),
                             {"key": worker_key})
        except OperationalError as exc:
            result = exc.orig.sqlite_errorname if sqlite else exc.orig.sqlstate
        else:
            result = "可写"
        await db.rollback()
        if sqlite:
            await db.execute(text("PRAGMA busy_timeout = 5000"))
        return result


@pytest.mark.parametrize("header_delay", [0, 32])
async def test_provider_within_configured_timeout(app, client, fake_llm, monkeypatch, header_delay):
    engine = app.state.task_engine
    _, _, (chapter,) = await _setup(client, content="原稿")
    first_key = engine.lease.worker_key
    sent = asyncio.Event()
    headers_finished = asyncio.Event()
    cancelled = []
    elapsed = []
    before_request = _PhysicalDispatchHooks._before_request

    async def delay_response_headers(hooks, request):
        await before_request(hooks, request)
        sent.set()
        if header_delay:
            started = time.monotonic()
            try:
                await asyncio.sleep(header_delay)
            except asyncio.CancelledError:
                cancelled.append(round(time.monotonic() - started, 2))
                raise
            finally:
                elapsed.append(round(time.monotonic() - started, 2))
                headers_finished.set()

    monkeypatch.setattr(_PhysicalDispatchHooks, "_before_request", delay_response_headers)
    fake_llm.push(["完整回复"])
    issued = await _generate(client, chapter)
    assert issued.status_code == 202
    await asyncio.wait_for(sent.wait(), 8)
    lock_result = None
    if header_delay:
        # 单独会话只诊断主库能否执行等价写入，不触碰正式心跳的内容。
        lock_result = await probe_write(app, first_key)
        await asyncio.wait_for(headers_finished.wait(), header_delay + 8)

    task = await wait_task(client, issued.json(), within=15)
    async with app.state.sessionmaker() as db:
        body = (await db.get(Chapter, chapter["id"])).content
    observed = (task["status"], body, first_key == engine.lease.worker_key)
    print(f"header_delay={header_delay} locked={lock_result} elapsed={elapsed} "
          f"cancelled_after={cancelled} outcome={observed}")
    assert observed == ("succeeded", "完整回复", True)


@pytest.mark.parametrize("header_delay", [0, 32])
async def test_tcp_provider_within_configured_timeout(app, client, monkeypatch, header_delay):
    engine = app.state.task_engine
    monkeypatch.setattr(engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    headers_processed = asyncio.Event()
    original = DispatchHooks._after_headers

    async def observe_headers(hooks, response):
        await original(hooks, response)
        headers_processed.set()

    monkeypatch.setattr(DispatchHooks, "_after_headers", observe_headers)
    await register(client)
    project = await create_project(client)
    first_key = engine.lease.worker_key
    content = "完整回复"
    chunk = openai_chunk({"content": content}, "stop")
    payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
    async with _server(payload=payload) as (url, received, send_headers, send_body, requests):
        await create_ai_config(client, base_url=f"{url}/v1/")
        chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters",
                                     json={"title": "首章", "content": "原稿"})).json()
        issued = await _generate(client, chapter)
        assert issued.status_code == 202
        await asyncio.wait_for(received.wait(), 10)
        started = time.monotonic()
        before = await probe_write(app, first_key)
        changed_after = None
        while time.monotonic() < started + header_delay:
            if changed_after is None and (not engine.lease.valid or first_key != engine.lease.worker_key):
                changed_after = round(time.monotonic() - started, 2)
            await asyncio.sleep(.1)
        send_headers.set()
        after = None
        if changed_after is None:
            await asyncio.wait_for(headers_processed.wait(), 5)
            after = await probe_write(app, first_key)
        send_body.set()
        task = await wait_task(client, issued.json(), within=15)
        assert len(requests) == 1
    async with app.state.sessionmaker() as db:
        body = (await db.get(Chapter, chapter["id"])).content
    observed = (task["status"], body, first_key == engine.lease.worker_key)
    print(f"tcp_delay={header_delay} before_headers={before} after_headers_before_body={after} "
          f"identity_lost_after={changed_after} outcome={observed}")
    assert observed == ("succeeded", content, True)
    assert after == "可写"
