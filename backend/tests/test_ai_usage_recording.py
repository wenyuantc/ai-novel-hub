"""从模型调用到持久用量的事务边界。"""

import asyncio
import sqlite3
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from app.ai.providers.base import AIProviderError
from app.ai.providers.dispatch_hooks import DispatchHooks, current_dispatch_hooks, use_dispatch_hooks
from app.models import AIConfig, AiUsageCall, User
from app.services import ai_config_service, ai_dispatch, ai_usage_recording, ai_usage_storage
from tests.helpers import create_ai_config, create_project, register


async def test_connectivity_records_model_calls_only(app, client, fake_llm):
    user = await register(client)
    text = await create_ai_config(client)
    vector = await create_ai_config(client, model_type="embedding", model="test-vector")
    image = await create_ai_config(client, model_type="image", model="test-picture")
    fake_llm.push("ok")

    for config in (text, vector, image):
        response = await client.post(f"/api/v1/ai-configs/{config['id']}/test")
        assert response.status_code == 200 and response.json()["ok"], response.text

    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AiUsageCall).order_by(AiUsageCall.started_at)))
    assert [(row.kind, row.model_type, row.status) for row in rows] == [
        ("model.test", "text", "succeeded"), ("model.test", "embedding", "succeeded"),
    ]
    assert all(row.actor_id == user["id"] and row.payer_id == user["id"] for row in rows)
    assert all(row.project_id is None and not row.had_project for row in rows)
    assert all(row.sent and row.input_source == "unknown" for row in rows)


async def test_fallback_stream_records_reported_cached_input(app, client, fake_llm):
    user = await register(client)
    config = await create_ai_config(client)
    maker = app.state.sessionmaker
    async with maker() as db:
        cfg = await db.get(AIConfig, config["id"])
        name = cfg.model
        await ai_usage_storage.set_price(
            db, cfg, currency="CNY", input_price_nano=1_000_000, output_price_nano=2_000_000,
            cached_input_price_nano=0,
        )
        await db.commit()
        raw = await ai_config_service.build_provider(cfg, app.state.cipher, fake_llm.factory, 2, allow_private=False)
        provider = ai_usage_recording.meter_provider(raw, cfg, maker, actor_id=user["id"], kind="model.test")

    fake_llm.push_tools([("usage", {"input_tokens": 10, "cached_input_tokens": 8, "output_tokens": 3})])
    async with aclosing(provider.chat_tools_stream([], name, [])) as stream:
        assert [kind async for kind, _ in stream] == ["usage", "finish"]

    async with maker() as db:
        call = await db.scalar(select(AiUsageCall))
        assert (call.input_tokens, call.cached_input_tokens, call.output_tokens, call.amount_nano) == (10, 8, 3, 8)


async def test_cancelled_stream_finishes_after_config_and_actor_deleted(app, client, fake_llm):
    user = await register(client)
    config = await create_ai_config(client)
    maker = app.state.sessionmaker
    async with maker() as db:
        cfg = await db.get(AIConfig, config["id"])
        assert cfg is not None
        model = cfg.model
        old_instance_key = cfg.instance_key
        raw = await ai_config_service.build_provider(cfg, app.state.cipher, fake_llm.factory, 2, allow_private=False)
        provider = ai_usage_recording.meter_provider(raw, cfg, maker, actor_id=user["id"], kind="model.test")

    fake_llm.push(["首片", "次片"])
    async with aclosing(provider.generate_stream([{"role": "user", "content": "测试"}], model)) as stream:
        assert await anext(stream) == "首片"
        async with maker() as db:
            original = await db.scalar(select(AiUsageCall))
            assert original is not None and original.status == "pending"
            old_user = await db.get(User, user["id"])
            await db.delete(old_user)
            await db.flush()
            db.add(User(id=user["id"], username="replacement", password_hash="unused"))
            await db.commit()

    async with maker() as db:
        saved = await db.get(AiUsageCall, original.id)
    assert saved is not None and saved.status == "cancelled" and saved.sent
    assert saved.actor_id is None and saved.payer_id is None
    assert saved.config_instance_key == old_instance_key


