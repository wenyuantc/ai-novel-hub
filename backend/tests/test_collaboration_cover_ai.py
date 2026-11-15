"""封面协作权限及真实 SDK 图片下载的物理发送边界。"""

import asyncio
import base64
import json
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.ai.providers import create_provider
from app.api.v1 import project_cover as cover_api
from app.core.errors import AppError
from app.models import AIConfig, Project, User, WritingStyle
from app.services import (
    ai_dispatch,
    collaboration_ai_guard,
    project_access,
    project_ai_config,
    style_service,
)
from app.services.authorization_service import AuthenticatedActor
from tests.cover_fixtures import PNG
from tests.helpers import create_ai_config, create_project, register
from tests.test_ai_visibility_dispatch import _server


async def _invite(owner, guest, base, username, role="editor", allow_owner_ai=False):
    response = await owner.post(f"{base}/collaborators", json={
        "username": username, "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert response.status_code == 201, response.text
    invitation = response.json()
    response = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _editor_image_guard(app, actor_id, project_id):
    async with app.state.sessionmaker() as db:
        actor = await db.get(User, actor_id)
        db.info["authenticated_actor"] = AuthenticatedActor(actor.id, actor.token_version)
        binding = await project_ai_config.resolve_project_model(
            db, actor_id, project_id, "image", cipher=app.state.cipher, allow_private=True, for_update=True,
        )
        stamp = await ai_dispatch.read_visibility_stamp(db, project_id)
        guard = collaboration_ai_guard.for_http(
            db, actor_id, binding, stamp, cipher=app.state.cipher, allow_private=True,
        )
        await db.rollback()
    return await guard.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=10)


@asynccontextmanager
async def _sequence_server(responses):
    calls, active = [], set()

    async def serve(reader, writer):
        current = asyncio.current_task()
        active.add(current)
        try:
            raw = (await reader.readuntil(b"\r\n\r\n")).decode()
            headers = dict(line.lower().split(": ", 1) for line in raw.split("\r\n")[1:] if line)
            calls.append(await reader.readexactly(int(headers.get("content-length", 0))))
            status, data = responses[len(calls) - 1]
            payload = json.dumps(data).encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: {len(payload)}\r\n"
                         "Content-Type: application/json\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            active.discard(current)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", calls
    finally:
        server.close()
        await server.wait_closed()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


async def test_cover_prompt_requires_ai_access_and_uses_only_actor_visible_style(
    app, client, make_client, system_admin,
):
    await register(client, "cover-prompt-owner")
    editor, viewer, stranger = make_client(), make_client(), make_client()
    editor_user = await register(editor, "cover-prompt-editor")
    viewer_user = await register(viewer, "cover-prompt-viewer")
    await register(stranger, "cover-prompt-stranger")
    style_response = await client.post("/api/v1/styles", json={
        "name": "作者私有名称", "description": "作者私有描述", "tone": "作者私有基调",
    })
    assert style_response.status_code == 201, style_response.text
    style = style_response.json()
    project = await create_project(client, default_style_id=style["id"], style="")
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-prompt-editor")
    await _invite(client, viewer, base, "cover-prompt-viewer", "viewer")
    path = f"{base}/cover/prompt"

    assert "作者私有名称" in (await client.get(path)).json()["prompt"]
    response = await editor.get(path)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"prompt", "warning"}
    for secret in ("作者私有名称", "作者私有描述", "作者私有基调"):
        assert secret not in json.dumps(response.json(), ensure_ascii=False)
    assert response.json()["warning"]
    assert (await viewer.get(path)).status_code == 403
    assert (await stranger.get(path)).status_code == 404
    assert (await system_admin.get(path)).status_code == 404

    async with app.state.sessionmaker() as db:
        (await db.get(User, editor_user["id"])).is_admin = True
        (await db.get(User, viewer_user["id"])).is_admin = True
        await db.commit()
    overlap = await editor.get(path)
    assert overlap.status_code == 200 and "作者私有" not in overlap.text
    assert (await viewer.get(path)).status_code == 403

    async with app.state.sessionmaker() as db:
        preset = await db.scalar(select(WritingStyle).where(WritingStyle.user_id.is_(None)).limit(1))
        assert preset is not None
        preset_id, preset_name = preset.id, preset.name
        (await db.get(Project, project["id"])).default_style_id = preset_id
        await db.commit()
    preset_response = await editor.get(path)
    assert preset_response.status_code == 200 and preset_name in preset_response.json()["prompt"]
    assert preset_response.json()["warning"] is None

    own = await editor.post("/api/v1/styles", json={"name": "编辑者风格", "description": "编辑者描述"})
    assert own.status_code == 201
    async with app.state.sessionmaker() as db:
        row = await db.get(WritingStyle, own.json()["id"])
        assert row.user_id == editor_user["id"]
        (await db.get(Project, project["id"])).default_style_id = row.id  # 旧损坏绑定也按实际 actor 过滤
        await db.commit()
    assert "编辑者风格" in (await editor.get(path)).json()["prompt"]
    assert "编辑者风格" not in (await client.get(path)).json()["prompt"]


