"""封面协作权限与真实图片发送的 PostgreSQL 双连接竞争。"""

import asyncio
import base64
import json
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import text

from app.ai.providers import create_provider
from app.api.v1 import project_cover as cover_api
from app.services import collaboration_ai_guard, project_access, style_service
from tests.cover_fixtures import PNG
from tests.helpers import create_ai_config
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_revocation_after_binding_prevents_image_admission(postgres_collaboration_apps, monkeypatch):
    owner_app, editor_app = postgres_collaboration_apps
    owner_app.state.settings.allow_private_ai_endpoints = True
    editor_app.state.provider_factory = create_provider
    entered, release = asyncio.Event(), asyncio.Event()
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def pause_after_binding(guard, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(guard, *args, **kwargs)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", pause_after_binding)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        project, user, base = await _setup(owner, editor)
        payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        async with _server(payload=payload, mime="application/json") as (url, received, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
            request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "过期授权"}))
            try:
                await asyncio.wait_for(entered.wait(), 15)
                revoked = await owner.delete(f"{base}/collaborators/{user['id']}", params={"expected_revision": 2})
                assert revoked.status_code == 204, revoked.text
                release.set()
                response = await asyncio.wait_for(request, 15)
                assert response.status_code in (403, 404, 409), response.text
                assert not received.is_set() and not calls and "image_base64" not in response.text
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                    with suppress(asyncio.CancelledError):
                        await request


async def test_prompt_holds_authorization_until_safe_projection(postgres_collaboration_apps, monkeypatch):
    owner_app, editor_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        project, user, base = await _setup(owner, editor)
        entered, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = style_service.visible_default_style

        async def wait_for_projection(db, row, *, actor_user_id):
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            await release.wait()
            return await original(db, row, actor_user_id=actor_user_id)

        monkeypatch.setattr(style_service, "visible_default_style", wait_for_projection)
        prompt = asyncio.create_task(editor.get(f"{base}/cover/prompt"))
        revoke = None
        try:
            await asyncio.wait_for(entered.wait(), 15)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": 2,
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 15)
            assert not revoke.done()
            release.set()
            assert (await asyncio.wait_for(prompt, 15)).status_code == 200
            assert (await asyncio.wait_for(revoke, 15)).status_code == 204
            assert (await editor.get(f"{base}/cover/prompt")).status_code == 404
        finally:
            release.set()
            for job in (prompt, revoke):
                if job is not None and not job.done():
                    job.cancel()
                    with suppress(asyncio.CancelledError):
                        await job


async def test_stalled_prompt_response_releases_project_lock(postgres_collaboration_apps, monkeypatch):
    owner_app, editor_app = postgres_collaboration_apps
    monkeypatch.setattr(cover_api, "_OUTPUT_LEASE_SECONDS", 1.5)
    reached_send, release_send = asyncio.Event(), asyncio.Event()
    holder = {}
    original = project_access.require_project

    async def observe_lock(db, actor_id, project_id, capability, **kwargs):
        project = await original(db, actor_id, project_id, capability, **kwargs)
        if capability == "ai.use" and kwargs.get("for_update"):
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        return project

    monkeypatch.setattr(project_access, "require_project", observe_lock)

    async def block_response(scope, receive, send):
        async def delayed_send(message):
            if (scope["path"].endswith("/cover/prompt") and message["type"] == "http.response.start"
                    and message["status"] == 200):
                reached_send.set()
                await release_send.wait()
            await send(message)

        await editor_app(scope, receive, delayed_send)

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=block_response), base_url="http://testserver") as editor,
    ):
        project, user, base = await _setup(owner, editor)
        request = asyncio.create_task(editor.get(f"{base}/cover/prompt"))
        revoke = None
        try:
            await asyncio.wait_for(reached_send.wait(), 15)
            assert holder.get("pid") is not None
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": 2,
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 15)
            assert not revoke.done()
            result = await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 15)
            if isinstance(result[0], httpx.Response):
                assert result[0].status_code >= 400 and "prompt" not in result[0].text
            else:
                assert isinstance(result[0], BaseException)
            assert (await asyncio.wait_for(revoke, 15)).status_code == 204
        finally:
            release_send.set()
            for job in (request, revoke):
                if job is not None and not job.done():
                    job.cancel()
                    with suppress(asyncio.CancelledError):
                        await job


