"""真实 PostgreSQL 与本机 SDK HTTP：发送 lease、撤权和任务轮次。"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text, update

from app.ai.providers import create_provider
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import Chapter, Task
from app.services import (
    ai_dispatch,
    chapter_claim,
    collaboration_ai_guard,
    project_access,
    project_ai_config,
)
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import bind_task_grant
from app.services.task_engine import execution_identity
from tests import pg_support
from tests.helpers import create_ai_config
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_access_postgresql import _setup, _wait_for_blocker


@pytest.fixture
async def guard_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url("AINOVEL_COLLAB_GUARD_POSTGRES_TEST_URL", "collab_guards", purpose="发送守卫测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _bound(guest_app, owner, guest, url, monkeypatch, *, task=False, chapter=False, after_verify=None):
    project, editor, base = await _setup(owner, guest)
    assert (await guest.get("/api/v1/auth/me")).status_code == 200
    actor = decode_access_token(guest.cookies[ACCESS_TOKEN_COOKIE], guest_app.state.settings.jwt_secret)
    assert actor is not None and actor[0] == editor["id"]
    await create_ai_config(owner, base_url=f"{url}/v1/", api_key="local-test", model="guard-model")
    changed = await owner.patch(f"{base}/collaborators/{editor['id']}", json={
        "expected_revision": 2, "allow_owner_ai": True,
    })
    assert changed.status_code == 200, changed.text
    maker = guest_app.state.sessionmaker
    pid_future = asyncio.get_running_loop().create_future()

    async with maker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(actor[0], actor[1])
        binding = await project_ai_config.resolve_project_model(
            db, editor["id"], project["id"], "text", cipher=guest_app.state.cipher, allow_private=True,
        )
        stamp = await read_visibility_stamp(db, project["id"])
        claim = None
        if task or chapter:
            await project_access.require_project(db, actor[0], project["id"], "ai.use", for_update=True,
                                                 visibility_write=True, grant_identity=binding.grant_identity)
            payload = bind_task_grant({}, binding.grant_identity)
            payload["_project_model"] = project_ai_config.serialize_project_model(binding)
            payload["_task_actor_token_version"] = actor[1]
            target = None
            if chapter:
                target = Chapter(project_id=project["id"], title="章节", content="旧正文", status="draft")
                db.add(target)
                await db.flush()
                payload.update(request={"project_id": project["id"]}, chapter_ids=[target.id])
            item = Task(user_id=editor["id"], project_id=project["id"],
                        kind="chapter.batch" if chapter else "outline.continue", status="queued",
                        resource_key=f"batch:{project['id']}" if chapter else None,
                        statistics_key=str(uuid4()), queued_at=datetime.now(UTC), input=payload,
                        checkpoint={"phase": "generate", "current": None, "items": [{"chapter_id": target.id}]}
                        if chapter else None)
            db.add(item)
            await db.flush()
            queued = await db.scalar(select(Task).where(Task.id == item.id).with_for_update())
            execution_key = str(uuid4())
            identity = execution_identity.from_queued_task(queued, execution_key)
            changed = await db.execute(update(Task).where(
                Task.id == identity.task_id, Task.statistics_key == identity.task_key, Task.status == "queued",
                Task.execution_key.is_(None), Task.queued_at == identity.queued_at,
            ).values(status="running", execution_key=execution_key))
            assert changed.rowcount == 1
            attempt = None
            if target is not None:
                attempt_key = str(uuid4())
                item.checkpoint = {**item.checkpoint, "_batch_attempt": attempt_key}
                await db.flush()
                claim = await chapter_claim.claim_batch_chapter(
                    db, task_id=item.id, task_key=item.statistics_key, actor_id=editor["id"],
                    project_id=project["id"], chapter_id=target.id, queued_at=item.queued_at,
                    grant_identity=binding.grant_identity, execution_key=execution_key,
                )
                attempt = collaboration_ai_guard.BatchAttemptIdentity(claim, 0, attempt_key)
            await db.commit()
            task_id = item.id
            checks = collaboration_ai_guard.for_task(identity, binding, stamp, cipher=guest_app.state.cipher,
                                                       allow_private=True, chapter=claim, batch_attempt=attempt,
                                                       after_verify=after_verify)
        else:
            checks = collaboration_ai_guard.for_http(db, actor[0], binding, stamp, cipher=guest_app.state.cipher,
                                                       allow_private=True, after_verify=after_verify)
            task_id = None
    bound = await checks.prepare_provider(maker, create_provider, request_timeout=5)
    original = ai_dispatch.authorize_send_actor

    async def observe(db, actor):
        await original(db, actor)
        if not pid_future.done():
            pid_future.set_result(await db.scalar(text("SELECT pg_backend_pid()")))

    monkeypatch.setattr(ai_dispatch, "authorize_send_actor", observe)
    return base, editor["id"], task_id, pid_future, bound, claim


async def _call(bound):
    return await _collect(bound.provider.generate_stream([{"role": "user", "content": "controlled text"}], bound.model))


async def test_revoke_waits_for_real_send_headers_then_old_binding_fails(guard_apps, monkeypatch):
    owner_app, guest_app = guard_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        _server(payload=_sse("openai")) as (url, received, headers, body, requests),
    ):
        base, editor_id, _, pid_future, bound, _ = await _bound(guest_app, owner, guest, url, monkeypatch)
        sending = asyncio.create_task(_call(bound))
        revoke = None
        try:
            await asyncio.wait_for(received.wait(), 6)
            pid = await asyncio.wait_for(pid_future, 6)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{editor_id}",
                                                       params={"expected_revision": 3}))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
            assert not revoke.done()
            headers.set()
            result = await asyncio.wait_for(revoke, 6)
            assert result.status_code == 204, result.text
            assert not sending.done()
            body.set()
            assert await asyncio.wait_for(sending, 6) == ["ok"]
            with pytest.raises(collaboration_ai_guard.CollaborationSendBlocked):
                await _call(bound)
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            for pending in (sending, revoke):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_task_status_change_waits_for_send_and_cannot_reuse_execution(guard_apps, monkeypatch):
    owner_app, guest_app = guard_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        _server(payload=_sse("openai")) as (url, received, headers, body, requests),
    ):
        _, _, task_id, pid_future, bound, _ = await _bound(guest_app, owner, guest, url, monkeypatch, task=True)
        sending = asyncio.create_task(_call(bound))
        cancelled = None
        try:
            await asyncio.wait_for(received.wait(), 6)
            pid = await asyncio.wait_for(pid_future, 6)

            async def cancel():
                async with owner_app.state.sessionmaker() as db:
                    await db.execute(update(Task).where(Task.id == task_id).values(status="cancelled"))
                    await db.commit()

            cancelled = asyncio.create_task(cancel())
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
            assert not cancelled.done()
            headers.set()
            await asyncio.wait_for(cancelled, 6)
            assert not sending.done()
            body.set()
            assert await asyncio.wait_for(sending, 6) == ["ok"]
            with pytest.raises(collaboration_ai_guard.CollaborationSendBlocked):
                await _call(bound)
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            for pending in (sending, cancelled):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_chapter_reset_waits_for_real_send_headers_and_old_claim_fails(guard_apps, monkeypatch):
    owner_app, guest_app = guard_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        _server(payload=_sse("openai")) as (url, received, headers, body, requests),
    ):
        _, editor_id, _, pid_future, bound, claim = await _bound(
            guest_app, owner, guest, url, monkeypatch, chapter=True,
        )
        assert claim is not None
        sending = asyncio.create_task(_call(bound))
        reset = None
        try:
            await asyncio.wait_for(received.wait(), 6)
            pid = await asyncio.wait_for(pid_future, 6)

            async def reset_chapter():
                async with owner_app.state.sessionmaker() as db:
                    changed = await chapter_claim.reset_generating_chapter(
                        db, editor_id, claim.project_id, claim.chapter_id, "draft",
                    )
                    await db.commit()
                    return changed

            reset = asyncio.create_task(reset_chapter())
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 6)
            assert not reset.done()
            headers.set()
            assert await asyncio.wait_for(reset, 6)
            assert not sending.done()
            body.set()
            assert await asyncio.wait_for(sending, 6) == ["ok"]
            with pytest.raises(VisibilityPolicyError) as error:
                await _call(bound)
            assert type(error.value) is VisibilityPolicyError
            assert error.value.extra == {"code": "chapter_claim_changed"}
            assert error.value.detail == "章节生成状态已变化，已停止发送"
            assert error.value.__context__ is None
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            for pending in (sending, reset):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending


async def test_postgres_preflight_never_consumes_one_shot_but_send_does(guard_apps, monkeypatch):
    owner_app, guest_app = guard_apps
    consumed = []

    async def once():
        if consumed:
            raise collaboration_ai_guard.VisibilityPolicyError(
                "already used", extra={"code": "section_confirmation_used"},
            )
        consumed.append(True)

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
    ):
        headers.set()
        body.set()
        _, _, _, _, bound, _ = await _bound(guest_app, owner, guest, url, monkeypatch,
                                           chapter=True, after_verify=once)
        await bound.provider.preflight()
        await bound.provider.preflight()
        assert consumed == [] and requests == []
        assert await _call(bound) == ["ok"]
        assert consumed == [True] and len(requests) == 1
        await bound.provider.preflight()
        assert consumed == [True]
        with pytest.raises(collaboration_ai_guard.VisibilityPolicyError) as error:
            await _call(bound)
        assert error.value.extra["code"] == "section_confirmation_used" and len(requests) == 1
