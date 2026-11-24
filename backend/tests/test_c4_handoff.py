"""排队订阅从原执行领取起衔接，正常结束、占满和换代均有可观察的界限。"""

import asyncio
import gc
import weakref
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import Task
from app.services.task_engine.hub import TaskHub
from app.services.task_engine.peer_handoff import HandoffUnavailable, PeerHandoffs
from tests.helpers import wait_task, wait_until
from tests.test_tasks import _generate, _setup


def _running():
    hub = TaskHub(1, statistics_key=str(uuid4()), queued_at=datetime.now(UTC), execution_key=str(uuid4()))
    worker = str(uuid4())
    task = Task(id=1, statistics_key=hub.statistics_key, status="running",
                queued_at=hub.queued_at, execution_key=hub.execution_key, worker_key=worker)
    return hub, worker, task


def _find(cache, task, worker):
    return cache.find(task.id, task.statistics_key, task.queued_at, worker_key=worker, execution_key=task.execution_key)


async def test_prefix_and_live_queue_keep_original_sources_and_order():
    hub, worker, row = _running()
    cache = PeerHandoffs()
    cache.begin(hub, worker)
    try:
        hub.source = "原领取来源"
        hub.set_progress(status="running", stage="执行中")
        hub.source = "原发布来源"
        data = {"章": {"序号": 1}}
        hub.publish("chapter_start", data)
        data["章"]["序号"] = 2
        hub.append("一")
        entry = _find(cache, row, worker)
        assert entry is not None and _find(cache, row, str(uuid4())) is None
        assert cache.find(row.id, row.statistics_key, row.queued_at.replace(tzinfo=None), worker_key=worker) is entry
        shifted = row.queued_at + timedelta(microseconds=1)
        assert cache.find(row.id, row.statistics_key, shifted, worker_key=worker) is None
        assert cache.find(row.id, str(uuid4()), row.queued_at, worker_key=worker) is None
        wrong_execution = str(uuid4())
        assert cache.find(row.id, row.statistics_key, row.queued_at, worker_key=worker, execution_key=wrong_execution
                          ) is None
        sub = cache.attach(entry, row, worker_key=worker, guarded=True)
        hub.append("二")
        hub.close("done", {})
        frames = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        assert [e for e, _, _ in frames] == ["progress", "chapter_start", "delta", "delta", "done"]
        assert frames[1] == ("chapter_start", {"章": {"序号": 1}}, "原发布来源")
        assert frames[0][2] == "原领取来源" and frames[-1][2] == "原发布来源"
    finally:
        cache.clear()


async def test_finished_hub_requires_original_receipt_and_live_worker():
    hub, worker, row = _running()
    cache = PeerHandoffs()
    cache.begin(hub, worker)
    try:
        hub.close("done", {"result": {"value": "成功"}})
        row.status = "succeeded"
        entry = _find(cache, row, worker)
        assert entry is not None
        with pytest.raises(HandoffUnavailable):
            cache.attach(entry, row, worker_key=worker, guarded=False)
        with pytest.raises(HandoffUnavailable):
            cache.attach(entry, row, worker_key=str(uuid4()), guarded=False)
    finally:
        cache.clear()


@dataclass
class Source:
    checkpoint: str


@pytest.mark.parametrize("large", ["payload", "source", "hub", "result", "count"])
async def test_overflow_drops_entire_prefix_without_interrupting_publication(large):
    hub, worker, row = _running()
    cache = PeerHandoffs()
    cache.max_round_bytes = 4096
    cache.max_frames = 2
    cache.begin(hub, worker)
    sub = hub.subscribe()[1]
    entry = _find(cache, row, worker)
    hub.append("开头")
    if large == "count":
        hub.append("二")
        hub.append("三")
    else:
        text = "私有数据" * 8192
        if large == "payload":
            hub.publish("tool_start", {"name": text})
        if large == "source":
            hub.source = Source(text)
            hub.publish("tool_start", {"name": "x"})
        if large == "hub":
            hub.text = text
            hub.publish("progress", {"progress": 25})
        if large == "result":
            hub.result = {"private": text}
            hub.close("done", {"result": None})
    assert _find(cache, row, worker) is None and hub.capture_handoff is None
    assert entry.frames == [] and sub.queue.qsize() >= 2
    with pytest.raises(HandoffUnavailable):
        cache.attach(entry, row, worker_key=worker, guarded=False)


async def test_round_capacity_and_deadline_release_hub_without_further_requests():
    cache = PeerHandoffs()
    cache.max_rounds = 1
    cache.retention_seconds = 0.03
    hub, worker, row = _running()
    cache.begin(hub, worker)
    replacement, other_worker, other_row = _running()
    cache.begin(replacement, other_worker)
    assert _find(cache, row, worker) is None and hub.capture_handoff is None
    assert _find(cache, other_row, other_worker) is not None
    ref = weakref.ref(replacement)
    del replacement
    gc.collect()
    assert ref() is not None
    await wait_until(lambda: ref() is None)


