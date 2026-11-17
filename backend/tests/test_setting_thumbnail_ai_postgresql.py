"""设定缩略图 AI：真实 PostgreSQL 双连接与本地 HTTP Provider 的授权和版本竞争。"""

import asyncio
import base64
import json
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import func, select, text

from app.ai.providers import create_provider
from app.api.v1 import setting_images as image_api
from app.models.setting_images import SettingImageResource
from app.services import collaboration_ai_guard, project_thumbnail_ai, series_thumbnail_ai
from tests.helpers import create_ai_config, create_project, register
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_setting_thumbnails import image_data

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


def _payload(color="blue"):
    encoded = base64.b64encode(image_data(color)).decode("ascii")
    return json.dumps({"created": 1, "data": [{"b64_json": encoded}]}).encode()


def _enable_provider(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True
        app.state.provider_factory = create_provider


async def _setup(owner, editor, scope):
    await register(owner, "thumbnail-ai-owner")
    editor_user = await register(editor, "thumbnail-ai-editor")
    if scope == "project":
        project = await create_project(owner)
        base = f"/api/v1/projects/{project['id']}"
        created = await owner.post(f"{base}/characters", json={"name": "有帽子的旅人", "appearance": "蓝色斗篷"})
        assert created.status_code == 201, created.text
        entry_id = created.json()["id"]
        thumbnail = f"{base}/settings/character/{entry_id}/thumbnail"
    else:
        created = await owner.post("/api/v1/series", json={"name": "未被作品采用的系列"})
        assert created.status_code == 201, created.text
        series = created.json()
        base = f"/api/v1/series/{series['id']}"
        created = await owner.post(f"{base}/entries", json={
            "kind": "character",
            "data": {"name": "系列旅人", "appearance": "蓝色斗篷"},
            "ai_settings": {"ai_mode": "manual_only"},
            "expected_revision": series["revision"],
        })
        assert created.status_code == 201, created.text
        entry_id = created.json()["item"]["id"]
        thumbnail = f"{base}/entries/{entry_id}/thumbnail"

    member = await _invite(owner, editor, scope, base)
    if scope == "series":
        current = await owner.get(base)
        assert current.status_code == 200, current.text
        revision = current.json()["revision"]
    else:
        revision = None
    return editor_user, base, thumbnail, entry_id, member, revision


async def _invite(owner, editor, scope, base):
    data = {"username": "thumbnail-ai-editor"}
    if scope == "project":
        data["role"] = "editor"
    invited = await owner.post(f"{base}/collaborators", json=data)
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/{scope}/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def _prompt(client, thumbnail):
    response = await client.get(f"{thumbnail}/ai/prompt")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["prompt"] and result["prompt_token"]
    return result


def _generate_body(prompt, version="", revision=None):
    body = {
        "prompt": prompt["prompt"],
        "prompt_token": prompt["prompt_token"],
        "expected_version": version,
    }
    if revision is not None:
        body["expected_revision"] = revision
    return body


async def _preview(client, thumbnail, version="", revision=None):
    prompt = await _prompt(client, thumbnail)
    response = await client.post(f"{thumbnail}/ai/generate", json=_generate_body(prompt, version, revision))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["model_source"] == "actor"
    assert result["mime_type"] == "image/webp"
    assert result["preview_token"] and result["image_base64"]
    return result


async def _upload(client, thumbnail, version="", revision=None, color="red"):
    data = {"expected_version": version}
    if revision is not None:
        data["expected_revision"] = str(revision)
    response = await client.put(
        thumbnail, files={"file": ("original.png", image_data(color), "image/png")}, data=data,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    if revision is not None:
        return result["thumbnail"]["version"], result["revision"]
    return result["version"], None


async def _cancel(task):
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("scope", ["project", "series"])
async def test_preview_cannot_cross_revocation_and_new_invitation(postgres_collaboration_apps, scope):
    """预览由旧邀请产生；撤权后重新接受的新邀请不能替旧预览授权。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        editor_user, base, thumbnail, _, member, revision = await _setup(owner, editor, scope)
        version, revision = await _upload(owner, thumbnail, revision=revision)
        original = await owner.get(thumbnail)
        assert original.status_code == 200
        async with _server(payload=_payload(), mime="application/json") as (url, _, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            preview = await _preview(editor, thumbnail, version, revision)
            assert len(calls) == 1

        revoked = await owner.delete(f"{base}/collaborators/{editor_user['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204, revoked.text
        await _invite(owner, editor, scope, base)
        result = await editor.post(f"{thumbnail}/ai/apply", json={
            "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
        })
        assert result.status_code in (403, 404, 409), result.text
        current = await owner.get(thumbnail)
        assert current.status_code == 200 and current.content == original.content
        async with owner_app.state.sessionmaker() as db:
            count = await db.scalar(select(func.count()).select_from(SettingImageResource))
            assert count == 1  # 预览和拒绝确认均不写入第二个图片资源


@pytest.mark.parametrize("scope", ["project", "series"])
async def test_preview_does_not_overwrite_concurrent_image(postgres_collaboration_apps, scope):
    """另一连接换图后，旧预览必须保留新图并返回冲突。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        _, _, thumbnail, _, _, revision = await _setup(owner, editor, scope)
        version, revision = await _upload(owner, thumbnail, revision=revision)
        async with _server(payload=_payload(), mime="application/json") as (url, _, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            preview = await _preview(editor, thumbnail, version, revision)
            assert len(calls) == 1

        next_version, _ = await _upload(owner, thumbnail, version, revision, color="green")
        replacement = await owner.get(thumbnail)
        assert replacement.status_code == 200
        result = await editor.post(f"{thumbnail}/ai/apply", json={
            "image_base64": preview["image_base64"], "preview_token": preview["preview_token"],
        })
        assert result.status_code == 409, result.text
        current = await owner.get(thumbnail)
        assert current.status_code == 200 and current.content == replacement.content
        assert current.headers["etag"] == f'"{next_version}"'


async def test_project_revocation_before_actual_send_stops_provider(postgres_collaboration_apps, monkeypatch):
    """绑定后、实际发送前撤权，Provider 不应收到旧请求。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    entered, release = asyncio.Event(), asyncio.Event()
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def paused(guard, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(guard, *args, **kwargs)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", paused)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        editor_user, base, thumbnail, _, member, _ = await _setup(owner, editor, "project")
        async with _server(payload=_payload(), mime="application/json") as (url, received, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            prompt = await _prompt(editor, thumbnail)
            request = asyncio.create_task(editor.post(f"{thumbnail}/ai/generate", json=_generate_body(prompt)))
            try:
                await asyncio.wait_for(entered.wait(), 15)
                revoked = await owner.delete(f"{base}/collaborators/{editor_user['id']}", params={
                    "expected_revision": member["revision"],
                })
                assert revoked.status_code == 204, revoked.text
                release.set()
                result = await asyncio.wait_for(request, 15)
                assert result.status_code in (403, 404, 409), result.text
                assert not received.is_set() and not calls and "image_base64" not in result.text
            finally:
                release.set()
                await _cancel(request)


async def test_project_send_fences_revocation_until_headers(postgres_collaboration_apps, monkeypatch):
    """请求字节已发时撤权等待到响应头，随后旧结果不得交付。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    holder, verified = {}, asyncio.Event()
    original = collaboration_ai_guard.SendGuard.verify

    async def observed(guard, db):
        await original(guard, db)
        holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        verified.set()

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "verify", observed)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        editor_user, base, thumbnail, _, member, _ = await _setup(owner, editor, "project")
        async with _server(payload=_payload(), mime="application/json") as (url, received, headers, body, calls):
            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            prompt = await _prompt(editor, thumbnail)
            request = asyncio.create_task(editor.post(f"{thumbnail}/ai/generate", json=_generate_body(prompt)))
            revoke = None
            try:
                await asyncio.wait_for(verified.wait(), 15)
                await asyncio.wait_for(received.wait(), 15)
                revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{editor_user['id']}", params={
                    "expected_revision": member["revision"],
                }))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 15)
                assert not revoke.done() and not request.done()
                headers.set()
                assert (await asyncio.wait_for(revoke, 15)).status_code == 204
                assert not request.done()
                body.set()
                result = await asyncio.wait_for(request, 15)
                assert result.status_code in (403, 404, 409), result.text
                assert "image_base64" not in result.text and len(calls) == 1
            finally:
                headers.set()
                body.set()
                await _cancel(request)
                await _cancel(revoke)