async def test_account_id_reused_between_binding_and_reservation_stays_unassociated(app, client, fake_llm):
    user = await register(client)
    config = await create_ai_config(client)
    maker = app.state.sessionmaker
    async with maker() as db:
        cfg = await db.get(AIConfig, config["id"])
        assert cfg is not None
        model, instance_key = cfg.model, cfg.instance_key
        raw = await ai_config_service.build_provider(cfg, app.state.cipher, fake_llm.factory, 2, allow_private=False)
        provider = ai_usage_recording.meter_provider(raw, cfg, maker, actor_id=user["id"], kind="model.test")

    async with maker() as db:
        actor = await db.get(User, user["id"])
        await db.delete(actor)
        await db.flush()
        db.add(User(id=user["id"], username="replacement", password_hash="unused"))
        await db.flush()
        db.add(AIConfig(
            id=config["id"], user_id=user["id"], name="新账号配置", provider="openai_compat",
            model=model, api_key_enc=app.state.cipher.encrypt("new-key"),
        ))
        await db.commit()

    fake_llm.push("历史请求返回")
    async with aclosing(provider.generate_stream([{"role": "user", "content": "测试"}], model)) as stream:
        assert [piece async for piece in stream] == ["历史请求返回"]

    async with maker() as db:
        saved = await db.scalar(select(AiUsageCall))
    assert saved is not None and saved.status == "succeeded" and saved.sent
    assert saved.config_instance_key == instance_key
    assert saved.actor_id is None and saved.payer_id is None


async def test_transient_recording_failure_retries_database_without_resending_model(app, client, fake_llm, monkeypatch):
    await register(client)
    config = await create_ai_config(client)
    original = ai_usage_storage.finish_call
    updates = []

    async def retry_once(db, call_id, **kwargs):
        updates.append(call_id)
        if len(updates) == 1:
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise OperationalError(None, None, error)
        return await original(db, call_id, **kwargs)

    monkeypatch.setattr(ai_usage_storage, "finish_call", retry_once)
    fake_llm.push("ok")
    response = await client.post(f"/api/v1/ai-configs/{config['id']}/test")

    assert response.status_code == 200 and response.json()["ok"], response.text
    assert len(fake_llm.calls) == 1 and len(updates) == 2 and len(set(updates)) == 1
    async with app.state.sessionmaker() as db:
        saved = await db.scalar(select(AiUsageCall))
    assert saved is not None and saved.id == updates[0] and saved.status == "succeeded"


async def test_rejected_send_does_not_become_billable_usage(app, client, fake_llm):
    user = await register(client)
    project = await create_project(client)
    config = await create_ai_config(client)
    maker = app.state.sessionmaker

    async def reject(_db):
        raise ai_dispatch.VisibilityChangedError()

    async with maker() as db:
        cfg = await db.get(AIConfig, config["id"])
        assert cfg is not None
        name = cfg.model
        raw = await ai_config_service.build_provider(cfg, app.state.cipher, fake_llm.factory, 2, allow_private=False)
        stamp = await ai_dispatch.read_visibility_stamp(db, project["id"])
        guarded = ai_dispatch.guard_provider(raw, stamp, maker, verify=reject)
        provider = ai_usage_recording.meter_provider(
            guarded, cfg, maker, actor_id=user["id"], kind="chapter.generate",
            project_id=project["id"], project_owner_id=user["id"],
        )

    async with aclosing(provider.generate_stream([{"role": "user", "content": "测试"}], name)) as stream:
        with pytest.raises(ai_dispatch.VisibilityChangedError):
            await anext(stream)

    assert fake_llm.calls == []
    async with maker() as db:
        saved = await db.scalar(select(AiUsageCall))
    assert saved is not None and saved.status == "not_sent" and saved.sent is False
    assert saved.amount_nano is None and saved.pricing_status == "not_sent"


