"""覆盖订阅建立后变更拓扑，以及认证连接只发心跳两种路由边界。"""

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from app.models import Project, Task, User
from app.services.task_engine import peer_stream
from app.services.task_engine.peer_protocol import PeerPublisher
from tests.helpers import Gate, wait_task, wait_until
from tests.test_c4_multiapp_streams import accounts, chapter, public_pair, start_generation, terminal
from tests.test_collaboration_stream_agent import _ProdStream


async def test_new_scheduler_receives_already_subscribed_local_queue(tmp_path, fake_llm, monkeypatch):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=False,
    ) as (owner, _, project, _, _), AsyncExitStack() as cleanup:
        first, later = pair.source.state.task_engine, pair.destination.state.task_engine
        chapter_row = await chapter(owner, project, 1)
        await later.request_worker.stop()
        await later.peer.stop()

        async def keep_queued():
            return

        monkeypatch.setattr(first, "_dispatch", keep_queued)
        first_ack = asyncio.Event()
        original_send, original_confirm = PeerPublisher.send, PeerPublisher.confirmation
        terminal_channels = set()

        async def observe_send(channel, frame):
            if frame["event"] == "done":
                terminal_channels.add(channel)
            await original_send(channel, frame)

        async def observe_confirmation(channel):
            kind = await original_confirm(channel)
            if kind == "ack" and channel in terminal_channels:
                first_ack.set()
            return kind

        monkeypatch.setattr(PeerPublisher, "send", observe_send)
        monkeypatch.setattr(PeerPublisher, "confirmation", observe_confirmation)
        direct = await cleanup.enter_async_context(httpx.AsyncClient(base_url=pair.direct_url, cookies=owner.cookies))
        gate = Gate()
        cleanup.callback(gate.release)
        fake_llm.push(["接任后的开头。", gate, "结尾。"])
        task = await start_generation(direct, chapter_row)
        stream = _ProdStream(pair.direct_url, direct, task)
        cleanup.push_async_callback(stream.aclose)
        await stream.wait_for(lambda frames: any(e == "snapshot" and d["task"]["status"] == "queued"
                                                for e, d in frames))
        assert first.hubs[task["id"]].subscriber_count == 1
        await later.peer.start()
        await later.request_worker.start()
        await later.start()
        cleanup.push_async_callback(later.stop)
        await gate.wait_reached(within=15)
        await stream.wait_for(lambda frames: ("delta", {"text": "接任后的开头。"}) in frames)
        assert later.hubs[task["id"]].subscriber_count == 1
        gate.release()
        frames = await stream.finish()
        assert [d["task"]["status"] for e, d in frames if e == "snapshot"] == ["queued"]
        assert [e for e, _ in terminal(frames)] == ["done"]
        await asyncio.wait_for(first_ack.wait(), timeout=5)


async def test_only_authenticated_heartbeats_finish_from_database(tmp_path, fake_llm, monkeypatch):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=False,
    ) as (owner, _, project, _, _), AsyncExitStack() as cleanup:
        source = pair.source.state.task_engine
        entered = asyncio.Event()

        async def heartbeats_only(payload, channel):
            while True:
                await channel.send({"event": "", "data": {}, "proof": None})
                assert await channel.confirmation() == "ack"
                entered.set()
                await asyncio.sleep(0.05)

        monkeypatch.setattr(source.peer, "on_subscribe", heartbeats_only)
        gate = Gate()
        cleanup.callback(gate.release)
        fake_llm.push(["仅保存到库的正文。", gate])
        task = await start_generation(owner, await chapter(owner, project, 1))
        await gate.wait_reached(within=15)
        stream = _ProdStream(pair.remote_url, owner, task)
        cleanup.push_async_callback(stream.aclose)
        await asyncio.wait_for(entered.wait(), timeout=10)
        await wait_until(lambda: ": ping" in stream.text)
        gate.release()
        frames = await stream.finish()
        assert [event for event, _ in frames] == ["snapshot", "done"]
        assert frames[0][1]["text"] == "仅保存到库的正文。"
        assert frames[-1][1]["task"]["task_key"] == task["task_key"]


