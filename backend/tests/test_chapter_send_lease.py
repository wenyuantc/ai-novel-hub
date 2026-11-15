"""章节校验只能使用实际发送事务，而不是调用方声称的锁。"""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import LLMProvider, OpenAICompatProvider, create_provider
from app.ai.providers.dispatch_hooks import DispatchHooks
from app.models import Chapter
from app.services import ai_dispatch, authorization_service, chapter_claim
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp
from app.services.collaboration_ai_guard import _BoundModelProvider
from tests.test_ai_visibility_dispatch import _server
from tests.test_chapter_claim_core import _world


async def _claimed(app, client, make_client):
    identity, execution = await _world(app, client, make_client, batch=True)
    async with app.state.sessionmaker() as db:
        claim = await chapter_claim.claim_batch_chapter(db, **identity, execution_key=execution)
        stamp = await read_visibility_stamp(db, claim.project_id)
        await db.commit()
    return claim, stamp


def _guard(maker, provider, claim, stamp, verify=None, authorize=None):
    async def check(db):
        await chapter_claim.require_sending_chapter(db, claim)

    async def auth(db):
        await ai_dispatch.authorize_send_actor(db, claim.actor_id)

    if type(provider) is not _BoundModelProvider:
        provider = _BoundModelProvider(provider, "test-model", "embedding")
    return ai_dispatch.guard_chapter_provider(provider, stamp, maker, chapter=claim, authorize=authorize or auth,
                                              verify=verify or check)


def _real_provider(base_url="http://127.0.0.1:9/v1/"):
    factory = ai_dispatch.chapter_provider_factory(create_provider)
    return factory("openai_compat", "key", base_url, 3)


