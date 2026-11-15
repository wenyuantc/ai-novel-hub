"""仅测试内部发送守卫；不接入协作者路由或后台执行器。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from traceback import format_exception
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import DBAPIError

import app.ai.providers as providers
from app.ai.providers import create_provider
from app.ai.providers.base import AIProviderError, LLMProvider, RequestPolicyError
from app.ai.providers.dispatch_hooks import DispatchHooks
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AIConfig, Chapter, Project, ProjectCollaborator, Task, User
from app.services import (
    ai_config_service,
    ai_dispatch,
    chapter_claim,
    project_access,
    project_ai_config,
    scene_ai_context,
)
from app.services import collaboration_ai_guard as guard
from app.services.ai_dispatch import VisibilityChangedError, VisibilityPolicyError, read_visibility_stamp
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import bind_task_grant
from app.services.task_engine import execution_identity
from tests.helpers import Gate, register
from tests.test_ai_visibility_dispatch import _collect, _server, _sse


@pytest.fixture
async def world(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        editor_out = await register(client, "guard-editor")
        assert (await client.get("/api/v1/auth/me")).status_code == 200
        actor = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
        assert actor is not None and actor[0] == editor_out["id"]
    async with app.state.sessionmaker() as db:
        owner = User(username="guard-owner", password_hash="test")
        editor = await db.get(User, actor[0])
        db.add(owner)
        await db.flush()
        project = Project(user_id=owner.id, title="共享作品")
        db.add(project)
        await db.flush()
        member = ProjectCollaborator(project_id=project.id, user_id=editor.id, status="accepted", role="editor",
                                     allow_owner_ai=True)
        config = AIConfig(user_id=owner.id, name="secret config", provider="openai_compat", model="secret model",
                          model_type="text", is_default=True, api_key_enc=app.state.cipher.encrypt("secret key"))
        db.add_all([member, config])
        await db.commit()
        binding = await project_ai_config.resolve_project_model(
            db, editor.id, project.id, "text", cipher=app.state.cipher,
        )
        stamp = await read_visibility_stamp(db, project.id)
        return {
            "owner": owner.id, "actor": editor.id, "project": project.id, "config": config.id,
            "token": actor[1], "binding": binding, "stamp": stamp,
        }


async def _http_guard(app, world, **kwargs):
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(world["actor"], world["token"])
        return guard.for_http(db, world["actor"], world["binding"], world["stamp"], cipher=app.state.cipher, **kwargs)


async def _fake_bound(app, fake_llm, checks):
    return await checks.prepare_provider(app.state.sessionmaker, fake_llm.factory, request_timeout=1)


async def _fake_call(bound):
    return [part async for part in bound.provider.generate_stream(
        [{"role": "user", "content": "private text"}], bound.model,
    )]


@pytest.mark.parametrize("change", ["revoke", "downgrade", "reinvite", "grant_off", "disable", "token", "config"])
async def test_http_guard_revalidates_identity_model_and_owner_grant_before_call(app, fake_llm, world, change):
    checks = await _http_guard(app, world)
    bound = await _fake_bound(app, fake_llm, checks)
    assert await _fake_call(bound) == ["ok"]
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == world["actor"],
        ))
        if change == "revoke":
            member.status = "revoked"
        elif change == "downgrade":
            member.role = "viewer"
        elif change == "reinvite":
            member.invitation_id, member.revision = str(uuid4()), member.revision + 1
        elif change == "grant_off":
            member.allow_owner_ai = False
        elif change == "disable":
            (await db.get(User, world["actor"])).is_active = False
        elif change == "token":
            (await db.get(User, world["actor"])).token_version += 1
        else:
            (await db.get(AIConfig, world["config"])).model = "changed secret model"
        await db.commit()
    with pytest.raises(guard.CollaborationSendBlocked) as caught:
        await _fake_call(bound)
    assert isinstance(caught.value, RequestPolicyError)
    assert caught.value.extra == {"code": "permission_denied"}
    assert "secret" not in str(caught.value) and "private text" not in str(caught.value)
    assert len(fake_llm.calls) == 1


async def _task(app, world):
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(world["actor"], world["token"])
        await project_access.require_project(db, world["actor"], world["project"], "ai.use", for_update=True,
                                             visibility_write=True, grant_identity=world["binding"].grant_identity)
        payload = bind_task_grant({}, world["binding"].grant_identity)
        payload["_project_model"] = project_ai_config.serialize_project_model(world["binding"])
        payload["_task_actor_token_version"] = world["token"]
        task = Task(user_id=world["actor"], project_id=world["project"], kind="outline.continue", status="queued",
                    statistics_key=str(uuid4()), queued_at=datetime.now(UTC), input=payload)
        db.add(task)
        await db.flush()
        queued = await db.scalar(select(Task).where(Task.id == task.id).with_for_update())
        execution_key = str(uuid4())
        identity = execution_identity.from_queued_task(queued, execution_key)
        changed = await db.execute(update(Task).where(
            Task.id == identity.task_id, Task.statistics_key == identity.task_key, Task.status == "queued",
            Task.execution_key.is_(None), Task.queued_at == identity.queued_at,
        ).values(status="running", execution_key=execution_key))
        assert changed.rowcount == 1
        await db.commit()
        return identity


async def _task_with_chapter(app, world):
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(world["actor"], world["token"])
        await project_access.require_project(db, world["actor"], world["project"], "ai.use", for_update=True,
                                             visibility_write=True, grant_identity=world["binding"].grant_identity)
        chapter = Chapter(project_id=world["project"], title="guard chapter", content="original", status="draft")
        db.add(chapter)
        await db.flush()
        payload = bind_task_grant({"request": {"project_id": world["project"]}, "chapter_ids": [chapter.id]},
                                  world["binding"].grant_identity)
        payload["_project_model"] = project_ai_config.serialize_project_model(world["binding"])
        payload["_task_actor_token_version"] = world["token"]
        task = Task(user_id=world["actor"], project_id=world["project"], kind="chapter.batch", status="queued",
                    resource_key=f"batch:{world['project']}", statistics_key=str(uuid4()),
                    queued_at=datetime.now(UTC), input=payload,
                    checkpoint={"phase": "generate", "current": None, "items": [{"chapter_id": chapter.id}]})
        db.add(task)
        await db.flush()
        queued = await db.scalar(select(Task).where(Task.id == task.id).with_for_update())
        execution_key = str(uuid4())
        identity = execution_identity.from_queued_task(queued, execution_key)
        changed = await db.execute(update(Task).where(
            Task.id == identity.task_id, Task.statistics_key == identity.task_key, Task.status == "queued",
            Task.execution_key.is_(None), Task.queued_at == identity.queued_at,
        ).values(status="running", execution_key=execution_key))
        assert changed.rowcount == 1
        attempt_key = str(uuid4())
        task.checkpoint = {**task.checkpoint, "_batch_attempt": attempt_key}
        await db.flush()
        claim = await chapter_claim.claim_batch_chapter(
            db, task_id=task.id, task_key=task.statistics_key, actor_id=world["actor"], project_id=world["project"],
            chapter_id=chapter.id, queued_at=task.queued_at, grant_identity=world["binding"].grant_identity,
            execution_key=execution_key,
        )
        attempt = guard.BatchAttemptIdentity(claim, 0, attempt_key)
        await db.commit()
        return identity, claim, attempt


@pytest.mark.parametrize("change", ["task_id", "round", "execution", "status", "admin_cancel", "grant", "model"])
async def test_task_guard_checks_original_server_identity_and_binding_each_send(app, fake_llm, world, change):
    task = await _task(app, world)
    checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher)
    bound = await _fake_bound(app, fake_llm, checks)
    assert await _fake_call(bound) == ["ok"]
    async with app.state.sessionmaker() as db:
        if change == "task_id":
            await db.execute(update(Task).where(Task.id == task.task_id).values(statistics_key=str(uuid4())))
        elif change == "round":
            await db.execute(update(Task).where(Task.id == task.task_id).values(
                queued_at=datetime(2020, 1, 1, tzinfo=UTC),
            ))
        elif change == "execution":
            await db.execute(update(Task).where(Task.id == task.task_id).values(execution_key=str(uuid4())))
        elif change == "status":
            await db.execute(update(Task).where(Task.id == task.task_id).values(status="cancelled"))
        elif change == "admin_cancel":
            await db.execute(update(Task).where(Task.id == task.task_id).values(admin_cancel_operation_id=str(uuid4())))
        else:
            current = await db.get(Task, task.task_id)
            payload = dict(current.input)
            payload["_collaboration_grant" if change == "grant" else "_project_model"] = {"private": "secret"}
            current.input = payload
        await db.commit()
    with pytest.raises(guard.CollaborationSendBlocked):
        await _fake_call(bound)
    assert len(fake_llm.calls) == 1


async def test_missing_execution_or_untrusted_request_actor_is_rejected(app, world):
    task = await _task(app, world)
    with pytest.raises(guard.CollaborationSendBlocked):
        guard.for_task(replace(task, execution_key=None), world["binding"], world["stamp"], cipher=app.state.cipher)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(world["actor"], world["token"])
        with pytest.raises(guard.CollaborationSendBlocked):
            guard.for_http(db, world["owner"], world["binding"], world["stamp"], cipher=app.state.cipher)
        db.info.pop("authenticated_actor")
        with pytest.raises(guard.CollaborationSendBlocked):
            guard.for_http(db, world["actor"], world["binding"], world["stamp"], cipher=app.state.cipher)


async def test_one_shot_callback_runs_only_after_binding_revalidation(app, world, fake_llm):
    calls = []

    async def one_shot():
        calls.append("accepted")

    checks = await _http_guard(app, world, after_verify=one_shot)
    bound = await _fake_bound(app, fake_llm, checks)
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"])
                         .values(status="revoked"))
        await db.commit()
    with pytest.raises(guard.CollaborationSendBlocked):
        await _fake_call(bound)
    assert not calls and not fake_llm.calls


async def test_one_shot_cannot_be_replayed_or_echo_callback_detail(app, world, fake_llm):
    used = False

    async def once():
        nonlocal used
        if used:
            raise VisibilityPolicyError("secret callback detail", extra={"code": "section_confirmation_used"})
        used = True

    bound = await _fake_bound(app, fake_llm, await _http_guard(app, world, after_verify=once))
    assert await _fake_call(bound) == ["ok"]
    with pytest.raises(VisibilityPolicyError) as caught:
        await _fake_call(bound)
    assert caught.value.extra == {"code": "section_confirmation_used"}
    assert "secret" not in str(caught.value) and len(fake_llm.calls) == 1


async def test_repeated_preflight_does_not_consume_one_shot_and_still_checks_revoke(app, world, fake_llm):
    consumed = []

    async def once():
        consumed.append(True)

    checks = await _http_guard(app, world, after_verify=once)
    bound = await _fake_bound(app, fake_llm, checks)
    await bound.provider.preflight()
    await bound.provider.preflight()
    assert consumed == [] and not fake_llm.calls
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"])
                         .values(status="revoked"))
        await db.commit()
    with pytest.raises(VisibilityPolicyError):
        await bound.provider.preflight()
    assert consumed == [] and not fake_llm.calls


@pytest.mark.parametrize("misuse", ["no_lease", "send_callback"])
async def test_verify_without_one_shot_requires_preflight_lease(app, world, misuse):
    consumed = []

    async def once():
        consumed.append(True)

    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        checks = await _http_guard(app, world, after_verify=once, allow_private=True)
        if misuse == "no_lease":
            async with app.state.sessionmaker() as db:
                await checks.authorize(db)
                with pytest.raises(VisibilityPolicyError):
                    await checks.verify_without_one_shot(db)
        else:
            bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
            # 即使内部调用方错误地把预检验证挂在真正发送上，也不能绕过一次性动作。
            bound.provider._verify = checks.verify_without_one_shot
            with pytest.raises(VisibilityPolicyError):
                await _fake_call(bound)
        assert consumed == [] and requests == []


@pytest.mark.parametrize("identity", ["actor", "project"])
async def test_preflight_lease_cannot_be_reused_for_another_guard_identity(app, world, fake_llm, identity):
    consumed = []

    async def once():
        consumed.append(True)

    checks = await _http_guard(app, world, after_verify=once)
    bound = await _fake_bound(app, fake_llm, checks)
    altered = replace(checks._binding, **{
        "actor_id" if identity == "actor" else "project_id":
            world["actor"] + 100 if identity == "actor" else world["project"] + 100,
    })
    wrong_guard = replace(checks, _binding=altered)
    bound.provider._verify_preflight = wrong_guard.verify_without_one_shot
    with pytest.raises(VisibilityPolicyError):
        await bound.provider.preflight()
    assert consumed == [] and not fake_llm.calls


async def test_preflight_lease_expires_after_return(app, world, fake_llm, monkeypatch):
    captured = {}
    original = ai_dispatch.require_preflight_lease

    async def observe(db, actor_id, project_id):
        await original(db, actor_id, project_id)
        captured["db"] = db

    checks = await _http_guard(app, world)
    bound = await _fake_bound(app, fake_llm, checks)
    monkeypatch.setattr(ai_dispatch, "require_preflight_lease", observe)
    await bound.provider.preflight()
    with pytest.raises(VisibilityPolicyError):
        await checks.verify_without_one_shot(captured["db"])
    assert not fake_llm.calls


@pytest.mark.parametrize("misuse", ["no_lease", "preflight_callback", "direct_send_lease"])
async def test_one_shot_verify_requires_real_send_lease(app, world, fake_llm, misuse):
    consumed = []

    async def once():
        consumed.append(True)

    checks = await _http_guard(app, world, after_verify=once)
    if misuse == "no_lease":
        async with app.state.sessionmaker() as db:
            await checks.authorize(db)
            with pytest.raises(VisibilityPolicyError):
                await checks.verify(db)
    elif misuse == "preflight_callback":
        bound = await _fake_bound(app, fake_llm, checks)
        bound.provider._verify_preflight = checks.verify
        with pytest.raises(VisibilityPolicyError):
            await bound.provider.preflight()
    else:
        bound = await _fake_bound(app, fake_llm, checks)
        with pytest.raises(VisibilityPolicyError):
            async with bound.provider._send_lease():
                pass
    assert consumed == [] and not fake_llm.calls


@pytest.mark.parametrize("claimed_hooks", [False, True])
@pytest.mark.parametrize("factory_source", ["custom", "registry"])
async def test_unhooked_network_provider_cannot_send(app, world, claimed_hooks, factory_source, monkeypatch):
    async with _server(payload=b"ok") as (url, _received, headers, body, requests):
        headers.set()
        body.set()

        class UnhookedNetworkProvider(LLMProvider):
            uses_dispatch_hooks = claimed_hooks

            async def generate_stream(self, messages, model, **kwargs):
                async with httpx.AsyncClient() as client:
                    await client.post(url, content=b"private text")
                yield "unprotected"

        called = []

        def factory(_provider, api_key, base_url, timeout):
            called.append(True)
            return UnhookedNetworkProvider(api_key, base_url, timeout)

        if factory_source == "registry":
            monkeypatch.setitem(providers._PROVIDERS, "openai_compat", UnhookedNetworkProvider)
            factory = create_provider
        checks = await _http_guard(app, world)
        with pytest.raises(guard.CollaborationSendBlocked):
            bound = await checks.prepare_provider(app.state.sessionmaker, factory, request_timeout=2)
            await _fake_call(bound)
        assert requests == []
        assert called == []  # 自定义工厂甚至不能在构造阶段执行任意代码。


async def test_task_guard_rejects_execution_uuid_reused_as_task_uuid(app, world):
    task = await _task(app, world)
    with pytest.raises(guard.CollaborationSendBlocked):
        guard.for_task(replace(task, execution_key=task.task_key), world["binding"], world["stamp"],
                       cipher=app.state.cipher)


@pytest.mark.parametrize("field", [
    "task_id", "bool_task_id", "task_key", "actor_id", "project_id", "queued_at", "execution_key", "grant", "phase",
])
async def test_chapter_guard_rejects_identity_from_another_task_or_grant(app, world, field):
    task, claim, attempt = await _task_with_chapter(app, world)
    changes = {
        "task_id": {"task_id": claim.task_id + 1},
        "bool_task_id": {"task_id": True},
        "task_key": {"task_key": str(uuid4())},
        "actor_id": {"actor_id": claim.actor_id + 1},
        "project_id": {"project_id": claim.project_id + 1},
        "queued_at": {"queued_at": claim.queued_at + timedelta(seconds=1)},
        "execution_key": {"execution_key": str(uuid4())},
        "grant": {"grant_identity": (str(uuid4()), 1)},
        "phase": {"phase": "queued"},
    }
    with pytest.raises(guard.CollaborationSendBlocked):
        forged = replace(claim, **changes[field])
        guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                       chapter=forged, batch_attempt=replace(attempt, claim=forged))


async def test_chapter_guard_rejects_another_scene_chapter(app, world):
    task, claim, attempt = await _task_with_chapter(app, world)
    scene = scene_ai_context.SceneContextStamp(world["project"], claim.chapter_id + 1, 0, 0, "test", world["stamp"])
    with pytest.raises(guard.CollaborationSendBlocked):
        guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                       chapter=claim, batch_attempt=attempt, scene=scene)


async def test_chapter_guard_verify_requires_real_session_lease(app, world):
    task, claim, attempt = await _task_with_chapter(app, world)
    checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                            chapter=claim, batch_attempt=attempt)
    async with app.state.sessionmaker() as db:
        await checks.authorize(db)
        with pytest.raises(VisibilityPolicyError):
            await checks.verify(db)


async def test_unhooked_fake_chapter_cannot_claim_a_send_lease(app, fake_llm, world, monkeypatch):
    task, claim, attempt = await _task_with_chapter(app, world)
    checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                            chapter=claim, batch_attempt=attempt)
    called = []

    def unhooked_factory(provider, api_key, base_url, timeout):
        called.append(True)
        fake = fake_llm.factory(provider, api_key, base_url, timeout)
        fake.uses_dispatch_hooks = False
        return fake

    # 即使误放开外层工厂检查，章节 transport 层也必须先拒绝未知工厂。
    monkeypatch.setattr(guard, "_trusted_factory", lambda _factory: True)
    monkeypatch.setattr(guard, "_trusted_provider", lambda *_args: True)
    with pytest.raises(VisibilityPolicyError):
        await checks.prepare_provider(app.state.sessionmaker, unhooked_factory, request_timeout=1)
    assert not called and not fake_llm.calls


async def test_registered_memory_fake_releases_precheck_before_output(app, fake_llm, world):
    task, claim, attempt = await _task_with_chapter(app, world)
    checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                            chapter=claim, batch_attempt=attempt)
    bound = await _fake_bound(app, fake_llm, checks)
    gate = Gate()
    fake_llm.push([gate, "ok"])
    sending = asyncio.create_task(_fake_call(bound))
    try:
        await gate.wait_reached()
        async with app.state.sessionmaker() as db:
            assert await chapter_claim.reset_generating_chapter(
                db, world["owner"], world["project"], claim.chapter_id, "draft",
            )
            await db.commit()
        assert not sending.done()
        gate.release()
        assert await sending == ["ok"]
        with pytest.raises(VisibilityPolicyError):
            await _fake_call(bound)
    finally:
        gate.release()
        if not sending.done():
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.parametrize("operation", ["commit", "rollback", "close"])
async def test_callback_cannot_end_transaction_before_network_send(app, fake_llm, world, operation, monkeypatch):
    captured = {}
    original = ai_dispatch.authorize_send_actor

    async def observe(db, actor):
        await original(db, actor)
        captured["db"] = db

    async def bad():
        await getattr(captured["db"], operation)()

    checks = await _http_guard(app, world, after_verify=bad)
    bound = await _fake_bound(app, fake_llm, checks)
    # 假设内部 callback 错误地从闭包取得了当前 lease session。
    monkeypatch.setattr(ai_dispatch, "authorize_send_actor", observe)
    with pytest.raises(guard.CollaborationSendBlocked):
        await _fake_call(bound)
    assert not fake_llm.calls


@pytest.mark.parametrize("change", ["token", "grant", "config"])
async def test_callback_cannot_change_credential_or_binding_in_same_transaction(
    app, fake_llm, world, change, monkeypatch,
):
    captured = {}
    original = ai_dispatch.authorize_send_actor

    async def observe(db, actor):
        await original(db, actor)
        captured["db"] = db

    async def bad():
        db = captured["db"]
        if change == "token":
            await db.execute(update(User).where(User.id == world["actor"]).values(token_version=User.token_version + 1))
        elif change == "grant":
            await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"])
                             .values(status="revoked"))
        else:
            await db.execute(update(AIConfig).where(AIConfig.id == world["config"]).values(model="other"))

    checks = await _http_guard(app, world, after_verify=bad)
    bound = await _fake_bound(app, fake_llm, checks)
    monkeypatch.setattr(ai_dispatch, "authorize_send_actor", observe)
    with pytest.raises(guard.CollaborationSendBlocked):
        await _fake_call(bound)
    assert not fake_llm.calls
    async with app.state.sessionmaker() as db:
        assert (await db.get(User, world["actor"])).token_version == world["token"]
        assert (await db.get(AIConfig, world["config"])).model == "secret model"
        member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"]))
        assert member.status == "accepted"


async def test_callback_cannot_hide_unflushed_revocation(app, fake_llm, world, monkeypatch):
    captured = {}
    original = ai_dispatch.authorize_send_actor

    async def observe(db, actor):
        await original(db, actor)
        captured["db"] = db

    async def bad():
        db = captured["db"]
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == world["actor"],
        ))
        member.status = "revoked"

    checks = await _http_guard(app, world, after_verify=bad)
    bound = await _fake_bound(app, fake_llm, checks)
    monkeypatch.setattr(ai_dispatch, "authorize_send_actor", observe)
    with pytest.raises(guard.CollaborationSendBlocked):
        await _fake_call(bound)
    assert not fake_llm.calls
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"]))
        assert member.status == "accepted"


async def test_pending_objects_are_not_flushed_or_silently_overwritten(app, world):
    checks = await _http_guard(app, world)
    async with app.state.sessionmaker() as db:
        await checks.authorize(db)
        pending = User(username="never-flushed", password_hash="test")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(guard.CollaborationSendBlocked):
            await checks.verify(db)
        assert pending.id is None and not flushes
        await db.rollback()


async def test_callback_cancel_releases_session_without_sending(app, fake_llm, world):
    async def cancelled():
        raise asyncio.CancelledError

    checks = await _http_guard(app, world, after_verify=cancelled)
    bound = await _fake_bound(app, fake_llm, checks)
    with pytest.raises(asyncio.CancelledError):
        await _fake_call(bound)
    assert not fake_llm.calls
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == world["actor"]).values(token_version=User.token_version + 1))
        await db.commit()


async def test_preflight_rechecks_visibility(app, world, fake_llm):
    checks = await _http_guard(app, world)
    bound = await _fake_bound(app, fake_llm, checks)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Project).where(Project.id == world["project"])
                         .values(ai_visibility_revision=Project.ai_visibility_revision + 1))
        await db.commit()
    with pytest.raises(VisibilityChangedError):
        await bound.provider.preflight()


async def test_db_failure_is_sanitized_and_never_sent(app, world, fake_llm, monkeypatch):
    bound = await _fake_bound(app, fake_llm, await _http_guard(app, world))

    async def failed(*_args, **_kwargs):
        raise RuntimeError("private text secret key SQL parameter")

    monkeypatch.setattr(project_ai_config, "revalidate_project_model", failed)
    with pytest.raises(guard.CollaborationSendBlocked) as caught:
        await _fake_call(bound)
    assert "secret" not in str(caught.value) and caught.value.__context__ is None
    assert not fake_llm.calls


async def test_callback_exception_is_sanitized_without_network(app, world, fake_llm):
    async def bad():
        raise RuntimeError("private text secret key SQL parameter")

    checks = await _http_guard(app, world, after_verify=bad)
    bound = await _fake_bound(app, fake_llm, checks)
    with pytest.raises(guard.CollaborationSendBlocked) as caught:
        await _fake_call(bound)
    assert "secret" not in str(caught.value) and caught.value.__context__ is None
    assert not fake_llm.calls


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_real_sdk_hook_rejects_revoked_member_without_network_request(app, world, kind):
    async with _server(payload=_sse(kind)) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.provider = {"openai": "openai_compat", "anthropic": "anthropic", "gemini": "gemini"}[kind]
            cfg.base_url = f"{url}/v1/" if kind == "openai" else url
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        checks = await _http_guard(app, world, allow_private=True)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"])
                             .values(status="revoked"))
            await db.commit()
        with pytest.raises(guard.CollaborationSendBlocked):
            await _collect(bound.provider.generate_stream([{"role": "user", "content": "do not send"}], bound.model))
        assert requests == []


@pytest.mark.parametrize("mode", ["text", "tools", "image", "embedding"])
@pytest.mark.parametrize("fault", ["database", "unexpected"])
async def test_real_sdk_lock_database_error_is_policy_failure_without_sql(app, world, monkeypatch, mode, fault):
    secret = "private SQL and bound parameter"
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            cfg.model_type = "text" if mode == "tools" else mode
            await db.commit()
            if mode == "embedding":
                world["binding"] = await project_ai_config.resolve_shared_embedding(
                    db, world["actor"], world["project"], cipher=app.state.cipher, allow_private=True,
                )
            else:
                world["binding"] = await project_ai_config.resolve_project_model(
                    db, world["actor"], world["project"], cfg.model_type, cipher=app.state.cipher, allow_private=True,
                )
        bound = await (await _http_guard(app, world, allow_private=True)).prepare_provider(
            app.state.sessionmaker, create_provider, request_timeout=2,
        )

        async def failing_lock(*_args, **_kwargs):
            if fault == "database":
                raise DBAPIError(f"SELECT {secret}", {"body": secret}, RuntimeError(secret))
            raise RuntimeError(secret)

        monkeypatch.setattr(ai_dispatch, "_lock", failing_lock)
        with pytest.raises(VisibilityPolicyError) as caught:
            messages = [{"role": "user", "content": "do not send"}]
            if mode == "text":
                await _collect(bound.provider.generate_stream(messages, bound.model))
            elif mode == "tools":
                await _collect(bound.provider.chat_tools_stream(messages, bound.model, []))
            elif mode == "image":
                await bound.provider.generate_image("do not send", bound.model)
            else:
                await bound.provider.embed(["do not send"], bound.model)
        assert secret not in "".join(format_exception(caught.value))
        assert caught.value.__context__ is None and requests == []


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_builtin_provider_sends_through_real_sdk(app, world, kind):
    async with _server(payload=_sse(kind)) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.provider = {"openai": "openai_compat", "anthropic": "anthropic", "gemini": "gemini"}[kind]
            cfg.base_url = f"{url}/v1/" if kind == "openai" else url
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        bound = await (await _http_guard(app, world, allow_private=True)).prepare_provider(
            app.state.sessionmaker, create_provider, request_timeout=2,
        )
        assert await _fake_call(bound) == ["ok"]
        assert len(requests) == 1


@pytest.mark.parametrize("alter", ["valid_then_reset", "wrong_claim", "wrong_chapter"])
async def test_real_sdk_chapter_claim_is_checked_inside_send_lease(app, world, alter):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        task, original, attempt = await _task_with_chapter(app, world)
        if alter == "wrong_claim":
            selected = replace(original, claim_key=str(uuid4()))
        elif alter == "wrong_chapter":
            selected = replace(original, chapter_id=original.chapter_id + 1)
        else:
            selected = original
        if alter != "valid_then_reset":
            # 伪造的 batch 指针须在准备 provider 时失败关闭，不能等到发包。
            forged = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                                    chapter=selected, batch_attempt=replace(attempt, claim=selected),
                                    allow_private=True)
            with pytest.raises(guard.CollaborationSendBlocked) as error:
                await forged.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
            assert error.value.extra == {"code": "permission_denied"}
            assert requests == []

        # 保留原领取与章节身份，先通过预检，再模拟章节占用在发送前变化。
        checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                                chapter=original, batch_attempt=attempt, allow_private=True)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        if alter == "valid_then_reset":
            assert await _fake_call(bound) == ["ok"]
            async with app.state.sessionmaker() as db:
                assert await chapter_claim.reset_generating_chapter(
                    db, world["owner"], world["project"], original.chapter_id, "draft",
                )
                await db.commit()
        else:
            async with app.state.sessionmaker() as db:
                chapter = await db.get(Chapter, original.chapter_id)
                if alter == "wrong_claim":
                    chapter.generation_claim_key = str(uuid4())
                else:
                    other = Project(user_id=world["owner"], title="另一个作品")
                    db.add(other)
                    await db.flush()
                    chapter.project_id = other.id
                await db.commit()
        with pytest.raises(VisibilityPolicyError) as error:
            await _fake_call(bound)
        assert type(error.value) is VisibilityPolicyError
        assert error.value.extra == {"code": "chapter_claim_changed"}
        assert error.value.detail == "章节生成状态已变化，已停止发送"
        assert error.value.__context__ is None
        assert len(requests) == (1 if alter == "valid_then_reset" else 0)


@pytest.mark.parametrize("mode", ["send", "preflight"])
async def test_chapter_failure_in_guard_verify_uses_same_safe_policy(app, world, monkeypatch, mode):
    secret = "private SQL secret credentials"
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        task, claim, attempt = await _task_with_chapter(app, world)
        checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                                chapter=claim, batch_attempt=attempt, allow_private=True)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        original = chapter_claim.require_sending_chapter
        calls = 0

        async def fail_guard_check(db, identity):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise VisibilityPolicyError(secret, extra={"code": "chapter_claim_changed", "secret": secret})
            await original(db, identity)

        monkeypatch.setattr(chapter_claim, "require_sending_chapter", fail_guard_check)
        with pytest.raises(VisibilityPolicyError) as error:
            if mode == "send":
                await _fake_call(bound)
            else:
                await bound.provider.preflight()
        assert calls == 2 and requests == []
        assert type(error.value) is VisibilityPolicyError
        assert error.value.extra == {"code": "chapter_claim_changed"}
        assert error.value.detail == "章节生成状态已变化，已停止发送"
        assert error.value.__context__ is None and secret not in "".join(format_exception(error.value))


async def test_chapter_storage_error_before_guard_verify_is_safe_and_nonretryable(app, world, monkeypatch):
    secret = "private chapter SQL and credential"
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        task, claim, attempt = await _task_with_chapter(app, world)
        checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                                chapter=claim, batch_attempt=attempt, allow_private=True)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)

        async def failed_task(*_args, **_kwargs):
            raise DBAPIError(f"SELECT {secret}", {"token": secret}, RuntimeError(secret))

        monkeypatch.setattr(chapter_claim, "_task", failed_task)
        with pytest.raises(VisibilityPolicyError) as error:
            await _fake_call(bound)
        assert type(error.value) is VisibilityPolicyError and isinstance(error.value, RequestPolicyError)
        assert error.value.extra == {"code": "chapter_claim_changed"}
        assert error.value.detail == "章节生成状态已变化，已停止发送"
        assert error.value.__context__ is None and secret not in "".join(format_exception(error.value))
        assert requests == []


async def test_other_policy_cannot_spoof_chapter_failure(app, world, fake_llm):
    secret = "private callback payload"

    async def forged():
        raise VisibilityPolicyError(secret, extra={"code": "chapter_claim_changed", "secret": secret})

    task, claim, attempt = await _task_with_chapter(app, world)
    checks = guard.for_task(task, world["binding"], world["stamp"], cipher=app.state.cipher,
                            chapter=claim, batch_attempt=attempt, after_verify=forged)
    bound = await _fake_bound(app, fake_llm, checks)
    with pytest.raises(guard.CollaborationSendBlocked) as error:
        await _fake_call(bound)
    assert error.value.extra == {"code": "permission_denied"}
    assert secret not in "".join(format_exception(error.value)) and error.value.__context__ is None
    assert not fake_llm.calls


async def test_real_sdk_redirect_rechecks_original_authorization_each_hop(app, world, monkeypatch):
    original = DispatchHooks._after_headers
    changed = False

    async def revoke_after_headers(hooks, response):
        nonlocal changed
        await original(hooks, response)
        if not changed:
            async with app.state.sessionmaker() as db:
                await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"])
                                 .values(status="revoked"))
                await db.commit()
            changed = True

    monkeypatch.setattr(DispatchHooks, "_after_headers", revoke_after_headers)
    async with _server(status="307 Temporary Redirect", payload=b"", headers={"Location": "/fallback"}) as (
        url, _received, headers, body, requests,
    ):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        checks = await _http_guard(app, world, allow_private=True)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        with pytest.raises(guard.CollaborationSendBlocked):
            await _collect(bound.provider.generate_stream([{"role": "user", "content": "first only"}], bound.model))
        assert changed and len(requests) == 1


async def test_real_sdk_unknown_response_does_not_retry_send(app, world):
    async with _server(status=None) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        bound = await (await _http_guard(app, world, allow_private=True)).prepare_provider(
            app.state.sessionmaker, create_provider, request_timeout=2,
        )
        with pytest.raises(AIProviderError):
            await _fake_call(bound)
        assert len(requests) == 1


@pytest.mark.parametrize("fault", ["database", "unexpected"])
async def test_real_sdk_lease_cleanup_failure_does_not_leak_sql(app, world, monkeypatch, fault):
    secret = "private cleanup SQL and bound parameter"
    async with _server(status=None) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url = f"{url}/v1/"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
        bound = await (await _http_guard(app, world, allow_private=True)).prepare_provider(
            app.state.sessionmaker, create_provider, request_timeout=2,
        )
        original_close = ai_dispatch._close_send_session

        async def failed_close(db):
            await original_close(db)
            if fault == "database":
                raise DBAPIError(f"SELECT {secret}", {"body": secret}, RuntimeError(secret))
            raise RuntimeError(secret)

        monkeypatch.setattr(ai_dispatch, "_close_send_session", failed_close)
        with pytest.raises(VisibilityPolicyError) as caught:
            await _fake_call(bound)
        assert secret not in "".join(format_exception(caught.value))
        assert caught.value.__context__ is None and len(requests) == 1


async def test_real_sdk_size_retry_rechecks_authorization_before_second_send(app, world, monkeypatch):
    original = DispatchHooks._after_headers
    changed = False

    async def revoke_after_headers(hooks, response):
        nonlocal changed
        await original(hooks, response)
        if not changed:
            async with app.state.sessionmaker() as db:
                await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"])
                                 .values(status="revoked"))
                await db.commit()
            changed = True

    monkeypatch.setattr(DispatchHooks, "_after_headers", revoke_after_headers)
    payload = json.dumps({"error": {"message": "unsupported size", "type": "invalid_request_error"}}).encode()
    async with _server(status="400 Bad Request", mime="application/json", payload=payload) as (
        url, _received, headers, body, requests,
    ):
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["config"])
            cfg.base_url, cfg.model_type = f"{url}/v1/", "image"
            await db.commit()
            world["binding"] = await project_ai_config.resolve_project_model(
                db, world["actor"], world["project"], "image", cipher=app.state.cipher, allow_private=True,
            )
        bound = await (await _http_guard(app, world, allow_private=True)).prepare_provider(
            app.state.sessionmaker, create_provider, request_timeout=2,
        )
        with pytest.raises(guard.CollaborationSendBlocked):
            await bound.provider.generate_image("first only", bound.model)
        assert changed and len(requests) == 1


async def test_prepared_provider_rejects_other_model_and_keeps_secrets_private(app, fake_llm, world):
    checks = await _http_guard(app, world)
    bound = await _fake_bound(app, fake_llm, checks)
    assert repr(checks) == "<SendGuard>" and repr(bound) == "<BoundProvider>"
    assert not hasattr(checks, "wrap_provider")  # 不能传入未经绑定的任意 provider。
    with pytest.raises(guard.CollaborationSendBlocked):
        await _collect(bound.provider.generate_stream([{"role": "user", "content": "do not send"}], "other model"))
    with pytest.raises(guard.CollaborationSendBlocked):
        await bound.provider.embed(["do not send"], bound.model)
    with pytest.raises(guard.CollaborationSendBlocked):
        await bound.provider.generate_image("do not send", bound.model)
    assert not fake_llm.calls
    assert not fake_llm.embed_calls and not fake_llm.image_calls


async def test_configuration_change_after_snapshot_cannot_switch_provider(app, fake_llm, world, monkeypatch):
    checks = await _http_guard(app, world)

    async def change_during_resolution(_url):
        # 构造 provider 前的 DNS 解析发生在读取事务结束后。
        async with app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == world["config"]).values(model="new model"))
            await db.commit()

    monkeypatch.setattr(ai_config_service, "ensure_endpoint_allowed", change_during_resolution)
    bound = await _fake_bound(app, fake_llm, checks)
    assert bound.model == "secret model" and bound.provider_name == "openai_compat"
    with pytest.raises(guard.CollaborationSendBlocked):
        await _fake_call(bound)
    assert not fake_llm.calls


async def test_shared_embedding_uses_only_bound_author_space_and_keyword_never_sends(app, fake_llm, world):
    async with app.state.sessionmaker() as db:
        db.add_all([
            AIConfig(user_id=world["owner"], name="author-vector", provider="openai_compat", model="author-vector",
                     model_type="embedding", is_default=True, api_key_enc=app.state.cipher.encrypt("author-key")),
            AIConfig(user_id=world["actor"], name="actor-vector", provider="openai_compat", model="actor-vector",
                     model_type="embedding", is_default=True, api_key_enc=app.state.cipher.encrypt("actor-key")),
        ])
        await db.commit()
        selection = await project_ai_config.resolve_shared_embedding(
            db, world["actor"], world["project"], cipher=app.state.cipher,
        )
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(world["actor"], world["token"])
        checks = guard.for_http(db, world["actor"], selection, world["stamp"], cipher=app.state.cipher)
    bound = await _fake_bound(app, fake_llm, checks)
    assert bound.model == "author-vector"
    await bound.provider.embed(["private"], bound.model)
    assert fake_llm.embed_calls[0]["model"] == "author-vector"
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.project_id == world["project"]))
        member.allow_owner_ai = False
        member.revision += 1
        await db.commit()
        keyword = await project_ai_config.resolve_shared_embedding(
            db, world["actor"], world["project"], cipher=app.state.cipher,
        )
    with pytest.raises(guard.CollaborationSendBlocked):
        await bound.provider.embed(["private"], bound.model)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(world["actor"], world["token"])
        with pytest.raises(guard.CollaborationSendBlocked):
            guard.for_http(db, world["actor"], keyword, world["stamp"], cipher=app.state.cipher)
    assert len(fake_llm.embed_calls) == 1
