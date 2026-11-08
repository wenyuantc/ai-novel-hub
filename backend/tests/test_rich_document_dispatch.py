"""已启用区块收紧与真实出站 lease、重定向之间的屏障。"""

import asyncio
import json

import pytest

from app.ai.providers import OpenAICompatProvider
from app.ai.providers.dispatch_hooks import DispatchHooks
from app.services import ai_dispatch
from app.services.ai_dispatch import VisibilityChangedError, guard_provider, read_visibility_stamp
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scenes import setup_scene


async def _enabled_document(app, client):
    project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    disable = save_payload(saved.json())
    disable["document"]["sections"][0]["include_in_ai"] = False
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    return endpoint, disable, stamp


async def test_tightening_waits_for_real_send_headers_then_blocks_stale_retry(app, client, monkeypatch):
    endpoint, disable, stamp = await _enabled_document(app, client)
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    messages = [{"role": "user", "content": "区块材料：作者🙂"}]
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        pending = asyncio.create_task(_collect(provider.generate_stream(messages, "test-model")))
        try:
            await asyncio.wait_for(received.wait(), 5)
            assert "作者🙂" in json.loads(requests[0])["messages"][0]["content"]
            blocked = await client.put(endpoint, json=disable)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            assert (await client.get(endpoint)).json()["document"]["sections"][0]["include_in_ai"] is True
            headers.set()
            saved = await client.put(endpoint, json=disable)
            assert saved.status_code == 200, saved.text
            assert not pending.done()
            body.set()
            assert await asyncio.wait_for(pending, 5) == ["ok"]
            with pytest.raises(VisibilityChangedError):
                await _collect(provider.generate_stream(messages, "test-model"))
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_redirect_does_not_replay_old_enabled_section_after_tightening(app, client, monkeypatch):
    endpoint, disable, stamp = await _enabled_document(app, client)
    original = DispatchHooks._after_headers
    saved = False

    async def after(self, response):
        nonlocal saved
        await original(self, response)
        if not saved:
            result = await client.put(endpoint, json=disable)
            assert result.status_code == 200, result.text
            saved = True

    monkeypatch.setattr(DispatchHooks, "_after_headers", after)
    messages = [{"role": "user", "content": "区块材料：作者🙂"}]
    async with _server(status="307 Temporary Redirect", mime="application/json", headers={"Location": "/next"}) as (
        url, _received, send_headers, send_body, requests,
    ):
        send_headers.set()
        send_body.set()
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        with pytest.raises(VisibilityChangedError):
            await _collect(provider.generate_stream(messages, "test-model"))
        assert saved and len(requests) == 1


@pytest.mark.parametrize("operation", ["split", "merge"])
async def test_moving_enabled_section_obeys_actual_send_lease(app, client, monkeypatch, operation):
    project, _chapter, url, scene = await setup_scene(client, "甲🙂乙丙丁尾")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    body["document"]["sections"][0]["anchor"] = 4
    assert (await client.put(endpoint, json=body)).status_code == 200
    if operation == "split":
        request_url = f"{url}/scenes/{scene['id']}/split"
        request_body = {"expected_content_revision": 1, "offset": 3}
    else:
        split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
            "expected_content_revision": 1, "offset": 3,
        })
        assert split.status_code == 200, split.text
        ids = [item["id"] for item in split.json()["items"]]
        preview = await client.post(f"{url}/scenes/merge-preview", json={
            "expected_content_revision": 2, "scene_ids": ids,
        })
        assert preview.status_code == 200 and not preview.json()["blockers"], preview.text
        request_url = f"{url}/scenes/merge"
        request_body = {"expected_content_revision": 2, "scene_ids": ids,
                        "expected_planning_revision": preview.json()["planning_revision"],
                        "signature": preview.json()["signature"], "metadata_source": "left"}
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    messages = [{"role": "user", "content": "区块材料：作者🙂"}]
    async with _server(payload=_sse("openai")) as (url, received, headers, response_body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        pending = asyncio.create_task(_collect(provider.generate_stream(messages, "test-model")))
        try:
            await asyncio.wait_for(received.wait(), 5)
            blocked = await client.post(request_url, json=request_body)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            headers.set()
            changed = await client.post(request_url, json=request_body)
            assert changed.status_code == 200, changed.text
            response_body.set()
            assert await asyncio.wait_for(pending, 5) == ["ok"]
            with pytest.raises(VisibilityChangedError):
                await _collect(provider.generate_stream(messages, "test-model"))
            assert len(requests) == 1
        finally:
            headers.set()
            response_body.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