async def test_real_fast_chapter_replays_claim_to_terminal_after_active_hub_removed(app, client, fake_llm):
    engine = app.state.task_engine
    await engine.stop()
    await engine.peer.start()
    assert engine.peer.url
    try:
        _, _, (chapter,) = await _setup(client)
        fake_llm.push("正常的逐帧交接")
        response = await _generate(client, chapter, auto_continue=False)
        assert response.status_code == 202 and response.json()["status"] == "queued"
        task = response.json()
        async with app.state.sessionmaker() as db:
            queued = await db.get(Task, task["id"])
            queued_at, actor_id = queued.queued_at, queued.user_id
        public = dict(statistics_key=task["task_key"], actor_id=actor_id, token_version=0,
                      initial_queued_at=queued_at, active_grant_required=True)
        snapshots = [item async for item in engine.subscribe(task["id"], db_snapshot_only=True, **public)]
        assert len(snapshots) == 1 and snapshots[0][1]["task"]["status"] == "queued"
        await engine.start()
        assert (await wait_task(client, task, within=12))["status"] == "succeeded"
        await wait_until(lambda: task["id"] not in engine.hubs and task["id"] not in engine._running)
        gc.collect()
        entry = engine.peer_handoffs.find(task["id"], task["task_key"], queued_at, worker_key=engine.lease.worker_key)
        assert entry is not None and entry.hub.closed
        async with app.state.sessionmaker() as db:
            finished = await db.get(Task, task["id"])
            receipt = entry.hub.terminal_source
            assert entry.matches_row(finished)
            entry.hub.terminal_source = replace(receipt)
            assert not entry.matches_row(finished)
            entry.hub.terminal_source = receipt
        async with asyncio.timeout(5):
            replay = [item async for item in engine.subscribe(task["id"], handoff=entry, **public)]
        assert replay[0][0] == "progress"
        assert "".join(data["text"] for event, data in replay if event == "delta") == "正常的逐帧交接"
        assert [event for event, _ in replay if event in {"done", "error", "paused", "snapshot"}] == ["done"]
        assert replay[-1][1]["task"]["task_key"] == task["task_key"]
    finally:
        await engine.stop()
        await engine.peer.stop()


@pytest.mark.parametrize("collaborator", [False, True], ids=["owner", "editor"])
async def test_remote_queued_batch_finishes_before_discovery(tmp_path, fake_llm, monkeypatch, collaborator):
    from app.services.task_engine import peer_stream
    from tests.test_c4_multiapp_streams import _ProdStream, accounts, chapter, public_pair, start_batch

    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=collaborator,
    ) as (owner, actor, project, _, _):
        source, destination = pair.source.state.task_engine, pair.destination.state.task_engine
        await source.stop()
        # 此处用屏障强制整轮都发生在两次发现之间；缓存到期和回收另有独立的实时时钟测试。
        monkeypatch.setattr(source.peer_handoffs, "retention_seconds", 60)
        first, second = await chapter(owner, project, 1), await chapter(owner, project, 2)
        fake_llm.push("第一段。")
        fake_llm.push("第二段。")
        response = await start_batch(actor, f"/api/v1/projects/{project['id']}", first, second)
        assert response.status_code == 202 and response.json()["status"] == "queued"
        task = response.json()

        queued_seen, blocked, allow_lookup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        read = peer_stream._read

        async def wait_for_short_task(engine, binding):
            if engine is destination and queued_seen.is_set():
                blocked.set()
                await allow_lookup.wait()
            return await read(engine, binding)

        monkeypatch.setattr(peer_stream, "_read", wait_for_short_task)
        stream = _ProdStream(pair.remote_url, actor, task)
        try:
            await stream.wait_for(lambda frames: any(event == "snapshot" and data["task"]["status"] == "queued"
                                                     for event, data in frames))
            queued_seen.set()
            await asyncio.wait_for(blocked.wait(), 5)
            await source.start()
            await wait_until(lambda: task["id"] in source._running, within=10)
            await wait_until(lambda: task["id"] not in source.hubs and task["id"] not in source._running, within=45)
            gc.collect()
            async with source.ctx.sessionmaker() as db:
                finished = await db.get(Task, task["id"])
                assert finished.status == "succeeded"
                entry = source.peer_handoffs.find(finished.id, finished.statistics_key, finished.queued_at,
                                                  worker_key=source.lease.worker_key)
                assert entry is not None and entry.matches_row(finished)
                published = [event for event, _, _ in entry.frames]
            allow_lookup.set()
            frames = await stream.finish()
            names = [event for event, _ in frames]
            assert names.count("chapter_start") == names.count("chapter_end") == 2, names
            assert names == ["snapshot", *published]
            assert [name for name in names if name in {"done", "paused", "error"}] == ["done"]
            assert frames[-1][1]["task"]["task_key"] == task["task_key"]
        finally:
            allow_lookup.set()
            await stream.aclose()
