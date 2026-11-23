"""SQLite 真实 HTTP 慢响应头持续一个租约以上，两端仍须以原身份续约。"""

import asyncio
import json
import time

from app.ai.providers import create_provider
from app.models import Chapter, Task
from tests.helpers import create_ai_config, create_project, register, wait_task
from tests.test_agent_providers import openai_chunk
from tests.test_ai_visibility_dispatch import _server
from tests.test_tasks import _generate
from tests.worker_helpers import worker_row


async def test_slow_provider_headers_preserve_both_worker_leases(app, second_app, client, monkeypatch):
    source, peer = app.state.task_engine, second_app.state.task_engine
    for engine in (source, peer):
        assert (engine.heartbeat_interval, engine.lease_seconds, engine.lease_safety_seconds) == (5, 30, 5)
        monkeypatch.setattr(engine.ctx.settings, "allow_private_ai_endpoints", True)
        monkeypatch.setattr(engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    await peer.start()
    keys = [source.lease.worker_key, peer.lease.worker_key]
    assert keys[0] and keys[1] and keys[0] != keys[1]

    await register(client)
    project = await create_project(client)
    content = "慢响应头后的完整正文"
    chunk = openai_chunk({"content": content}, "stop")
    payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
    async with _server(payload=payload) as (url, received, send_headers, send_body, requests):
        await create_ai_config(client, base_url=f"{url}/v1/")
        response = await client.post(f"/api/v1/projects/{project['id']}/chapters",
                                     json={"title": "首章", "content": "原稿"})
        assert response.status_code == 201, response.text
        chapter = response.json()
        issued = await _generate(client, chapter)
        assert issued.status_code == 202, issued.text
        ref = issued.json()
        await asyncio.wait_for(received.wait(), 10)
        started = time.monotonic()
        engines = (source, peer)
        previous = [(await worker_row(engine, key)).heartbeat_at for engine, key in zip(engines, keys, strict=True)]
        # 真实 SDK 已发送请求。保持服务器不返回响应头，在同一门禁未释放时检查两次持久续约。
        for _ in range(2):
            await asyncio.sleep(16)
            for idx, engine in enumerate(engines):
                row = await worker_row(engine, keys[idx])
                assert row.heartbeat_at > previous[idx]
                assert row.fenced_at is None and row.stopped_at is None
                assert engine.lease.worker_key == keys[idx] and engine.lease.valid
                previous[idx] = row.heartbeat_at
        assert time.monotonic() - started >= 32
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, ref["id"])
            assert task.status == "running" and task.worker_key in keys
            assert (await db.get(Chapter, chapter["id"])).content == "原稿"
        send_headers.set()
        send_body.set()
        assert (await wait_task(client, ref, within=15))["status"] == "succeeded"
        assert len(requests) == 1

    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).content == content
    assert [source.lease.worker_key, peer.lease.worker_key] == keys
