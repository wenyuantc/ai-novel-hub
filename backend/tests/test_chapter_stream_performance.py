"""章节模型碎片在进入昂贵的事件门禁前合并；文本、错误与取消语义保持不变。"""

import asyncio
from collections import Counter
from contextlib import aclosing

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import update

from app.ai.providers import AIProviderError
from app.models import Chapter, User
from app.services import generation_service
from app.services.generation_service import stream_pieces
from app.services.task_engine.handlers.chapter import ChapterHandler
from app.services.task_engine.hub import TaskHub
from tests.helpers import Gate, create_ai_config, create_project, register
from tests.test_collaboration_chapter_entry_e4f import _assert_untouched, _change, _terminal, _world


def _stream(fake_llm, pieces, *, prefix=""):
    provider = fake_llm.factory("openai_compat", "test-key", None, 1)
    return stream_pieces(provider, [], "test-model", max_tokens=2000, thinking="off", pieces=pieces, prefix=prefix)


async def test_ready_character_fragments_do_not_enter_slow_consumer_one_at_a_time(fake_llm):
    raw = "".join(str(index % 10) for index in range(1024))
    fake_llm.push(list(raw))
    pieces, frames = [], []
    async with aclosing(_stream(fake_llm, pieces)) as events:
        async for event, data in events:
            assert event == "delta"
            frames.append(data["text"])
            # 代替每帧同步数据库校验；帧数是延迟放大的来源，不依赖墙钟耗时断言。
            await asyncio.sleep(0.001)

    assert "".join(frames) == "".join(pieces) == raw
    assert len(frames) <= 4
    assert fake_llm.aborted == 0


async def test_async_ready_fragments_are_batched_even_when_provider_yields_control():
    raw = "".join(str(index % 10) for index in range(1024))

    class Provider:
        async def generate_stream(self, _messages, _model, **_kwargs):
            for character in raw:
                await asyncio.sleep(0)
                yield character

    pieces, frames = [], []
    events = stream_pieces(Provider(), [], "test-model", max_tokens=2000, thinking="off", pieces=pieces)
    async with aclosing(events):
        async for event, data in events:
            assert event == "delta"
            frames.append(data["text"])
            await asyncio.sleep(0.001)

    assert "".join(frames) == "".join(pieces) == raw
    assert len(frames) <= 4


async def test_slow_consumer_keeps_model_read_ahead_bounded():
    total, read = 32_768, 0
    closed = asyncio.Event()

    class Provider:
        async def generate_stream(self, _messages, _model, **_kwargs):
            nonlocal read
            try:
                for _ in range(total):
                    read += 1
                    yield "字"
            finally:
                closed.set()

    pieces = []
    events = stream_pieces(Provider(), [], "test-model", max_tokens=40_000, thinking="off", pieces=pieces)
    async with aclosing(events):
        event, data = await asyncio.wait_for(anext(events), 5)
        assert event == "delta" and data["text"]
        # 消费者处理本批期间，只允许有限预读，不能提前吞下整份回复。
        await asyncio.sleep(0)
        assert read <= 8192 < total
        assert "".join(pieces) == data["text"]
    assert closed.is_set()


async def test_continuation_prefix_is_emitted_once_without_becoming_model_text(fake_llm):
    fake_llm.push(list("续写正文，保持原顺序。"))
    pieces = []
    frames = [frame async for frame in _stream(fake_llm, pieces, prefix="\n\n")]

    assert all(event == "delta" for event, _ in frames)
    assert "".join(data["text"] for _, data in frames) == "\n\n续写正文，保持原顺序。"
    assert "".join(pieces) == "续写正文，保持原顺序。"


async def test_empty_model_stream_never_emits_continuation_prefix(fake_llm):
    fake_llm.push([])
    pieces = []
    assert [frame async for frame in _stream(fake_llm, pieces, prefix="\n\n")] == []
    assert pieces == []