async def test_chapter_transport_rejects_forged_factory_and_provider(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    called = []

    def unsafe_factory(*args):
        called.append(args)
        return OpenAICompatProvider("key", "http://127.0.0.1:9/v1/", 3)

    with pytest.raises(VisibilityPolicyError):
        ai_dispatch.chapter_provider_factory(unsafe_factory)
    assert called == []

    class ForgedProvider(OpenAICompatProvider):
        uses_dispatch_hooks = True

    class UnhookedNetworkProvider(LLMProvider):
        # 自称支持 hooks 却根本不调用它，构造期就必须被拒绝。
        uses_dispatch_hooks = True

        async def generate_stream(self, messages, model, **kwargs):
            called.append("outbound")
            yield "不应发出"

    for provider in (ForgedProvider("key", None, 3), OpenAICompatProvider("key", None, 3),
                     UnhookedNetworkProvider("key", None, 3)):
        with pytest.raises(VisibilityPolicyError):
            ai_dispatch.guard_chapter_provider(provider, stamp, app.state.sessionmaker, chapter=claim)
    assert called == []

    checked_factory = ai_dispatch.chapter_provider_factory(create_provider)
    provider = checked_factory("openai_compat", "key", "http://127.0.0.1:9/v1/", 3)
    with pytest.raises(VisibilityPolicyError):
        ai_dispatch.guard_chapter_provider(provider, stamp, app.state.sessionmaker, chapter=claim)
    protected = ai_dispatch.guard_chapter_provider(
        _BoundModelProvider(provider, "test-model", "embedding"), stamp, app.state.sessionmaker, chapter=claim,
    )
    assert protected is not provider


async def test_chapter_guard_cannot_skip_claim_when_verify_omits_it(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    async with _server(payload=b"{}", mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()

        async def authorize(db):
            await ai_dispatch.authorize_send_actor(db, claim.actor_id)

        async def only_other_checks(db):
            return None

        protected = ai_dispatch.guard_chapter_provider(
            _BoundModelProvider(_real_provider(f"{url}/v1/"), "test-model", "embedding"),
            stamp, app.state.sessionmaker,
            chapter=claim, authorize=authorize, verify=only_other_checks,
        )
        async with app.state.sessionmaker() as db:
            await db.execute(update(Chapter).where(Chapter.id == claim.chapter_id).values(
                generation_claim_key=str(uuid4()),
            ))
            await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await protected.embed(["旧占用"], "test-model")
        assert not received.is_set() and requests == []


async def test_boolean_actor_cannot_masquerade_as_user_one(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    assert claim.actor_id == 1
    async with _server(payload=b"{}", mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()

        async def authorize(db):
            await ai_dispatch.authorize_send_actor(db, True)

        guarded = _guard(app.state.sessionmaker, _real_provider(f"{url}/v1/"), claim, stamp,
                         authorize=authorize)
        with pytest.raises(VisibilityPolicyError):
            await guarded.embed(["不得出站"], "test-model")
        assert not received.is_set() and requests == []


async def test_chapter_guard_rechecks_after_callback_and_rejects_tampering(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    async with _server(payload=b"{}", mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()

        async def overwrite(db):
            await db.execute(update(Chapter).where(Chapter.id == claim.chapter_id).values(
                generation_claim_key=str(uuid4()),
            ))

        guarded = _guard(app.state.sessionmaker, _real_provider(f"{url}/v1/"), claim, stamp, verify=overwrite)
        with pytest.raises(VisibilityPolicyError):
            await guarded.embed(["不得出站"], "test-model")
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(Chapter.generation_claim_key).where(Chapter.id == claim.chapter_id)) \
                == claim.claim_key
        guarded._chapter = replace(claim, claim_key=str(uuid4()))
        with pytest.raises(VisibilityPolicyError):
            await guarded.embed(["不可替换原身份"], "test-model")
        assert not received.is_set() and requests == []


async def test_chapter_guard_rejects_mutated_provider_configuration(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    async with _server(payload=b"{}", mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()
        provider = _real_provider(f"{url}/v1/")
        guarded = _guard(app.state.sessionmaker, provider, claim, stamp)
        provider.api_key = "different-key"
        with pytest.raises(VisibilityPolicyError):
            await guarded.embed(["不得使用变更后的模型凭据"], "test-model")
        assert not received.is_set() and requests == []


async def test_chapter_send_uses_private_configuration_against_aba(app, client, make_client, monkeypatch):
    claim, stamp = await _claimed(app, client, make_client)
    payload = json.dumps({"object": "list", "model": "test-model",
                          "data": [{"index": 0, "embedding": [1.0]}]}).encode()
    async with (
        _server(payload=payload, mime="application/json") as (safe_url, _, safe_headers, safe_body, safe_requests),
        _server(payload=payload, mime="application/json") as (other_url, _, other_headers, other_body, other_requests),
    ):
        safe_headers.set()
        safe_body.set()
        other_headers.set()
        other_body.set()
        source = _real_provider(f"{safe_url}/v1/")
        guarded = _guard(app.state.sessionmaker, source, claim, stamp)
        original_client = OpenAICompatProvider._client
        original_hook = DispatchHooks._before_request
        wire_headers = []

        async def observe(hooks, request):
            wire_headers.append((str(request.url), request.headers.get("authorization")))
            await original_hook(hooks, request)

        def construct_during_aba(provider):
            # 模拟其他执行体恰在请求组装时短暂切换源 provider 并在 hook 前恢复。
            source.api_key, source.base_url = "different-key", f"{other_url}/v1/"
            try:
                return original_client(provider)
            finally:
                source.api_key, source.base_url = "key", f"{safe_url}/v1/"

        monkeypatch.setattr(OpenAICompatProvider, "_client", construct_during_aba)
        monkeypatch.setattr(DispatchHooks, "_before_request", observe)
        assert await guarded.embed(["仅发送到固定配置"], "test-model") == [[1.0]]
        assert len(safe_requests) == 1 and other_requests == []
        assert wire_headers == [(f"{safe_url}/v1/embeddings", "Bearer key")]


@pytest.mark.parametrize("with_chapter", [False, True])
async def test_preflight_is_physical_but_does_not_consume_send_action(app, client, make_client, with_chapter):
    claim, stamp = await _claimed(app, client, make_client)
    events, observed = [], []
    payload = json.dumps({"object": "list", "model": "test-model",
                          "data": [{"index": 0, "embedding": [1.0]}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, _, headers, body, requests):
        headers.set()
        body.set()

        async def authorize(db):
            await ai_dispatch.authorize_send_actor(db, claim.actor_id)

        async def send_verify(db):
            await ai_dispatch.require_actual_send_lease(db, claim.actor_id, claim.project_id)
            events.append("send")

        async def preflight_verify(db):
            await ai_dispatch.require_preflight_lease(db, claim.actor_id, claim.project_id)
            events.append("preflight")
            observed.append(db)

        provider = _real_provider(f"{url}/v1/")
        if with_chapter:
            protected = ai_dispatch.guard_chapter_provider(
                _BoundModelProvider(provider, "test-model", "embedding"), stamp, app.state.sessionmaker,
                chapter=claim, authorize=authorize, verify=send_verify, verify_preflight=preflight_verify,
            )
        else:
            protected = ai_dispatch.guard_provider(
                provider, stamp, app.state.sessionmaker, authorize=authorize, verify=send_verify,
                verify_preflight=preflight_verify,
            )
        with pytest.raises(TypeError):
            protected._send_lease(True)
        with pytest.raises(VisibilityPolicyError):
            async with protected._send_lease():
                pass
        assert events == []
        await protected.preflight()
        assert events == ["preflight"] and requests == []
        with pytest.raises(VisibilityPolicyError):
            await ai_dispatch.require_preflight_lease(observed[0], claim.actor_id, claim.project_id)
        with pytest.raises(VisibilityPolicyError):
            await ai_dispatch.require_actual_send_lease(observed[0], claim.actor_id, claim.project_id)
        assert await protected.embed(["真实发送"], "test-model") == [[1.0]]
        assert events == ["preflight", "send"] and len(requests) == 1


@pytest.mark.parametrize("mode", ["as_send", "as_preflight", "wrong_actor", "new_transaction", "nested"])
async def test_preflight_proof_cannot_be_reused_or_forged(app, client, make_client, mode):
    claim, stamp = await _claimed(app, client, make_client)
    async with _server(payload=b"{}", mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()

        async def authorize(db):
            await ai_dispatch.authorize_send_actor(db, claim.actor_id)

        async def preflight_only(db):
            if mode == "as_preflight":
                await ai_dispatch.require_actual_send_lease(db, claim.actor_id, claim.project_id)
            elif mode == "wrong_actor":
                await ai_dispatch.require_preflight_lease(db, claim.actor_id + 1, claim.project_id)
            elif mode == "new_transaction":
                await db.rollback()
                await db.scalar(select(1))
                await ai_dispatch.require_preflight_lease(db, claim.actor_id, claim.project_id)
            elif mode == "nested":
                async with db.begin_nested():
                    await ai_dispatch.require_preflight_lease(db, claim.actor_id, claim.project_id)
            else:
                await ai_dispatch.require_preflight_lease(db, claim.actor_id, claim.project_id)

        protected = ai_dispatch.guard_chapter_provider(
            _BoundModelProvider(_real_provider(f"{url}/v1/"), "test-model", "embedding"),
            stamp, app.state.sessionmaker, chapter=claim, authorize=authorize,
            verify=preflight_only, verify_preflight=preflight_only,
        )
        with pytest.raises(VisibilityPolicyError):
            if mode == "as_send":
                await protected.embed(["不能跳过单次动作"], "test-model")
            else:
                await protected.preflight()
        assert not received.is_set() and requests == []


async def test_only_live_send_lease_can_prove_the_claim(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    maker = app.state.sessionmaker
    async with maker() as db:
        await authorization_service.require_authenticated_for_send(db, claim.actor_id)
        await ai_dispatch._lock(db, claim.project_id, write=False)
        db.info["send_lease"] = {"actor_id": claim.actor_id, "project_id": claim.project_id, "locked": True}
        with pytest.raises(VisibilityPolicyError):
            await chapter_claim.require_sending_chapter(db, claim)
    observed = []

    async def verify(db):
        await chapter_claim.require_sending_chapter(db, claim)
        observed.append(db)

    provider = _guard(maker, _real_provider(), claim, stamp, verify=verify)
    await provider.preflight()
    assert len(observed) == 1
    with pytest.raises(VisibilityPolicyError):
        await chapter_claim.require_sending_chapter(observed[0], claim)


@pytest.mark.parametrize("mode", [
    "commit", "rollback", "new_transaction", "nested", "connection_nested", "connection_closed",
    "other_task", "wrong_actor", "wrong_claim", "suppressed",
])
async def test_invalidated_lease_never_reaches_real_http(app, client, make_client, mode):
    claim, stamp = await _claimed(app, client, make_client)
    async with _server(payload=b"{}", mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()

        async def verify(db):
            if mode == "commit":
                await db.commit()
            elif mode == "rollback":
                await db.rollback()
            elif mode == "new_transaction":
                await db.rollback()
                await db.scalar(select(1))
            elif mode == "nested":
                async with db.begin_nested():
                    await chapter_claim.require_sending_chapter(db, claim)
            elif mode == "connection_nested":
                connection = await db.connection()
                await connection.begin_nested()
                await chapter_claim.require_sending_chapter(db, claim)
            elif mode == "connection_closed":
                await (await db.connection()).close()
                await chapter_claim.require_sending_chapter(db, claim)
            elif mode == "other_task":
                await asyncio.create_task(chapter_claim.require_sending_chapter(db, claim))
            elif mode == "wrong_actor":
                await chapter_claim.require_sending_chapter(db, replace(claim, actor_id=claim.actor_id + 1))
            elif mode == "wrong_claim":
                await chapter_claim.require_sending_chapter(db, replace(claim, claim_key=str(uuid4())))
            else:
                try:
                    await chapter_claim.require_sending_chapter(db, replace(claim, claim_key=str(uuid4())))
                except VisibilityPolicyError:
                    pass

        provider = _guard(app.state.sessionmaker, _real_provider(f"{url}/v1/"),
                          claim, stamp, verify=verify)
        with pytest.raises(VisibilityPolicyError):
            await provider.embed(["不能发送"], "test-model")
        assert not received.is_set() and requests == []


async def test_real_http_rechecks_claim_for_every_request(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    payload = json.dumps({"object": "list", "model": "test-model",
                          "data": [{"index": 0, "embedding": [1.0]}]}).encode()
    async with _server(payload=payload, mime="application/json") as (url, received, headers, body, requests):
        headers.set()
        body.set()
        provider = _guard(app.state.sessionmaker, _real_provider(f"{url}/v1/"), claim, stamp)
        assert await provider.embed(["可发送"], "test-model") == [[1.0]]
        assert received.is_set() and len(requests) == 1
        async with app.state.sessionmaker() as db:
            await db.execute(update(Chapter).where(Chapter.id == claim.chapter_id).values(
                generation_claim_key=str(uuid4()),
            ))
            await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await provider.embed(["旧占用不能发送"], "test-model")
        assert len(requests) == 1


async def test_cancel_during_verification_releases_sqlite_gate(app, client, make_client):
    claim, stamp = await _claimed(app, client, make_client)
    entered = asyncio.Event()

    async def verify(db):
        await chapter_claim.require_sending_chapter(db, claim)
        entered.set()
        await asyncio.Event().wait()

    provider = _guard(app.state.sessionmaker, _real_provider(),
                      claim, stamp, verify=verify)
    sending = asyncio.create_task(provider.embed(["不得出站"], "test-model"))
    await asyncio.wait_for(entered.wait(), 5)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    async with app.state.sessionmaker() as db:
        await asyncio.wait_for(chapter_claim.reset_generating_chapter(
            db, claim.actor_id, claim.project_id, claim.chapter_id, status="draft",
        ), 5)
        await db.commit()


async def test_repeated_cancel_waits_for_session_close(app, client, make_client, monkeypatch):
    claim, stamp = await _claimed(app, client, make_client)
    verifying, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session = None
    original_close = AsyncSession.close

    async def close(db):
        if db is session:
            closing.set()
            await release.wait()
        await original_close(db)

    monkeypatch.setattr(AsyncSession, "close", close)

    async def verify(db):
        nonlocal session
        session = db
        await chapter_claim.require_sending_chapter(db, claim)
        verifying.set()
        await asyncio.Event().wait()

    provider = _guard(app.state.sessionmaker, _real_provider(), claim, stamp, verify=verify)
    sending = asyncio.create_task(provider.embed(["不得出站"], "test-model"))
    try:
        await asyncio.wait_for(verifying.wait(), 5)
        sending.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        sending.cancel()
        await asyncio.sleep(0)
        assert not sending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(sending, 5)
    async with app.state.sessionmaker() as db:
        assert await chapter_claim.reset_generating_chapter(
            db, claim.actor_id, claim.project_id, claim.chapter_id, status="draft",
        )
        await db.commit()