async def test_prompt_database_failure_does_not_log_private_sql(app, client, make_client, monkeypatch, caplog):
    caught = []
    original_handler = app.exception_handlers[AppError]

    async def observe_error(request, exc):
        caught.append(exc)
        return await original_handler(request, exc)

    app.add_exception_handler(AppError, observe_error)
    await register(client, "cover-sql-prompt-owner")
    editor = make_client()
    await register(editor, "cover-sql-prompt-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-sql-prompt-editor")

    async def fail_style(*args, **kwargs):
        raise DBAPIError("SELECT private_style WHERE key=:secret", {"secret": "PRIVATE_STYLE_TOKEN"},
                         RuntimeError("PRIVATE_STYLE_DRIVER"))

    monkeypatch.setattr(style_service, "visible_default_style", fail_style)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver",
        cookies=editor.cookies,
    ) as safe_editor:
        response = await safe_editor.get(f"{base}/cover/prompt")
    assert response.status_code == 500 and "prompt" not in response.text
    assert len(caught) == 1 and caught[0].__context__ is None and caught[0].__cause__ is None
    for secret in ("private_style", "PRIVATE_STYLE_TOKEN", "PRIVATE_STYLE_DRIVER"):
        assert secret not in response.text and secret not in caplog.text


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_cover_generation_prefers_actor_and_only_falls_back_with_grant(
    app, client, make_client, system_admin,
):
    await register(client, "cover-model-owner")
    editor, viewer = make_client(), make_client()
    await register(editor, "cover-model-editor")
    viewer_user = await register(viewer, "cover-model-viewer")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-model-editor", allow_owner_ai=True)
    await _invite(client, viewer, base, "cover-model-viewer", "viewer")
    body = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
    app.state.provider_factory = create_provider
    async with (
        _server(payload=body, mime="application/json") as (owner_url, _, owner_headers, owner_body, owner_calls),
        _server(payload=body, mime="application/json") as (actor_url, _, actor_headers, actor_body, actor_calls),
    ):
        for event in (owner_headers, owner_body, actor_headers, actor_body):
            event.set()
        owner_config = await create_ai_config(client, model_type="image", model="owner-private-model",
                                              base_url=f"{owner_url}/v1/")
        actor_config = await create_ai_config(editor, model_type="image", model="actor-model",
                                              base_url=f"{actor_url}/v1/")
        path = f"{base}/cover/generate"
        assert (await viewer.post(path, json={"prompt": "查看者禁止生图"})).status_code == 403
        assert (await system_admin.post(path, json={"prompt": "管理员禁止代生图"})).status_code == 404
        async with app.state.sessionmaker() as db:
            (await db.get(User, viewer_user["id"])).is_admin = True
            await db.commit()
        assert (await viewer.post(path, json={"prompt": "管理员查看者仍禁止生图"})).status_code == 403
        response = await editor.post(path, json={"prompt": "真实触发者"})
        assert response.status_code == 200, response.text
        assert response.json() == {"image_base64": base64.b64encode(PNG).decode(), "mime_type": "image/png",
                                   "model_source": "actor"}
        assert len(actor_calls) == 1 and not owner_calls
        explicit_owner = await editor.post(path, json={"prompt": "不能选作者配置", "config_id": owner_config["id"]})
        assert explicit_owner.status_code == 404
        assert len(actor_calls) == 1 and not owner_calls
        assert (await editor.delete(f"/api/v1/ai-configs/{actor_config['id']}")).status_code == 204
        fallback = await editor.post(path, json={"prompt": "授权回退"})
        assert fallback.status_code == 200, fallback.text
        assert fallback.json()["model_source"] == "owner"
        assert set(fallback.json()) == {"image_base64", "mime_type", "model_source"}
        assert "owner-private-model" not in fallback.text and str(owner_config["id"]) not in fallback.json()
        assert len(owner_calls) == 1 and len(actor_calls) == 1


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_invalid_actor_image_model_never_falls_back_to_owner(app, client, make_client, monkeypatch):
    await register(client, "cover-invalid-owner")
    editor = make_client()
    await register(editor, "cover-invalid-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-invalid-editor", allow_owner_ai=True)
    async with _server(payload=b"{}", mime="application/json") as (url, _, headers, body, calls):
        headers.set()
        body.set()
        await create_ai_config(client, model_type="image", model="owner-image", base_url=f"{url}/v1/")
        actor_config = await create_ai_config(editor, model_type="image", model="invalid-image", base_url=f"{url}/v1/")
        async with app.state.sessionmaker() as db:
            (await db.get(AIConfig, actor_config["id"])).api_key_enc = "broken-ciphertext"
            await db.commit()
        response = await editor.post(f"{base}/cover/generate", json={"prompt": "禁止暗中回退"})
        assert response.status_code == 400, response.text
        assert not calls


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_editor_own_image_model_does_not_require_author_model_grant(app, client, make_client):
    app.state.provider_factory = create_provider
    await register(client, "cover-own-owner")
    editor = make_client()
    await register(editor, "cover-own-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-own-editor")
    payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, _, headers, body, calls):
        headers.set()
        body.set()
        await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
        response = await editor.post(f"{base}/cover/generate", json={"prompt": "本人模型"})
        assert response.status_code == 200, response.text
        assert response.json()["model_source"] == "actor" and len(calls) == 1


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_editor_without_model_grant_or_trusted_factory_cannot_send(app, client, make_client, monkeypatch):
    await register(client, "cover-trust-owner")
    editor = make_client()
    user = await register(editor, "cover-trust-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor, base, "cover-trust-editor")
    async with _server(payload=b"{}", mime="application/json") as (url, _, headers, body, calls):
        headers.set()
        body.set()
        await create_ai_config(client, model_type="image", model="owner-image", base_url=f"{url}/v1/")
        app.state.provider_factory = create_provider
        denied = await editor.post(f"{base}/cover/generate", json={"prompt": "未授权作者费用"})
        assert denied.status_code == 400 and not calls
        updated = await client.patch(f"{base}/collaborators/{user['id']}", json={
            "expected_revision": member["revision"], "allow_owner_ai": True,
        })
        assert updated.status_code == 200, updated.text
        constructed = []

        def unsafe_factory(*args):
            constructed.append(args)
            raise AssertionError("不能调用不可信工厂")

        app.state.provider_factory = unsafe_factory
        refused = await editor.post(f"{base}/cover/generate", json={"prompt": "不可信工厂"})
        assert refused.status_code == 409 and not calls and not constructed


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_owner_model_failure_does_not_leak_private_provider_details(app, client, make_client, monkeypatch):
    app.state.provider_factory = create_provider
    await register(client, "cover-error-owner")
    editor = make_client()
    await register(editor, "cover-error-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-error-editor", allow_owner_ai=True)
    payload = json.dumps({"error": {"message": "author-secret-model author-secret-key", "type": "auth"}}).encode()
    async with _server(status="401 Unauthorized", payload=payload, mime="application/json") as (
        url, _, headers, body, calls,
    ):
        headers.set()
        body.set()
        await create_ai_config(client, model_type="image", model="author-secret-model",
                               api_key="author-secret-key", base_url=f"{url}/v1/")
        response = await editor.post(f"{base}/cover/generate", json={"prompt": "生成封面"})
        assert response.status_code == 400 and response.json()["detail"] == "图片模型调用失败，请稍后重试"
        assert len(calls) == 1
        for secret in ("author-secret-model", "author-secret-key", url):
            assert secret not in response.text


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
@pytest.mark.parametrize("stage", ["initial", "final"])
async def test_model_check_database_failure_cannot_leak_sql_or_image(
    app, client, make_client, monkeypatch, caplog, stage,
):
    app.state.provider_factory = create_provider
    caught = []
    original_handler = app.exception_handlers[AppError]

    async def observe_error(request, exc):
        caught.append(exc)
        return await original_handler(request, exc)

    app.add_exception_handler(AppError, observe_error)
    await register(client, "cover-sql-owner")
    editor = make_client()
    await register(editor, "cover-sql-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-sql-editor", allow_owner_ai=True)
    original = project_access.require_project

    async def fail_check(db, actor_id, project_id, capability, **kwargs):
        final = kwargs.get("for_update") and kwargs.get("grant_identity")
        if capability == "ai.use" and (bool(final) if stage == "final" else not kwargs.get("for_update")):
            raise DBAPIError("SELECT owner_secret WHERE token=:private", {"private": "PRIVATE_DB_TOKEN"},
                             RuntimeError("PRIVATE_DRIVER_ERROR"))
        return await original(db, actor_id, project_id, capability, **kwargs)

    monkeypatch.setattr(project_access, "require_project", fail_check)
    payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, _, headers, body, calls):
        headers.set()
        body.set()
        await create_ai_config(client, model_type="image", model="author-sql-private", base_url=f"{url}/v1/")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://testserver",
            cookies=editor.cookies,
        ) as safe_editor:
            response = await safe_editor.post(f"{base}/cover/generate", json={"prompt": "数据库错误"})
        assert len(calls) == (1 if stage == "final" else 0) and response.status_code == 500
        assert "image_base64" not in response.text
        assert len(caught) == 1 and caught[0].__context__ is None and caught[0].__cause__ is None
        for secret in ("owner_secret", "PRIVATE_DB_TOKEN", "PRIVATE_DRIVER_ERROR", "author-sql-private"):
            assert secret not in response.text and secret not in caplog.text


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_revocation_after_binding_stops_first_image_send(app, client, make_client, monkeypatch):
    app.state.provider_factory = create_provider
    await register(client, "cover-binding-owner")
    editor = make_client()
    user = await register(editor, "cover-binding-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor, base, "cover-binding-editor")
    entered, release = asyncio.Event(), asyncio.Event()
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def after_binding(guard, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(guard, *args, **kwargs)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", after_binding)
    async with _server(payload=b"{}", mime="application/json") as (url, received, headers, body, calls):
        headers.set()
        body.set()
        await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
        request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "过期的授权"}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            revoked = await client.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert revoked.status_code == 204, revoked.text
            release.set()
            response = await asyncio.wait_for(request, 5)
            assert response.status_code in (403, 404, 409), response.text
            assert not received.is_set() and not calls and "image_base64" not in response.text
        finally:
            release.set()
            if not request.done():
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_final_image_response_keeps_grant_through_asgi_send(app, client, make_client, monkeypatch):
    app.state.provider_factory = create_provider
    await register(client, "cover-asgi-owner")
    registered_editor = make_client()
    user = await register(registered_editor, "cover-asgi-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, registered_editor, base, "cover-asgi-editor")
    reached_send, release_send = asyncio.Event(), asyncio.Event()

    async def delay_response(scope, receive, send):
        async def gate(message):
            if scope["path"].endswith("/cover/generate") and message["type"] == "http.response.start":
                reached_send.set()
                await release_send.wait()
            await send(message)

        await app(scope, receive, gate)

    payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, _, headers, body, calls):
        headers.set()
        body.set()
        await create_ai_config(registered_editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delay_response), base_url="http://testserver",
                                     cookies=registered_editor.cookies) as editor:
            request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "响应屏障"}))
            revoke = None
            try:
                await asyncio.wait_for(reached_send.wait(), 5)
                revoke = asyncio.create_task(client.delete(f"{base}/collaborators/{user['id']}", params={
                    "expected_revision": member["revision"],
                }))
                await asyncio.sleep(0.05)
                assert not revoke.done() and len(calls) == 1
                release_send.set()
                response = await asyncio.wait_for(request, 5)
                assert response.status_code == 200 and base64.b64decode(response.json()["image_base64"]) == PNG
                assert (await asyncio.wait_for(revoke, 5)).status_code == 204
            finally:
                release_send.set()
                for job in (request, revoke):
                    if job is not None and not job.done():
                        job.cancel()
                        with suppress(asyncio.CancelledError):
                            await job


