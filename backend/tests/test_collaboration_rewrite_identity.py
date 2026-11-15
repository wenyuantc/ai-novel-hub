"""协作改写经真实 HTTP、本机模型服务器和 SQLite 验证。"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.ai.providers import create_provider
from app.api.sse import sse_response
from app.models import GenerationLog, User
from app.services import generation_log_service, rewrite_service
from tests.helpers import create_ai_config, create_project, parse_sse, register, wait_until
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_rich_document_api import KEY, add_section, save_payload


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True}


@asynccontextmanager
async def _http_clients(app):
    app.state.provider_factory = create_provider
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started, within=10)
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as owner,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as editor,
        ):
            yield owner, editor
    finally:
        server.should_exit = True
        await asyncio.wait_for(runner, 10)


async def _world(owner, editor, model_url, *, source="actor"):
    await register(owner, "rewrite-owner")
    editor_user = await register(editor, "rewrite-editor")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    chapter = await owner.post(f"{base}/chapters", json={"title": "章节", "content": "旧正文示例"})
    assert chapter.status_code == 201, chapter.text
    if source == "actor":
        await create_ai_config(editor, base_url=f"{model_url}/v1/", model="editor-model")
    else:
        await create_ai_config(owner, base_url=f"{model_url}/v1/", model="owner-private-model")
    invite = await owner.post(f"{base}/collaborators", json={
        "username": "rewrite-editor", "role": "editor", "allow_owner_ai": source == "owner",
    })
    assert invite.status_code == 201, invite.text
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return project, chapter.json(), editor_user


async def _section(owner, project, chapter):
    base = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
    scene = (await owner.get(f"{base}/scenes")).json()["items"][0]
    document_url = f"{base}/scenes/{scene['id']}/document"
    document = save_payload((await owner.get(document_url)).json())
    add_section(document)
    document["document"]["sections"][0].update(title="隐藏标题", content="前缀选中后缀")
    saved = await owner.put(document_url, json=document)
    assert saved.status_code == 200, saved.text
    request = {
        "expected_content_revision": saved.json()["content_revision"],
        "expected_planning_revision": saved.json()["planning_revision"],
        "request_id": str(uuid4()), "confirmed": True, "op": "rewrite", "start": 2, "end": 4,
        "selected_hash": sha256("选中".encode()).hexdigest(),
    }
    return f"{base}/scenes/{scene['id']}/sections/{KEY}/rewrite", document_url, request


async def test_editor_preview_and_apply_require_original_preview_evidence(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, _editor_user = await _world(owner, editor, url)
            apply_url = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}/rewrite/apply"
            payload = {"start": 0, "end": 2, "original": "旧正", "replacement": "新文"}
            rejected = await editor.post(apply_url, json=payload)
            assert rejected.status_code == 409, rejected.text
            preview = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            assert preview.status_code == 200, preview.text
            events = parse_sse(preview.text)
            assert events[-1][0] == "done", preview.text
            assert len(requests) == 1
            result = events[-1][1]
            applied = await editor.post(apply_url, json={**payload, "log_id": result["log_id"]})
            assert applied.status_code == 200, applied.text
            assert applied.json()["content"] == "新文文示例"


async def test_editor_scene_preview_uses_guarded_output(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, _editor_user = await _world(owner, editor, url)
            base = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
            scene = (await owner.get(f"{base}/scenes")).json()["items"][0]
            document = (await owner.get(f"{base}/scenes/{scene['id']}/document")).json()
            response = await editor.post(
                f"{base}/scenes/{scene['id']}/rewrite", json={
                    "op": "rewrite", "start": 0, "end": 2,
                    "expected_content_revision": document["content_revision"],
                    "expected_planning_revision": document["planning_revision"],
                },
            )
            events = parse_sse(response.text)
            assert response.status_code == 200 and events[-1][0] == "done", response.text
            assert events[-1][1]["scene_id"] == scene["id"] and len(requests) == 1


@pytest.mark.parametrize("termination", ["timeout", "client_cancel"])
async def test_output_frame_releases_auth_and_closes_generator_on_interruption(app, monkeypatch, termination):
    from tests.test_collaboration_rewrite_identity_postgresql import _delayed_editor

    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(rewrite_service, "OUTPUT_LEASE_SECONDS", 0.8 if termination == "timeout" else 5.0)
    original_save = generation_log_service.save_log

    async def observe_save(*args, **kwargs):
        try:
            return await original_save(*args, **kwargs)
        finally:
            cleaned.set()

    monkeypatch.setattr(generation_log_service, "save_log", observe_save)
    async with (
        _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
        _http_clients(app) as (owner, _unused),
        _delayed_editor(app, entered, release) as editor,
    ):
        headers.set()
        body.set()
        project, chapter, editor_user = await _world(owner, editor, url)
        pending = asyncio.create_task(editor.post(
            f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
        ))
        revoke = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            revoke = asyncio.create_task(owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                params={"expected_revision": 2},
            ))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(revoke), 0.15)
            if termination == "client_cancel":
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            result = await asyncio.wait_for(revoke, 5)
            assert result.status_code == 204, result.text
            await asyncio.wait_for(cleaned.wait(), 5)
            assert len(requests) == 1
        finally:
            release.set()
            for task in (pending, revoke):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (pending, revoke) if task is not None), return_exceptions=True)


async def test_output_frame_timeout_includes_lock_acquisition():
    acquired, released, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    never = asyncio.Event()
    sent = []

    @asynccontextmanager
    async def blocked_lease():
        acquired.set()
        try:
            await never.wait()
            yield
        finally:
            released.set()

    async def events():
        try:
            yield "delta", {"text": "不得输出正文"}
        finally:
            closed.set()

    async def send(message):
        sent.append(message)

    response = sse_response(events(), frame_lease=blocked_lease, frame_timeout=0.05)
    await asyncio.wait_for(response.stream_response(send), 2)
    assert acquired.is_set() and released.is_set() and closed.is_set()
    assert all("不得输出正文".encode() not in message.get("body", b"") for message in sent)


async def test_output_scope_failure_is_safe_and_recorded_as_error(app, monkeypatch, caplog):
    secret = "private SQL and key"

    async def fail_scope(*_args, **_kwargs):
        raise DBAPIError(f"SELECT {secret}", {"prompt": secret}, RuntimeError(secret))

    monkeypatch.setattr(rewrite_service, "_stream_scope", fail_scope)
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            _project, chapter, editor_user = await _world(owner, editor, url)
            response = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            events = parse_sse(response.text)
            assert response.status_code == 200 and events[-1][0] == "error"
            assert events[-1][1] == {"detail": "权限或模型授权已变化，已停止发送", "code": "permission_denied"}
            assert not [kind for kind, _data in events if kind in {"delta", "done"}]
            assert secret not in response.text and secret not in caplog.text and len(requests) == 1
            async with app.state.sessionmaker() as db:
                log = await db.scalar(select(GenerationLog).where(GenerationLog.actor_user_id == editor_user["id"]))
                assert log is not None and log.status == "error"


async def test_output_close_failure_invalidates_connection_without_leaking(app, monkeypatch, caplog):
    secret = "private close SQL and content"
    invalidated = asyncio.Event()
    original = rewrite_service._stream_scope

    async def failing_close(db, job):
        await original(db, job)
        invalidate = db.invalidate

        async def fail():
            raise RuntimeError(secret)

        async def tracked_invalidate():
            try:
                await invalidate()
            finally:
                invalidated.set()

        db.close = fail
        db.invalidate = tracked_invalidate

    monkeypatch.setattr(rewrite_service, "_stream_scope", failing_close)
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, editor_user = await _world(owner, editor, url)
            try:
                response = await editor.post(
                    f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
                )
                assert secret not in response.text
            except httpx.HTTPError:
                pass  # 一帧可能已发送，连接中断不能重发。
            await asyncio.wait_for(invalidated.wait(), 5)
            revoked = await asyncio.wait_for(owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                params={"expected_revision": 2},
            ), 5)
            assert revoked.status_code == 204 and len(requests) == 1 and secret not in caplog.text


async def test_owner_model_error_does_not_expose_private_details(app, caplog):
    secret = "owner-private-model author-secret-key"
    payload = json.dumps({"error": {"message": secret, "type": "invalid_request_error"}}).encode()
    async with _server(status="401 Unauthorized", mime="application/json", payload=payload) as (
        url, _received, headers, body, requests,
    ):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            _project, chapter, editor_user = await _world(owner, editor, url, source="owner")
            response = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            events = parse_sse(response.text)
            assert response.status_code == 200 and events[-1] == ("error", {"detail": "模型调用失败，请稍后重试"})
            assert secret not in response.text and secret not in caplog.text and len(requests) == 1
            async with app.state.sessionmaker() as db:
                log = await db.scalar(select(GenerationLog).where(GenerationLog.actor_user_id == editor_user["id"]))
                assert log is not None and log.status == "error" and secret not in (log.error or "")


async def test_failure_recording_success_does_not_log_sql_or_content(app, monkeypatch, caplog):
    secret = "private SQL parameter and story content"

    async def fail_write(*_args, **_kwargs):
        raise DBAPIError(f"INSERT {secret}", {"content": secret}, RuntimeError(secret))

    monkeypatch.setattr(generation_log_service, "write_log", fail_write)
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            _project, chapter, _editor_user = await _world(owner, editor, url)
            response = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            events = parse_sse(response.text)
            assert response.status_code == 200 and events[-1] == ("error", {"detail": "改写失败：服务器内部错误"})
            assert secret not in response.text and secret not in caplog.text and len(requests) == 1


async def test_revoke_after_model_headers_blocks_later_stream_and_success(app):
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        async with _http_clients(app) as (owner, editor):
            project, chapter, editor_user = await _world(owner, editor, url)
            pending = asyncio.create_task(editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            ))
            try:
                await asyncio.wait_for(received.wait(), 15)
                headers.set()
                revoked = await owner.delete(
                    f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                    params={"expected_revision": 2},
                )
                assert revoked.status_code == 204, revoked.text
                body.set()
                response = await asyncio.wait_for(pending, 15)
                events = parse_sse(response.text)
                assert response.status_code == 200 and events[-1][0] == "error", response.text
                assert not [event for event, _ in events if event in {"delta", "done"}]
                assert len(requests) == 1
                async with app.state.sessionmaker() as db:
                    success = await db.scalar(select(GenerationLog.id).where(
                        GenerationLog.actor_user_id == editor_user["id"], GenerationLog.status == "success",
                    ))
                    assert success is None
            finally:
                headers.set()
                body.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_editor_section_single_confirmation_and_apply(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, _editor_user = await _world(owner, editor, url)
            path, _document_url, request = await _section(owner, project, chapter)
            preview = await editor.post(path, json=request)
            assert preview.status_code == 200, preview.text
            events = parse_sse(preview.text)
            assert events[-1][0] == "done", preview.text
            assert len(requests) == 1
            prompt = "\n".join(item["content"] for item in json.loads(requests[0])["messages"])
            assert "选中" in prompt and all(text not in prompt for text in ("隐藏标题", "前缀", "后缀"))
            duplicate = await editor.post(path, json=request)
            assert duplicate.status_code == 409 and len(requests) == 1
            done = events[-1][1]
            response = await editor.post(f"{path}/apply", json={
                "expected_content_revision": request["expected_content_revision"],
                "expected_planning_revision": request["expected_planning_revision"],
                "request_id": request["request_id"], "log_id": done["log_id"],
                "start": 2, "end": 4, "original": "选中", "replacement": done["text"],
            })
            assert response.status_code == 200, response.text
            assert response.json()["document"]["sections"][0]["content"] == "前缀ok后缀"


async def test_reinvited_editor_cannot_apply_old_section_receipt(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, editor_user = await _world(owner, editor, url)
            path, document_url, request = await _section(owner, project, chapter)
            preview = await editor.post(path, json=request)
            assert preview.status_code == 200 and parse_sse(preview.text)[-1][0] == "done", preview.text
            done = parse_sse(preview.text)[-1][1]
            base = f"/api/v1/projects/{project['id']}"
            assert (await owner.delete(
                f"{base}/collaborators/{editor_user['id']}", params={"expected_revision": 2},
            )).status_code == 204
            invite = await owner.post(f"{base}/collaborators", json={"username": "rewrite-editor", "role": "editor"})
            assert invite.status_code == 201, invite.text
            assert (await editor.post(
                f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
                json={"expected_revision": invite.json()["revision"]},
            )).status_code == 200
            applied = await editor.post(f"{path}/apply", json={
                "expected_content_revision": request["expected_content_revision"],
                "expected_planning_revision": request["expected_planning_revision"],
                "request_id": request["request_id"], "log_id": done["log_id"],
                "start": 2, "end": 4, "original": "选中", "replacement": "非法覆盖",
            })
            assert applied.status_code in (403, 409), applied.text
            saved = (await owner.get(document_url)).json()["document"]
            assert saved["sections"][0]["content"] == "前缀选中后缀"
            assert len(requests) == 1


async def test_section_redirect_does_not_resend_editor_selection(app):
    async with _server(status="307 Temporary Redirect", headers={"Location": "/next"}) as (
        url, _received, headers, body, requests,
    ):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, _editor_user = await _world(owner, editor, url)
            path, _document_url, request = await _section(owner, project, chapter)
            preview = await editor.post(path, json=request)
            assert preview.status_code == 200 and parse_sse(preview.text)[-1][0] == "error", preview.text
            assert len(requests) == 1
            assert (await editor.post(path, json=request)).status_code == 409
            assert len(requests) == 1


async def test_reinvited_editor_cannot_apply_old_preview(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, editor_user = await _world(owner, editor, url)
            preview = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            assert preview.status_code == 200 and parse_sse(preview.text)[-1][0] == "done", preview.text
            done = parse_sse(preview.text)[-1][1]
            base = f"/api/v1/projects/{project['id']}"
            assert (await owner.delete(
                f"{base}/collaborators/{editor_user['id']}", params={"expected_revision": 2},
            )).status_code == 204
            invite = await owner.post(f"{base}/collaborators", json={"username": "rewrite-editor", "role": "editor"})
            assert invite.status_code == 201, invite.text
            assert (await editor.post(
                f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
                json={"expected_revision": invite.json()["revision"]},
            )).status_code == 200
            applied = await editor.post(f"{base}/chapters/{chapter['id']}/rewrite/apply", json={
                "start": 0, "end": 2, "original": "旧正", "replacement": "非法覆盖", "log_id": done["log_id"],
            })
            assert applied.status_code in (403, 409), applied.text
            assert (await owner.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == "旧正文示例"
            assert len(requests) == 1


@pytest.mark.parametrize(
    "invalid_log", ["other_ai_kind", "different_rewrite_kind", "missing_scene_snapshot", "missing_context_snapshot"],
)
async def test_editor_apply_requires_matching_rewrite_log_and_full_snapshot(app, invalid_log):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, _requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, _editor_user = await _world(owner, editor, url)
            preview = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            assert preview.status_code == 200 and parse_sse(preview.text)[-1][0] == "done", preview.text
            log_id = parse_sse(preview.text)[-1][1]["log_id"]
            async with app.state.sessionmaker() as db:
                log = await db.get(GenerationLog, log_id)
                if invalid_log == "other_ai_kind":
                    log.kind = "generate"
                elif invalid_log == "different_rewrite_kind":
                    log.kind = "expand"
                else:
                    params = dict(log.params)
                    params.pop("scene_id" if invalid_log == "missing_scene_snapshot" else "context_snapshot")
                    log.params = params
                await db.commit()
            base = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
            applied = await editor.post(f"{base}/rewrite/apply", json={
                "start": 0, "end": 2, "original": "旧正", "replacement": "非法覆盖", "log_id": log_id,
            })
            assert applied.status_code == 409, applied.text
            assert (await owner.get(base)).json()["content"] == "旧正文示例"


async def test_owner_fallback_model_details_are_private_in_editor_log(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, editor_user = await _world(owner, editor, url, source="owner")
            preview = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            assert preview.status_code == 200 and parse_sse(preview.text)[-1][0] == "done", preview.text
            assert len(requests) == 1 and json.loads(requests[0])["model"] == "owner-private-model"
            log_id = parse_sse(preview.text)[-1][1]["log_id"]
            path = f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}/generation-logs/{log_id}"
            response = await editor.get(path)
            assert response.status_code == 200, response.text
            assert response.json()["ai_config_id"] is None
            assert response.json()["provider"] == response.json()["model"] == ""
            assert "_model_source" not in response.text and "owner-private-model" not in response.text
            async with app.state.sessionmaker() as db:
                record = await db.get(GenerationLog, log_id)
                assert record.actor_user_id == editor_user["id"]
                assert record.model == "owner-private-model" and record.ai_config_id is not None


@pytest.mark.parametrize("admin", [False, True])
async def test_viewer_and_admin_viewer_cannot_rewrite(app, admin):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            project, chapter, editor_user = await _world(owner, editor, url)
            base = f"/api/v1/projects/{project['id']}"
            changed = await owner.patch(f"{base}/collaborators/{editor_user['id']}", json={
                "expected_revision": 2, "role": "viewer",
            })
            assert changed.status_code == 200, changed.text
            if admin:
                async with app.state.sessionmaker() as db:
                    (await db.get(User, editor_user["id"])).is_admin = True
                    await db.commit()
            denied = await editor.post(
                f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
            )
            assert denied.status_code in (403, 404), denied.text
            assert not requests


async def test_explicit_author_config_id_is_not_editor_model(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            _project, chapter, _editor_user = await _world(owner, editor, url)
            owner_config = await create_ai_config(owner, base_url=f"{url}/v1/", model="author-only")
            denied = await editor.post(f"/api/v1/chapters/{chapter['id']}/rewrite", json={
                "op": "rewrite", "start": 0, "end": 2, "ai_config_id": owner_config["id"],
            })
            assert denied.status_code == 404, denied.text
            assert not requests
