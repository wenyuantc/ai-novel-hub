"""FakeLLM 的本地预检只测试钩子调用，不替代真实网络或 PostgreSQL 竞争。"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.ai.providers import create_provider
from app.ai.providers.base import GeneratedImage
from app.ai.providers.dispatch_hooks import DispatchHooks, use_dispatch_hooks
from app.services import ai_dispatch, collaboration_ai_guard
from app.services.ai_dispatch import SendLeaseError, VisibilityChangedError
from tests.helpers import FakeLLM


def test_fixture_trust_is_limited_to_its_own_in_memory_provider(fake_llm):
    create = ai_dispatch.chapter_provider_factory(fake_llm.factory)
    provider = create("openai_compat", "key", None, 1)
    ai_dispatch.require_chapter_transport(provider)
    other = FakeLLM()
    other_provider = other.factory("openai_compat", "key", None, 1)
    assert not ai_dispatch._chapter_provider_allowed(fake_llm.factory, "openai_compat", other_provider)
    assert collaboration_ai_guard._trusted_factory(fake_llm.factory)
    assert collaboration_ai_guard._trusted_provider(fake_llm.factory, provider, "openai_compat")
    assert not collaboration_ai_guard._trusted_provider(fake_llm.factory, other_provider, "openai_compat")
    assert not collaboration_ai_guard._trusted_provider(fake_llm.factory, provider, "anthropic")
    assert not collaboration_ai_guard._trusted_factory(other.factory)
    with pytest.raises(SendLeaseError):
        ai_dispatch.chapter_provider_factory(other.factory)
    assert ai_dispatch._chapter_factory_allowed(create_provider)
    assert collaboration_ai_guard._trusted_factory(create_provider)


@pytest.mark.parametrize("kind", ["generate", "tools", "embed", "image", "models"])
async def test_fake_operations_run_and_release_dispatch_preflight(kind):
    steps = []

    @asynccontextmanager
    async def lease():
        steps.append("acquire")
        try:
            yield
        finally:
            steps.append("release")

    fake = FakeLLM()
    fake.push("正文")
    fake.images.append(GeneratedImage(b"image", "image/png"))
    provider = fake.factory("openai_compat", "key", None, 1)
    hooks = DispatchHooks(lease)
    try:
        with use_dispatch_hooks(hooks):
            if kind == "generate":
                assert "".join([item async for item in provider.generate_stream([], "model")]) == "正文"
            elif kind == "tools":
                assert [item async for item in provider.chat_tools_stream([], "model", [])]
            elif kind == "embed":
                assert len(await provider.embed(["文本"], "model")) == 1
            elif kind == "image":
                assert (await provider.generate_image("提示", "model")).data == b"image"
            else:
                assert await provider.list_models() == []
        assert steps == ["acquire", "release"]
    finally:
        await hooks.aclose()


async def test_fake_preflight_denial_does_not_record_or_consume_model_call():
    @asynccontextmanager
    async def denied():
        raise VisibilityChangedError
        yield  # pragma: no cover

    fake = FakeLLM()
    fake.push("保留响应")
    provider = fake.factory("openai_compat", "key", None, 1)
    hooks = DispatchHooks(denied)
    try:
        with use_dispatch_hooks(hooks), pytest.raises(VisibilityChangedError):
            [item async for item in provider.generate_stream([], "model")]
        assert fake.calls == [] and fake.responses == [["保留响应"]]
    finally:
        await hooks.aclose()


async def test_fake_preflight_cancellation_releases_pending_lease():
    entered, released = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def pending():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield
        finally:
            released.set()

    fake = FakeLLM()
    provider = fake.factory("openai_compat", "key", None, 1)
    hooks = DispatchHooks(pending)

    async def send():
        with use_dispatch_hooks(hooks):
            await provider.embed(["文本"], "model")

    task = asyncio.create_task(send())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released.is_set() and fake.embed_calls == []
    finally:
        await hooks.aclose()