async def test_stalled_prompt_response_releases_authorization(app, client, make_client, monkeypatch):
    monkeypatch.setattr(cover_api, "_OUTPUT_LEASE_SECONDS", 0.15)
    await register(client, "cover-prompt-slow-owner")
    registered_editor = make_client()
    user = await register(registered_editor, "cover-prompt-slow-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, registered_editor, base, "cover-prompt-slow-editor")
    reached, release = asyncio.Event(), asyncio.Event()

    async def stalled_response(scope, receive, send):
        async def gate(message):
            if (scope["path"].endswith("/cover/prompt") and message["type"] == "http.response.start"
                    and message["status"] == 200):
                reached.set()
                await release.wait()
            await send(message)

        await app(scope, receive, gate)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stalled_response), base_url="http://testserver",
                                 cookies=registered_editor.cookies) as editor:
        request = asyncio.create_task(editor.get(f"{base}/cover/prompt"))
        try:
            await asyncio.wait_for(reached.wait(), 5)
            revoked = await asyncio.wait_for(client.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": member["revision"],
            }), 3)
            assert revoked.status_code == 204, revoked.text
            assert request.done()
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_stalled_image_response_does_not_hold_authorization_indefinitely(app, client, make_client, monkeypatch):
    monkeypatch.setattr(cover_api, "_OUTPUT_LEASE_SECONDS", 0.15, raising=False)
    app.state.provider_factory = create_provider
    await register(client, "cover-slow-owner")
    registered_editor = make_client()
    user = await register(registered_editor, "cover-slow-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, registered_editor, base, "cover-slow-editor")
    reached, release = asyncio.Event(), asyncio.Event()

    async def stalled_response(scope, receive, send):
        async def gate(message):
            if (scope["path"].endswith("/cover/generate") and message["type"] == "http.response.start"
                    and message["status"] == 200):
                reached.set()
                await release.wait()
            await send(message)

        await app(scope, receive, gate)

    payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, _, headers, body, _):
        headers.set()
        body.set()
        await create_ai_config(registered_editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stalled_response), base_url="http://testserver",
                                     cookies=registered_editor.cookies) as editor:
            request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "阻塞客户端"}))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                revoked = await asyncio.wait_for(client.delete(f"{base}/collaborators/{user['id']}", params={
                    "expected_revision": member["revision"],
                }), 3)
                assert revoked.status_code == 204, revoked.text
                assert request.done()
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
@pytest.mark.parametrize("change", ["revoke", "reinvite", "delete", "downgrade", "model_grant", "token", "config"])
async def test_cover_response_is_withheld_when_identity_changes_after_response_headers(
    app, client, make_client, change,
):
    app.state.provider_factory = create_provider
    await register(client, "cover-response-owner")
    editor = make_client()
    editor_user = await register(editor, "cover-response-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor, base, "cover-response-editor", allow_owner_ai=True)
    payload = json.dumps({"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, received, headers, body, calls):
        owner_config = await create_ai_config(client, model_type="image", model="作者私密模型",
                                              base_url=f"{url}/v1/")
        request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "有效授权的请求"}))
        try:
            await asyncio.wait_for(received.wait(), 5)
            headers.set()
            member_path = f"{base}/collaborators/{editor_user['id']}"
            if change in ("revoke", "reinvite"):
                updated = await client.delete(member_path, params={"expected_revision": member["revision"]})
                assert updated.status_code == 204, updated.text
                if change == "reinvite":
                    await _invite(client, editor, base, "cover-response-editor", allow_owner_ai=True)
            elif change == "delete":
                updated = await client.delete(base)
                assert updated.status_code == 204, updated.text
            elif change in ("downgrade", "model_grant"):
                updated = await client.patch(member_path, json={
                    "expected_revision": member["revision"],
                    **({"role": "viewer"} if change == "downgrade" else {"allow_owner_ai": False}),
                })
                assert updated.status_code == 200, updated.text
            elif change == "token":
                updated = await editor.post("/api/v1/auth/change-password", json={
                    "old_password": "secret123", "new_password": "new-secret123",
                })
                assert updated.status_code == 204, updated.text
            else:
                updated = await client.patch(f"/api/v1/ai-configs/{owner_config['id']}", json={
                    "model": "作者的新私密模型",
                })
                assert updated.status_code == 200, updated.text
            assert not request.done()
            body.set()
            response = await asyncio.wait_for(request, 10)
            assert response.status_code in (400, 401, 403, 404, 409), response.text
            assert "image_base64" not in response.text and "作者私密模型" not in response.text
            assert len(calls) == 1
        finally:
            headers.set()
            body.set()
            if not request.done():
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_openai_image_url_download_rechecks_original_collaboration_grant(app, client, make_client):
    """真实 SDK 两次本地 HTTP，撤权先于下载时第二次不得发送。"""
    owner = await register(client, "cover-owner")
    editor_client = make_client()
    editor = await register(editor_client, "cover-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor_client, base, "cover-editor")
    async with _server(payload=PNG, mime="image/png") as (image_url, image_received, image_headers, image_body, hits):
        image_headers.set()
        image_body.set()
        payload = json.dumps({"created": 1, "data": [{"url": f"{image_url}/preview.png"}]}).encode()
        async with _server(payload=payload, mime="application/json") as (url, received, headers, body, calls):
            await create_ai_config(editor_client, model_type="image", model="image-test", base_url=f"{url}/v1/")
            bound = await _editor_image_guard(app, editor["id"], project["id"])
            generation = asyncio.create_task(bound.provider.generate_image("只发送一次", bound.model))
            try:
                await asyncio.wait_for(received.wait(), 5)
                headers.set()  # 第一跳响应头释放发送锁，图片 URL 仍在被阻塞的响应体内。
                revoked = await client.delete(
                    f"{base}/collaborators/{editor['id']}", params={"expected_revision": member["revision"]},
                )
                assert revoked.status_code == 204, revoked.text
                body.set()
                with pytest.raises(ai_dispatch.VisibilityPolicyError):
                    await asyncio.wait_for(generation, 5)
                assert len(calls) == 1 and not hits and not image_received.is_set()
            finally:
                headers.set()
                body.set()
                if not generation.done():
                    generation.cancel()
                    with suppress(asyncio.CancelledError):
                        await generation
    assert owner["id"] != editor["id"]


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_editor_http_url_download_stops_after_revocation(app, client, make_client):
    app.state.provider_factory = create_provider
    await register(client, "cover-url-http-owner")
    editor = make_client()
    user = await register(editor, "cover-url-http-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor, base, "cover-url-http-editor")
    async with _server(payload=PNG, mime="image/png") as (image_url, image_received, image_headers, image_body, hits):
        image_headers.set()
        image_body.set()
        payload = json.dumps({"created": 1, "data": [{"url": f"{image_url}/preview.png"}]}).encode()
        async with _server(payload=payload, mime="application/json") as (url, received, headers, body, calls):
            await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
            request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "下载前撤权"}))
            try:
                await asyncio.wait_for(received.wait(), 5)
                headers.set()
                revoked = await client.delete(f"{base}/collaborators/{user['id']}", params={
                    "expected_revision": member["revision"],
                })
                assert revoked.status_code == 204, revoked.text
                body.set()
                response = await asyncio.wait_for(request, 5)
                assert response.status_code in (403, 404, 409) and "image_base64" not in response.text
                assert len(calls) == 1 and not hits and not image_received.is_set()
            finally:
                headers.set()
                body.set()
                if not request.done():
                    request.cancel()
                    with suppress(asyncio.CancelledError):
                        await request


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_openai_image_download_redirect_rechecks_each_hop(app, client, make_client):
    await register(client, "cover-redirect-owner")
    editor_client = make_client()
    editor = await register(editor_client, "cover-redirect-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor_client, base, "cover-redirect-editor")
    async with _server(payload=PNG, mime="image/png") as (final_url, final_received, final_headers, final_body, final):
        final_headers.set()
        final_body.set()
        # 非空响应体可阻止 HTTPX 在撤权提交之前立刻跟随重定向。
        async with _server(status="307 Temporary Redirect", payload=b"wait", headers={
            "Location": f"{final_url}/private.png",
        }) as (
            image_url, image_received, image_headers, image_body, image_calls,
        ):
            payload = json.dumps({"created": 1, "data": [{"url": f"{image_url}/preview.png"}]}).encode()
            async with _server(payload=payload, mime="application/json") as (url, _, headers, body, calls):
                headers.set()
                body.set()
                await create_ai_config(editor_client, model_type="image", model="image-test", base_url=f"{url}/v1/")
                bound = await _editor_image_guard(app, editor["id"], project["id"])
                generation = asyncio.create_task(bound.provider.generate_image("图片下载", bound.model))
                try:
                    await asyncio.wait_for(image_received.wait(), 5)
                    image_headers.set()
                    revoked = await client.delete(f"{base}/collaborators/{editor['id']}", params={
                        "expected_revision": member["revision"],
                    })
                    assert revoked.status_code == 204, revoked.text
                    image_body.set()
                    with pytest.raises(ai_dispatch.VisibilityPolicyError):
                        await asyncio.wait_for(generation, 5)
                    assert len(calls) == len(image_calls) == 1 and not final and not final_received.is_set()
                finally:
                    image_headers.set()
                    image_body.set()
                    if not generation.done():
                        generation.cancel()
                        with suppress(asyncio.CancelledError):
                            await generation


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
@pytest.mark.parametrize("phase", ["generate", "download"])
async def test_cancelled_image_request_releases_actual_send_lease(app, client, make_client, monkeypatch, phase):
    app.state.provider_factory = create_provider
    await register(client, "cover-cancel-owner")
    editor = make_client()
    user = await register(editor, "cover-cancel-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor, base, "cover-cancel-editor")
    async with _server(payload=PNG, mime="image/png") as (image_url, image_received, image_headers, image_body, hits):
        payload = json.dumps({"created": 1, "data": [{"url": f"{image_url}/image.png"}]}).encode()
        async with _server(payload=payload, mime="application/json") as (url, received, headers, body, calls):
            await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
            request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "取消生图"}))
            try:
                await asyncio.wait_for(received.wait(), 5)
                if phase == "download":
                    headers.set()
                    body.set()
                    await asyncio.wait_for(image_received.wait(), 5)
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                revoked = await asyncio.wait_for(client.delete(f"{base}/collaborators/{user['id']}", params={
                    "expected_revision": member["revision"],
                }), 5)
                assert revoked.status_code == 204, revoked.text
                assert len(calls) == 1 and len(hits) == (1 if phase == "download" else 0)
            finally:
                headers.set()
                body.set()
                image_headers.set()
                image_body.set()
                if not request.done():
                    request.cancel()
                    with suppress(asyncio.CancelledError):
                        await request


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_openai_size_retry_rechecks_editor_grant(app, client, make_client, monkeypatch):
    app.state.provider_factory = create_provider
    await register(client, "cover-retry-owner")
    editor = make_client()
    user = await register(editor, "cover-retry-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor, base, "cover-retry-editor")
    payload = json.dumps({"error": {"message": "Invalid size: 1024x1536 is not supported", "type": "invalid_request"}})
    async with _server(status="400 Bad Request", payload=payload.encode(), mime="application/json") as (
        url, received, headers, body, calls,
    ):
        await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
        request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "不可重试发送"}))
        try:
            await asyncio.wait_for(received.wait(), 5)
            headers.set()
            revoked = await client.delete(f"{base}/collaborators/{user['id']}", params={
                "expected_revision": member["revision"],
            })
            assert revoked.status_code == 204, revoked.text
            body.set()
            response = await asyncio.wait_for(request, 5)
            assert response.status_code in (403, 404, 409), response.text
            assert len(calls) == 1 and "image_base64" not in response.text
        finally:
            headers.set()
            body.set()
            if not request.done():
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_openai_size_retry_succeeds_only_with_original_grant(app, client, make_client, monkeypatch):
    app.state.provider_factory = create_provider
    await register(client, "cover-retry-ok-owner")
    editor = make_client()
    await register(editor, "cover-retry-ok-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, editor, base, "cover-retry-ok-editor")
    async with _sequence_server([
        ("400 Bad Request", {"error": {"message": "Invalid size: not supported", "type": "invalid_request"}}),
        ("200 OK", {"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}),
    ]) as (url, calls):
        await create_ai_config(editor, model_type="image", model="image-test", base_url=f"{url}/v1/")
        response = await editor.post(f"{base}/cover/generate", json={"prompt": "尺寸兼容"})
        assert response.status_code == 200, response.text
        assert response.json()["model_source"] == "actor" and len(calls) == 2
        assert json.loads(calls[0])["size"] == "1024x1536" and "size" not in json.loads(calls[1])


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
@pytest.mark.parametrize("revoke", [False, True])
async def test_gemini_image_uses_real_guarded_transport_and_revalidates_output(
    app, client, make_client, monkeypatch, revoke,
):
    app.state.provider_factory = create_provider
    await register(client, "gemini-cover-owner")
    editor = make_client()
    user = await register(editor, "gemini-cover-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(client, editor, base, "gemini-cover-editor")
    payload = json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"inlineData": {
        "mimeType": "image/png", "data": base64.b64encode(PNG).decode(),
    }}]}}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, received, headers, body, calls):
        await create_ai_config(editor, provider="gemini", model_type="image", model="gemini-2.5-flash-image",
                               base_url=url)
        request = asyncio.create_task(editor.post(f"{base}/cover/generate", json={"prompt": "原生图片"}))
        try:
            await asyncio.wait_for(received.wait(), 5)
            headers.set()
            if revoke:
                result = await client.delete(f"{base}/collaborators/{user['id']}", params={
                    "expected_revision": member["revision"],
                })
                assert result.status_code == 204, result.text
            body.set()
            response = await asyncio.wait_for(request, 10)
            assert len(calls) == 1
            assert json.loads(calls[0])["generationConfig"]["responseModalities"] == ["IMAGE"]
            if revoke:
                assert response.status_code in (403, 404, 409) and "image_base64" not in response.text
            else:
                assert response.status_code == 200, response.text
                assert base64.b64decode(response.json()["image_base64"]) == PNG
                assert response.json()["model_source"] == "actor"
        finally:
            headers.set()
            body.set()
            if not request.done():
                request.cancel()
                with suppress(asyncio.CancelledError):
                    await request
