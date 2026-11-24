"""通过两个独立应用的公共 HTTP 端口验证跨进程输出，A 执行而 B 只接待 HTTP。"""

import asyncio
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager, closing
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import select

from app.main import create_app
from app.models import Base, ChapterVersion, Task
from app.services.task_engine.engine import RECEIPT_FINALIZED_DETAIL, RECEIPT_FINALIZED_NOTE
from tests.helpers import Gate, create_ai_config, create_project, make_settings, register, wait_until
from tests.schema_helpers import isolated_worker_database
from tests.test_agent_entries_e4id import _invite, _new_conversation, _send
from tests.test_agent_runtime import call
from tests.test_batch_generate import _start as start_batch
from tests.test_collaboration_stream_agent import DENIED, _ProdStream
from tests.test_collaboration_stream_running_e5c1 import _serve


@dataclass
class Pair:
    source: object
    destination: object
    direct_url: str
    remote_url: str


@asynccontextmanager
async def public_pair(tmp_path, fake_llm, monkeypatch, *, wal=False, auto_analyze=False):
    async with AsyncExitStack() as stack:
        if wal:
            with closing(sqlite3.connect(tmp_path / "test.db")) as connection:
                assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            overrides = {}
        else:
            overrides = await stack.enter_async_context(isolated_worker_database(monkeypatch))
        settings = make_settings(
            tmp_path, allow_private_ai_endpoints=True, auto_analyze_enabled=auto_analyze,
            task_peer_bind_host="127.0.0.1", task_peer_advertise_host="127.0.0.1", **overrides,
        )
        source = create_app(settings, provider_factory=fake_llm.factory, start_task_engine=True)
        destination = create_app(settings.model_copy(deep=True), provider_factory=fake_llm.factory,
                                 start_task_engine=False)
        async with source.state.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        for application in (source, destination):
            await stack.enter_async_context(application.router.lifespan_context(application))
        direct_url = await stack.enter_async_context(_serve(source))
        remote_url = await stack.enter_async_context(_serve(destination))
        yield Pair(source, destination, direct_url, remote_url)


@asynccontextmanager
async def accounts(pair, *, collaborator):
    async with AsyncExitStack() as stack:
        owner = await stack.enter_async_context(
            httpx.AsyncClient(base_url=pair.remote_url, trust_env=False, timeout=25),
        )
        await register(owner, "stream-owner")
        await create_ai_config(owner)
        project = await create_project(owner)
        actor, user, member = owner, None, None
        if collaborator:
            actor = await stack.enter_async_context(
                httpx.AsyncClient(base_url=pair.remote_url, trust_env=False, timeout=25),
            )
            user = await register(actor, "stream-member")
            member = await _invite(owner, actor, project["id"], "stream-member", allow_owner_ai=True)
        yield owner, actor, project, user, member


async def chapter(owner, project, number):
    response = await owner.post(f"/api/v1/projects/{project['id']}/chapters",
                                json={"title": f"第{number}章", "content": ""})
    assert response.status_code == 201, response.text
    return response.json()


async def running_task(pair, task):
    async with pair.destination.state.sessionmaker() as db:
        row = await db.scalar(select(Task).where(Task.id == task["id"], Task.statistics_key == task["task_key"]))
        assert row.status == "running"
        assert row.worker_key == pair.source.state.task_engine.lease.worker_key
        assert row.worker_key != pair.destination.state.task_engine.request_worker.worker_key


def visible(marker):
    return lambda frames: any(event in {"snapshot", "delta"} and marker in frame.get("text", "")
                              for event, frame in frames)


def terminal(frames):
    return [(event, data) for event, data in frames if event in {"paused", "error", "done"}]