async def test_ready_text_is_available_while_later_network_read_is_blocked(fake_llm):
    gate = Gate()
    fake_llm.push(["已返回的正文", gate, "后续正文"])
    pieces = []
    async with aclosing(_stream(fake_llm, pieces)) as events:
        assert await asyncio.wait_for(anext(events), 1) == ("delta", {"text": "已返回的正文"})
        assert "".join(pieces) == "已返回的正文"
    assert fake_llm.aborted == 1


async def test_provider_error_follows_all_ready_text_and_keeps_original_exception(fake_llm):
    failure = AIProviderError("模拟模型流中断")
    fake_llm.push(["甲", "乙", failure])
    pieces, received = [], []
    async with aclosing(_stream(fake_llm, pieces, prefix="\n\n")) as events:
        with pytest.raises(AIProviderError) as caught:
            async for event, data in events:
                assert event == "delta"
                received.append(data["text"])

    assert caught.value is failure
    assert "".join(received) == "\n\n甲乙"
    assert "".join(pieces) == "甲乙"
    assert fake_llm.aborted == 1


async def test_provider_error_before_text_never_emits_prefix(fake_llm):
    failure = AIProviderError("模拟首帧失败")
    fake_llm.push(failure)
    pieces = []
    async with aclosing(_stream(fake_llm, pieces, prefix="\n\n")) as events:
        with pytest.raises(AIProviderError) as caught:
            await anext(events)

    assert caught.value is failure
    assert pieces == []


async def test_cancelling_pending_network_read_closes_provider_without_later_output(fake_llm):
    gate = Gate()
    fake_llm.push(["已返回的正文", gate, "取消后不得出现的正文"])
    pieces = []
    async with aclosing(_stream(fake_llm, pieces)) as events:
        assert await asyncio.wait_for(anext(events), 1) == ("delta", {"text": "已返回的正文"})
        pending = asyncio.create_task(anext(events))
        try:
            await gate.wait_reached(within=1)
        finally:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

    assert fake_llm.aborted == 1
    assert "".join(pieces) == "已返回的正文"


async def test_first_fragment_is_published_without_waiting_for_batch_timer():
    entered, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        async def generate_stream(self, _messages, _model, **_kwargs):
            try:
                entered.set()
                yield "首片立即可见"
                await asyncio.Event().wait()
            finally:
                closed.set()

    events = stream_pieces(Provider(), [], "test-model", max_tokens=2000, thinking="off", pieces=[])
    async with aclosing(events):
        first = asyncio.create_task(anext(events))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            # 只让就绪协程交接控制权，不等待50ms聚合窗口或任何网络计时器。
            for _ in range(12):
                if first.done():
                    break
                await asyncio.sleep(0)
            assert first.done(), "首片不应等待后续碎片的聚合定时器"
            assert first.result() == ("delta", {"text": "首片立即可见"})
        finally:
            if not first.done():
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
    assert closed.is_set()


async def test_provider_cancelled_error_reaches_consumer_and_closes_stream():
    closed = asyncio.Event()

    class Provider:
        async def generate_stream(self, _messages, _model, **_kwargs):
            try:
                yield "取消前的正文"
                raise asyncio.CancelledError
            finally:
                closed.set()

    pieces = []
    events = stream_pieces(Provider(), [], "test-model", max_tokens=2000, thinking="off", pieces=pieces)
    async with aclosing(events):
        assert await asyncio.wait_for(anext(events), 1) == ("delta", {"text": "取消前的正文"})
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(anext(events), 1)
    assert closed.is_set()
    assert pieces == ["取消前的正文"]