async def test_image_send_fences_revoke_until_headers_and_withholds_late_result(
    postgres_collaboration_apps, monkeypatch,
):
    owner_app, editor_app = postgres_collaboration_apps
    owner_app.state.settings.allow_private_ai_endpoints = True
    editor_app.state.provider_factory = create_provider
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        project, user, base = await _setup(owner, editor)
        holder, verified = {}, asyncio.Event()
        original = collaboration_ai_guard.SendGuard.verify

        async def observed_verify(guard, db):
            await original(guard, db)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            verified.set()

        monkeypatch.setattr(collaboration_ai_guard.SendGuard, "verify", observed_verify)
        payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        async with _server(payload=payload, mime="application/json") as (url, received, headers, body, calls):
            await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
            request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "受控封面"}))
            revoke = None
            try:
                await asyncio.wait_for(verified.wait(), 15)
                await asyncio.wait_for(received.wait(), 15)
                revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                    "expected_revision": 2,
                }))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 15)
                assert not revoke.done() and not request.done()
                headers.set()
                assert (await asyncio.wait_for(revoke, 15)).status_code == 204
                assert not request.done()
                body.set()
                response = await asyncio.wait_for(request, 15)
                assert response.status_code in (403, 404, 409), response.text
                assert "image_base64" not in response.text and len(calls) == 1
            finally:
                headers.set()
                body.set()
                for job in (request, revoke):
                    if job is not None and not job.done():
                        job.cancel()
                        with suppress(asyncio.CancelledError):
                            await job


async def test_url_download_does_not_send_after_revoke_on_other_connection(postgres_collaboration_apps, monkeypatch):
    owner_app, editor_app = postgres_collaboration_apps
    owner_app.state.settings.allow_private_ai_endpoints = True
    editor_app.state.provider_factory = create_provider
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        project, user, base = await _setup(owner, editor)
        async with _server(payload=PNG, mime="image/png") as (image_url, image_received, image_heads, image_body, hits):
            image_heads.set()
            image_body.set()
            payload = json.dumps({"created": 1, "data": [{"url": f"{image_url}/preview.png"}]}).encode()
            async with _server(payload=payload, mime="application/json") as (url, received, heads, body, calls):
                await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
                request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "图片 URL"}))
                try:
                    await asyncio.wait_for(received.wait(), 15)
                    heads.set()
                    revoked = await owner.delete(f"{base}/collaborators/{user['id']}", params={
                        "expected_revision": 2,
                    })
                    assert revoked.status_code == 204, revoked.text
                    body.set()
                    response = await asyncio.wait_for(request, 15)
                    assert response.status_code in (403, 404, 409), response.text
                    assert len(calls) == 1 and not hits and not image_received.is_set()
                    assert "image_base64" not in response.text
                finally:
                    heads.set()
                    body.set()
                    if not request.done():
                        request.cancel()
                        with suppress(asyncio.CancelledError):
                            await request


@pytest.mark.parametrize("stalled", [False, True])
async def test_final_image_response_holds_original_grant_until_asgi_send(
    postgres_collaboration_apps, monkeypatch, stalled,
):
    owner_app, editor_app = postgres_collaboration_apps
    owner_app.state.settings.allow_private_ai_endpoints = True
    editor_app.state.provider_factory = create_provider
    if stalled:
        monkeypatch.setattr(cover_api, "_OUTPUT_LEASE_SECONDS", 1.5)
    reached_send, release_send = asyncio.Event(), asyncio.Event()
    holder = {}
    original = project_access.require_project

    async def observe_final_lock(db, actor_id, project_id, capability, **kwargs):
        project = await original(db, actor_id, project_id, capability, **kwargs)
        if capability == "ai.use" and kwargs.get("for_update"):
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        return project

    monkeypatch.setattr(project_access, "require_project", observe_final_lock)

    async def block_response(scope, receive, send):
        async def delayed_send(message):
            if (scope["path"].endswith("/cover/generate") and message["type"] == "http.response.start"
                    and message["status"] == 200):
                reached_send.set()
                await release_send.wait()
            await send(message)

        await editor_app(scope, receive, delayed_send)

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=block_response), base_url="http://testserver") as editor,
    ):
        project, user, base = await _setup(owner, editor)
        payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
        async with _server(payload=payload, mime="application/json") as (url, _, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
            request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "验证最终响应"}))
            revoke = None
            try:
                await asyncio.wait_for(reached_send.wait(), 15)
                assert holder.get("pid") is not None and len(calls) == 1
                revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{user['id']}", params={
                    "expected_revision": 2,
                }))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 15)
                assert not revoke.done()
                if stalled:
                    result = await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 15)
                    if isinstance(result[0], httpx.Response):
                        assert result[0].status_code >= 400 and "image_base64" not in result[0].text
                    else:
                        assert isinstance(result[0], BaseException)
                else:
                    release_send.set()
                    response = await asyncio.wait_for(request, 15)
                    assert response.status_code == 200 and base64.b64decode(response.json()["image_base64"]) == PNG
                assert (await asyncio.wait_for(revoke, 15)).status_code == 204
            finally:
                release_send.set()
                for job in (request, revoke):
                    if job is not None and not job.done():
                        job.cancel()
                        with suppress(asyncio.CancelledError):
                            await job