async def start_generation(actor, row):
    response = await actor.post(f"/api/v1/chapters/{row['id']}/generate", json={"auto_continue": False})
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["chapter", "batch", "agent"])
@pytest.mark.parametrize("collaborator", [False, True], ids=["owner", "editor"])
async def test_peer_and_direct_reader_receive_same_public_events(tmp_path, fake_llm, monkeypatch, kind, collaborator):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=collaborator,
    ) as (owner, actor, project, _, _), AsyncExitStack() as cleanup:
        gate, final = Gate(), Gate()
        if kind == "agent":
            base = f"/api/v1/projects/{project['id']}/agent"
            conversation = await _new_conversation(actor, base)
            fake_llm.push_tools([("text", {"text": "开头。"}), gate, ("text", {"text": "中段。"}), final,
                                 call("get_project_overview", {}, "first"),
                                 call("update_project", {"synopsis": "修改后"}, "second")])
            task = await _send(actor, base, conversation)
        else:
            first = await chapter(owner, project, 1)
            fake_llm.push(["开头。", gate, "中段。", final, "结束。"])
            if kind == "batch":
                second = await chapter(owner, project, 2)
                fake_llm.push("第二章正文。")
                resp = await start_batch(actor, f"/api/v1/projects/{project['id']}", first, second)
                assert resp.status_code == 202, resp.text
                task = resp.json()
            else:
                task = await start_generation(actor, first)
        cleanup.callback(final.release)
        cleanup.callback(gate.release)
        await gate.wait_reached(within=15)
        await running_task(pair, task)
        hub = pair.source.state.task_engine.hubs[task["id"]]
        await wait_until(lambda: hub.text == "开头。", within=10)
        direct, remote = [_ProdStream(url, actor, task) for url in (pair.direct_url, pair.remote_url)]
        cleanup.push_async_callback(direct.aclose)
        cleanup.push_async_callback(remote.aclose)
        await asyncio.gather(direct.wait_for(visible("开头。")), remote.wait_for(visible("开头。")))
        assert hub.subscriber_count == 2
        gate.release()
        await asyncio.gather(direct.wait_for(visible("中段。")), remote.wait_for(visible("中段。")))
        final.release()
        first_events, second_events = await asyncio.gather(direct.finish(), remote.finish())
        assert second_events == first_events
        names = [event for event, _ in second_events]
        assert len(terminal(second_events)) == 1
        assert names[-1] == ("paused" if kind == "agent" else "done")
        if kind == "batch":
            assert [data["index"] for event, data in second_events if event == "chapter_start"] == [1]
            assert [data["index"] for event, data in second_events if event == "chapter_end"] == [0, 1]
        if kind == "agent":
            assert {"tool_start", "tool_end", "proposal"} <= set(names)
        await wait_until(lambda: hub.subscriber_count == 0, within=3)


@pytest.mark.parametrize("collaborator", [False, True], ids=["owner", "editor"])
async def test_disconnect_peer_after_confirmed_delta(tmp_path, fake_llm, monkeypatch, collaborator):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=collaborator,
    ) as (owner, actor, project, _, _), AsyncExitStack() as cleanup:
        before, cut, finally_finish = Gate(), Gate(), Gate()
        fake_llm.push(["开头。", before, "通道可用。", cut, "通道已断。", finally_finish])
        task = await start_generation(actor, await chapter(owner, project, 1))
        for barrier in (before, cut, finally_finish):
            cleanup.callback(barrier.release)
        await before.wait_reached(within=15)
        await running_task(pair, task)
        remote = _ProdStream(pair.remote_url, actor, task)
        cleanup.push_async_callback(remote.aclose)
        await remote.wait_for(visible("开头。"))
        before.release()
        await remote.wait_for(lambda frames: ("delta", {"text": "通道可用。"}) in frames)
        await cut.wait_reached(within=15)
        await pair.source.state.task_engine.peer.stop()
        cut.release()
        await finally_finish.wait_reached(within=15)
        if collaborator:
            assert terminal(await remote.finish()) == [("error", DENIED)]
            assert "通道已断。" not in remote.text
        else:
            await remote.wait_for(lambda frames: any(event == "snapshot" and "通道已断。" in data["text"]
                                                      for event, data in frames), within=12)
        finally_finish.release()
        if not collaborator:
            result = terminal(await remote.finish())
            assert len(result) == 1 and result[0][0] == "done"
            assert result[0][1]["task"]["status"] == "succeeded"
        await wait_until(lambda: task["id"] not in pair.source.state.task_engine._running, within=15)
        replay = _ProdStream(pair.remote_url, actor, task)
        cleanup.push_async_callback(replay.aclose)
        replayed = await replay.finish()
        assert [event for event, _ in replayed] == ["snapshot", "done"]
        assert replayed[0][1]["text"] == "开头。通道可用。通道已断。"