async def test_series_send_fences_revocation_until_headers(postgres_collaboration_apps, monkeypatch):
    """系列发送持自己的授权屏障；撤权完成后不能交付迟到预览。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    holder, verified = {}, asyncio.Event()
    original = series_thumbnail_ai._verify

    async def observed(db, *args, **kwargs):
        await original(db, *args, **kwargs)
        holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        verified.set()

    monkeypatch.setattr(series_thumbnail_ai, "_verify", observed)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        editor_user, base, thumbnail, _, member, revision = await _setup(owner, editor, "series")
        async with _server(payload=_payload(), mime="application/json") as (url, received, headers, body, calls):
            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            prompt = await _prompt(editor, thumbnail)
            request = asyncio.create_task(editor.post(
                f"{thumbnail}/ai/generate", json=_generate_body(prompt, revision=revision),
            ))
            revoke = None
            try:
                await asyncio.wait_for(verified.wait(), 15)
                await asyncio.wait_for(received.wait(), 15)
                revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{editor_user['id']}", params={
                    "expected_revision": member["revision"],
                }))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 15)
                assert not revoke.done() and not request.done()
                headers.set()
                assert (await asyncio.wait_for(revoke, 15)).status_code == 204
                assert not request.done()
                body.set()
                result = await asyncio.wait_for(request, 15)
                assert result.status_code in (403, 404, 409), result.text
                assert "image_base64" not in result.text and len(calls) == 1
            finally:
                headers.set()
                body.set()
                await _cancel(request)
                await _cancel(revoke)


@pytest.mark.parametrize("scope", ["project", "series"])
@pytest.mark.parametrize("action", ["prompt", "generate"])
@pytest.mark.parametrize("termination", ["timeout", "cancel"])
async def test_ai_response_holds_authorization_and_releases_on_stalled_client(
    postgres_collaboration_apps, monkeypatch, scope, action, termination,
):
    """真实 ASGI 发送未开始时保留授权屏障；超时或取消后释放锁。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    monkeypatch.setattr(image_api, "_OUTPUT_LEASE_SECONDS", 1.5)
    holder, reached, release = {}, asyncio.Event(), asyncio.Event()
    if scope == "project":
        original = project_thumbnail_ai._identity

        async def observed(db, *args, **kwargs):
            result = await original(db, *args, **kwargs)
            if kwargs.get("lock"):
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            return result

        monkeypatch.setattr(project_thumbnail_ai, "_identity", observed)
    else:
        original = series_thumbnail_ai._lock_series

        async def observed(db, *args, **kwargs):
            result = await original(db, *args, **kwargs)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            return result

        monkeypatch.setattr(series_thumbnail_ai, "_lock_series", observed)

    async def block_response(asgi_scope, receive, send):
        async def delayed_send(message):
            if (asgi_scope["path"].endswith(f"/thumbnail/ai/{action}")
                    and message["type"] == "http.response.start" and message["status"] == 200):
                reached.set()
                await release.wait()
            await send(message)

        await editor_app(asgi_scope, receive, delayed_send)

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=block_response), base_url="http://testserver") as editor,
    ):
        editor_user, base, thumbnail, _, member, revision = await _setup(owner, editor, scope)
        async with _server(payload=_payload(), mime="application/json") as (url, _, headers, body, calls):
            headers.set()
            body.set()
            if action == "generate":
                await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
                prompt = await _prompt(editor, thumbnail)
                request = asyncio.create_task(editor.post(
                    f"{thumbnail}/ai/generate", json=_generate_body(prompt, revision=revision),
                ))
            else:
                request = asyncio.create_task(editor.get(f"{thumbnail}/ai/prompt"))
            revoke = None
            try:
                await asyncio.wait_for(reached.wait(), 15)
                pid = holder.get("pid")
                assert pid is not None
                revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{editor_user['id']}", params={
                    "expected_revision": member["revision"],
                }))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 15)
                assert not revoke.done()
                if termination == "cancel":
                    request.cancel()
                outcome = (await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 15))[0]
                if termination == "cancel":
                    assert isinstance(outcome, asyncio.CancelledError)
                elif isinstance(outcome, httpx.Response):
                    assert outcome.status_code >= 400
                    assert "prompt_token" not in outcome.text and "image_base64" not in outcome.text
                else:
                    assert isinstance(outcome, BaseException)
                assert (await asyncio.wait_for(revoke, 15)).status_code == 204
                assert len(calls) == (1 if action == "generate" else 0)
            finally:
                release.set()
                await _cancel(request)
                await _cancel(revoke)