async def test_successful_chapter_batches_bound_real_validation_and_output_writes(
    app, client, fake_llm, monkeypatch, record_property,
):
    await register(client, "chapter-performance-owner")
    await create_ai_config(client)
    project = await create_project(client)
    created = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "合并生成"})
    assert created.status_code == 201, created.text
    chapter = created.json()
    raw = "".join(str(index % 10) for index in range(1024))
    fake_llm.push(list(raw))
    verifications = Counter()
    output_writes = 0
    original_verify = ChapterHandler.verify_event

    async def count_real_verification(handler, db, task, **kwargs):
        verifications[kwargs["event"]] += 1
        await original_verify(handler, db, task, **kwargs)

    def count_real_output_write(_connection, _cursor, statement, _parameters, _context, _executemany):
        nonlocal output_writes
        sql = statement.lower().lstrip()
        if sql.startswith("update tasks set ") and "output_text=" in sql.partition(" where ")[0]:
            output_writes += 1

    monkeypatch.setattr(ChapterHandler, "verify_event", count_real_verification)
    monkeypatch.setattr(app.state.task_engine, "flush_interval", 0)
    sqlalchemy_event.listen(app.state.engine.sync_engine, "after_cursor_execute", count_real_output_write)
    try:
        response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={"auto_continue": False})
        assert response.status_code == 202, response.text
        row = await _terminal(app, response.json()["id"])
    finally:
        sqlalchemy_event.remove(app.state.engine.sync_engine, "after_cursor_execute", count_real_output_write)

    assert row.status == "succeeded", row.error
    assert row.output_text == raw
    async with app.state.sessionmaker() as db:
        saved = await db.get(Chapter, chapter["id"])
        assert saved.content == raw and saved.status == "done"
    assert 1 <= verifications["delta"] <= 4, dict(verifications)
    # 设为每批刷库后，校验/写入仍只随批数增长；包含阶段与终态保存的余量。
    assert verifications["flush"] <= verifications["delta"] + verifications["stage"]
    assert 1 <= output_writes <= verifications["delta"] + verifications["stage"] + 2
    assert len(fake_llm.calls) == 1
    record_property("delta_verifications", verifications["delta"])
    record_property("flush_verifications", verifications["flush"])
    record_property("task_output_writes", output_writes)


@pytest.mark.parametrize("change", ["token", "revoked"])
async def test_backlogged_chapter_batches_cannot_publish_after_identity_loss(
    app, client, make_client, fake_llm, monkeypatch, change,
):
    """上一批已发布后撤权；下一批即使已在模型缓冲中，也必须再次通过真实门禁。"""
    guest = make_client()
    world = await _world(client, guest)
    first_consumed, release_next_batch = asyncio.Event(), asyncio.Event()
    published = []
    original_stream = generation_service.stream_pieces
    original_append = TaskHub.append

    async def pause_after_first_consumed_batch(*args, **kwargs):
        async with aclosing(original_stream(*args, **kwargs)) as events:
            async for event in events:
                yield event
                # 引擎已完成这批的实际发布与可能的进度落库，暂停后续业务消费。
                if not first_consumed.is_set():
                    first_consumed.set()
                    await release_next_batch.wait()

    def record_published(hub, text):
        published.append(text)
        original_append(hub, text)

    monkeypatch.setattr(generation_service, "stream_pieces", pause_after_first_consumed_batch)
    monkeypatch.setattr(TaskHub, "append", record_published)
    # 模拟另一实例完成撤权：不允许仅靠进程内取消通知让测试通过。
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    fake_llm.push(list("前" * 4096 + "撤权后禁止发布" * 1024))
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    try:
        await asyncio.wait_for(first_consumed.wait(), 10)
        assert len(published) == 1 and set(published[0]) == {"前"}
        if change == "token":
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == world.guest["id"]).values(
                    token_version=User.token_version + 1,
                ))
                await db.commit()
        else:
            await _change(app, client, world, "revoked")
    finally:
        release_next_batch.set()

    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.result is None
    assert len(published) == 1
    assert row.output_text in ("", published[0])
    assert "撤权后禁止发布" not in row.output_text
    assert fake_llm.aborted == 1
    await _assert_untouched(app, world)