async def test_unknown_handoff_owner_has_a_single_discovery_deadline(tmp_path, fake_llm, monkeypatch):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=False,
    ) as (owner, _, project, _, _), AsyncExitStack() as cleanup:
        source, destination = pair.source.state.task_engine, pair.destination.state.task_engine
        allow_run, allow_discovery, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        dispatch, read = source._dispatch, peer_stream._read
        checked = False

        async def hold_dispatch():
            if allow_run.is_set():
                await dispatch()

        async def after_queued(engine, binding):
            nonlocal checked
            if engine is destination:
                if checked:
                    await allow_discovery.wait()
                checked = True
            return await read(engine, binding)

        async def silent(payload, channel):
            attempted.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(source, "_dispatch", hold_dispatch)
        monkeypatch.setattr(peer_stream, "_read", after_queued)
        monkeypatch.setattr(source.peer, "on_subscribe", silent)
        monkeypatch.setattr(destination.peer_handoffs, "retention_seconds", 0.5)
        fake_llm.push("已保存。")
        task = await start_generation(owner, await chapter(owner, project, 1))
        stream = _ProdStream(pair.remote_url, owner, task)
        cleanup.push_async_callback(stream.aclose)
        cleanup.callback(allow_discovery.set)
        await stream.wait_for(lambda frames: any(e == "snapshot" and d["task"]["status"] == "queued"
                                                for e, d in frames))
        allow_run.set()
        source.notify()
        assert (await wait_task(owner, task, within=15))["status"] == "succeeded"
        allow_discovery.set()
        await asyncio.wait_for(attempted.wait(), timeout=3)
        events = await stream.finish(within=3)
        assert [e for e, _ in events] == ["snapshot", "snapshot", "done"]
        assert events[-2][1]["text"] == "已保存。"


async def test_public_json_effect_keeps_its_verified_reference_across_peer(tmp_path, fake_llm, monkeypatch):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=False,
    ) as (owner, _, project, _, _), AsyncExitStack() as cleanup:
        gate = Gate()
        cleanup.callback(gate.release)
        script = [gate]
        fake_llm.push(script)
        task = await start_generation(owner, await chapter(owner, project, 1))
        await gate.wait_reached(within=15)
        async with pair.source.state.sessionmaker() as db:
            user_id = (await db.get(Task, task["id"])).user_id
        effect = {"table": "tasks", "pk": {"id": task["id"]}, "before": {"project_id": project["id"]}}
        script.append(json.dumps([{**effect, "_private_identity": {
            "actor_user_id": user_id, "statistics_key": task["task_key"],
        }}]))
        readers = [_ProdStream(url, owner, task) for url in (pair.direct_url, pair.remote_url)]
        for reader in readers:
            cleanup.push_async_callback(reader.aclose)
        await asyncio.gather(*(reader.wait_for(lambda frames: any(e == "snapshot" for e, _ in frames))
                               for reader in readers))
        gate.release()
        for events in await asyncio.gather(*(reader.finish() for reader in readers)):
            deltas = [d["text"] for e, d in events if e == "delta"]
            assert json.loads("".join(deltas)) == [effect]
            assert [e for e, _ in terminal(events)] == ["done"]


@pytest.mark.parametrize("invalidate_account", [False, True], ids=["owner", "old-token"])
async def test_deleting_owner_receives_only_bound_cancellation_or_denial(
    tmp_path, fake_llm, monkeypatch, invalidate_account,
):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=False,
    ) as (owner, _, project, _, _), AsyncExitStack() as cleanup:
        async def keep_queued():
            return

        monkeypatch.setattr(pair.source.state.task_engine, "_dispatch", keep_queued)
        task = await start_generation(owner, await chapter(owner, project, 1))
        readers = [_ProdStream(url, owner, task) for url in (pair.direct_url, pair.remote_url)]
        for reader in readers:
            cleanup.push_async_callback(reader.aclose)
        await asyncio.gather(*(reader.wait_for(lambda frames: any(e == "snapshot" for e, _ in frames))
                               for reader in readers))
        async with pair.source.state.sessionmaker() as db:
            row = await db.get(Task, task["id"])
            if invalidate_account:
                await db.execute(update(User).where(User.id == row.user_id).values(
                    token_version=User.token_version + 1))
            await db.execute(update(Project).where(Project.id == project["id"]).values(
                deletion_started_at=datetime.now(UTC), deletion_token=str(uuid4()),
                deletion_worker_key=pair.source.state.task_engine.request_worker.worker_key))
            row.status = "cancelled"
            await db.commit()
        for events in await asyncio.gather(*(reader.finish() for reader in readers)):
            (event, data), = terminal(events)
            assert event == "error"
            if invalidate_account:
                assert data == {"task": None, "detail": "任务访问权限已失效", "code": "permission_denied"}
            else:
                assert data["detail"] == "任务已取消"
                assert data["task"]["task_key"] == task["task_key"] and data["task"]["chapter_id"] is None