async def test_attempt_without_headers_finishes_after_external_lease_closed(app, client, fake_llm):
    user = await register(client)
    config = await create_ai_config(client, model_type="image", model="test-picture")
    maker = app.state.sessionmaker

    @asynccontextmanager
    async def hold_write_lease():
        async with maker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == config["id"]).values(name="租约期间不提交"))
            yield

    async def no_response(_prompt, _model):
        hooks = current_dispatch_hooks()
        assert hooks is not None
        request = httpx.Request("POST", "https://fake-provider.invalid/request")
        for callback in hooks.event_hooks["request"]:
            await callback(request)
        raise AIProviderError("请求已开始但无响应头")

    async with maker() as db:
        cfg = await db.get(AIConfig, config["id"])
        assert cfg is not None
        model = cfg.model
        raw = await ai_config_service.build_provider(cfg, app.state.cipher, fake_llm.factory, 2, allow_private=False)
        raw.generate_image = no_response
        provider = ai_usage_recording.meter_provider(
            raw, cfg, maker, actor_id=user["id"], kind="series.thumbnail",
        )

    hooks = DispatchHooks(hold_write_lease)
    async with asyncio.timeout(3):
        try:
            with use_dispatch_hooks(hooks), pytest.raises(AIProviderError, match="无响应头"):
                await provider.generate_image("测试", model)
        finally:
            await hooks.aclose()

    async with maker() as db:
        saved = await db.scalar(select(AiUsageCall))
    assert saved is not None and saved.status == "failed" and saved.sent is None
    assert saved.kind == "series.thumbnail" and saved.payer_id == user["id"]


async def test_recovery_finishes_only_stale_pending_attempts(app):
    maker = app.state.sessionmaker
    now = datetime.now(UTC)
    old, recent = str(uuid4()), str(uuid4())
    identity = ai_usage_storage.UsageCallIdentity(
        actor_id=None, payer_id=None, provider="gemini", model="test", model_type="text", kind="chapter.generate",
    )
    async with maker() as db:
        await ai_usage_storage.reserve_call(db, identity, call_id=old, started_at=now - timedelta(days=2))
        await ai_usage_storage.reserve_call(db, identity, call_id=recent, started_at=now - timedelta(minutes=5))
        await db.commit()

    await ai_usage_recording.recover_stale_usage(maker)

    async with maker() as db:
        still_running, unresolved = await db.get(AiUsageCall, recent), await db.get(AiUsageCall, old)
    assert still_running.status == "pending" and still_running.finished_at is None
    assert unresolved.status == "interrupted" and unresolved.sent is None
    assert unresolved.amount_nano is None and unresolved.pricing_status == "unconfirmed"


async def test_over_budget_image_is_refused_before_provider_call(app, client, fake_llm):
    from app.core.errors import BadRequestError

    user = await register(client)
    cfg_data = await create_ai_config(client, model_type="image", model="test-picture")
    maker = app.state.sessionmaker
    async with maker() as db:
        cfg = await db.get(AIConfig, cfg_data["id"])
        await ai_usage_storage.set_price(db, cfg, currency="CNY", image_price_nano=2_000_000_000)
        await ai_usage_storage.set_budget(db, user_id=user["id"], currency="CNY",
                                          limit_nano=1_000_000_000, threshold_percent=90)
        await db.commit()
        raw = await ai_config_service.build_provider(cfg, app.state.cipher, fake_llm.factory, 2, allow_private=False)
        provider = ai_usage_recording.meter_provider(raw, cfg, maker, actor_id=user["id"], kind="project.cover")
        with pytest.raises(BadRequestError, match="预算"):
            await provider.generate_image("超预算", cfg.model)
        assert await db.scalar(select(AiUsageCall.id)) is None