async def test_idle_remote_client_disconnect_releases_real_source_subscription(tmp_path, fake_llm, monkeypatch):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=False,
    ) as (owner, actor, project, _, _), AsyncExitStack() as cleanup:
        gate = Gate()
        fake_llm.push(["首帧", gate, "结束"])
        task = await start_generation(actor, await chapter(owner, project, 1))
        cleanup.callback(gate.release)
        await gate.wait_reached(within=15)
        remote = _ProdStream(pair.remote_url, actor, task)
        cleanup.push_async_callback(remote.aclose)
        await remote.wait_for(visible("首帧"))
        hub = pair.source.state.task_engine.hubs[task["id"]]
        assert hub.subscriber_count == 1
        await remote.aclose()
        await wait_until(lambda: hub.subscriber_count == 0,
                         within=pair.source.state.task_engine.heartbeat_seconds)
        assert task["id"] in pair.source.state.task_engine._running
        gate.release()


async def test_original_http_stream_recovers_unpublished_chapter_receipt(tmp_path, fake_llm, monkeypatch):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=False,
    ) as (owner, _, project, _, _), AsyncExitStack() as cleanup:
        source, destination = pair.source.state.task_engine, pair.destination.state.task_engine
        entered, resume = asyncio.Event(), asyncio.Event()
        finalize = source._finalize

        async def before_finalization(*args, **kwargs):
            entered.set()
            await resume.wait()
            await finalize(*args, **kwargs)

        monkeypatch.setattr(source, "_finalize", before_finalization)
        cleanup.callback(resume.set)
        write, save = Gate(), Gate()
        cleanup.callback(write.release)
        cleanup.callback(save.release)
        fake_llm.push(["原稿。", write, "已保存的结尾。", save])
        first = await chapter(owner, project, 1)
        task = await start_generation(owner, first)
        await write.wait_reached(within=15)
        await running_task(pair, task)
        stream = _ProdStream(pair.remote_url, owner, task)
        cleanup.push_async_callback(stream.aclose)
        await stream.wait_for(visible("原稿。"))
        write.release()
        await stream.wait_for(lambda frames: ("delta", {"text": "已保存的结尾。"}) in frames)
        save.release()
        await asyncio.wait_for(entered.wait(), timeout=15)

        maker = pair.destination.state.sessionmaker
        async with maker() as db:
            row = await db.get(Task, task["id"])
            receipt = row.checkpoint["_chapter_claim"]
            assert row.status == "running" and row.worker_key == source.lease.worker_key
            assert (receipt["phase"], receipt["task_key"], receipt["execution_key"]) == (
                "completed", task["task_key"], row.execution_key,
            )
            version = await db.get(ChapterVersion, receipt["version_id"])
            assert (version.chapter_id, version.content) == (first["id"], "原稿。已保存的结尾。")
        assert not source.hubs[task["id"]].closed and not terminal(stream.events())
        await source.peer.stop()
        # 在原执行者仍卡在终态之前时停止它。新调度器只能读取已提交的业务收据。
        await asyncio.wait_for(source.stop(), timeout=15)
        async with maker() as db:
            assert (await db.get(Task, task["id"])).status == "running"
        cleanup.push_async_callback(destination.stop)
        await destination.start()
        frames = await stream.finish()
        [(event, body)] = terminal(frames)
        assert (event, body["task"]["task_key"], body["task"]["status"]) == ("done", task["task_key"], "succeeded")
        assert body["unpublished"] is True and body["result"] is None and body["detail"] == RECEIPT_FINALIZED_DETAIL
        async with maker() as db:
            row = await db.get(Task, task["id"])
            assert (row.worker_key, row.error) == (destination.lease.worker_key, RECEIPT_FINALIZED_NOTE)
        replay = _ProdStream(pair.remote_url, owner, task)
        cleanup.push_async_callback(replay.aclose)
        assert [name for name, _ in frames[-2:]] == ["snapshot", "done"]
        assert await replay.finish() == frames[-2:]