async def test_series_editor_uses_only_own_model(postgres_collaboration_apps):
    """系列没有作品式作者模型授权，无本人图片模型不能回退作者配置。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        _, _, thumbnail, _, _, revision = await _setup(owner, editor, "series")
        async with _server(payload=_payload(), mime="application/json") as (url, received, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(owner, model_type="image", model="owner-private-image", base_url=f"{url}/v1/")
            prompt = await _prompt(editor, thumbnail)
            missing = await editor.post(f"{thumbnail}/ai/generate", json=_generate_body(prompt, revision=revision))
            assert missing.status_code in (400, 403, 404), missing.text
            assert not received.is_set() and not calls and "owner-private-image" not in missing.text

            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            result = await editor.post(f"{thumbnail}/ai/generate", json=_generate_body(prompt, revision=revision))
            assert result.status_code == 200, result.text
            assert result.json()["model_source"] == "actor"
            assert len(calls) == 1 and json.loads(calls[0])["model"] == "editor-image"
            assert "owner-private-image" not in result.text


async def test_series_policy_change_rejects_old_prompt_before_send(postgres_collaboration_apps):
    """提示词取得后系列策略收紧，不得用新策略代次发送旧文本。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        _, base, thumbnail, entry_id, _, revision = await _setup(owner, editor, "series")
        async with _server(payload=_payload(), mime="application/json") as (url, received, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            prompt = await _prompt(editor, thumbnail)
            changed = await owner.patch(f"{base}/entries/{entry_id}", json={
                "expected_revision": revision, "ai_settings": {"ai_mode": "never"},
            })
            assert changed.status_code == 200, changed.text
            result = await editor.post(f"{thumbnail}/ai/generate", json=_generate_body(prompt, revision=revision))
            assert result.status_code in (400, 403, 404, 409), result.text
            assert not received.is_set() and not calls and "image_base64" not in result.text


async def test_project_policy_change_rejects_old_prompt_before_send(postgres_collaboration_apps):
    """作品字段被隐藏后，已显示的旧默认词不得再次外发。"""
    owner_app, editor_app = postgres_collaboration_apps
    _enable_provider(owner_app, editor_app)
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=editor_app), base_url="http://testserver") as editor,
    ):
        _, base, thumbnail, entry_id, _, _ = await _setup(owner, editor, "project")
        async with _server(payload=_payload(), mime="application/json") as (url, received, headers, body, calls):
            headers.set()
            body.set()
            await create_ai_config(editor, model_type="image", model="editor-image", base_url=f"{url}/v1/")
            prompt = await _prompt(editor, thumbnail)
            assert "蓝色斗篷" in prompt["prompt"]
            settings_path = f"{base}/setting-entries/character/{entry_id}/ai-settings"
            settings = await owner.get(settings_path)
            assert settings.status_code == 200, settings.text
            changed = await owner.patch(settings_path, json={
                "expected_ai_visibility_revision": settings.json()["ai_visibility_revision"],
                "ai_hidden_builtin_fields": ["appearance"],
            })
            assert changed.status_code == 200, changed.text
            result = await editor.post(f"{thumbnail}/ai/generate", json=_generate_body(prompt))
            assert result.status_code in (400, 403, 404, 409), result.text
            assert not received.is_set() and not calls and "image_base64" not in result.text
            updated = await _prompt(editor, thumbnail)
            assert "蓝色斗篷" not in updated["prompt"]
