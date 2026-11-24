"""在 A 已签发帧之后改变持久状态，经过 B 的生产任务流验证业务授权。"""

import asyncio
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update

from app.models import Chapter, Project, SceneReviewProposal, Task
from app.services.task_engine import peer_guard
from app.services.task_engine.peer_protocol import PeerSubscriber
from app.services.task_engine.peer_stream import _binding_fields
from app.services.task_engine.stream_lease import send_peer_source_frame
from tests.helpers import Gate
from tests.test_batch_generate import _start as start_batch
from tests.test_c4_authorization_races import Received
from tests.test_c4_multiapp_streams import accounts, chapter, public_pair, terminal, visible
from tests.test_collaboration_entity_entry_e4g3b import _world
from tests.test_collaboration_stream_agent import DENIED, _ProdStream
from tests.test_collaboration_stream_replay_e5b import _collect, _finished, _send
from tests.test_collaboration_stream_running_e5c1 import _scene_review_world, _settled
from tests.test_collaboration_stream_wizard_e5c3 import _pieces, _text
from tests.test_collaboration_stream_wizard_successor import _hold_done_event
from tests.test_collaboration_wizard_entry_e4g3c import ANSWERS
from tests.test_task_sse_scope import _binding


@pytest.mark.parametrize("kind,third_party", [("characters", False), ("characters", True), ("outline", False)])
async def test_wizard_held_delta_requires_original_save_successor(tmp_path, fake_llm, monkeypatch, kind, third_party):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, AsyncExitStack() as stack:
        owner, editor = [await stack.enter_async_context(httpx.AsyncClient(base_url=pair.remote_url, trust_env=False))
                         for _ in range(2)]
        world = await _world(owner, editor, allow_owner_ai=True)
        head, marker, tail = _pieces(ANSWERS[kind])
        before, finish = Gate(), Gate()
        held = Received(monkeypatch, pair, lambda packet: packet.event == "delta" and packet.data["text"] == marker)
        saved = None
        if kind == "outline":
            saved, allow_done = _hold_done_event(monkeypatch)
            stack.callback(allow_done.set)
            repeated = asyncio.Event()
            recheck = PeerSubscriber.recheck

            async def record_recheck(subscriber):
                await recheck(subscriber)
                repeated.set()

            monkeypatch.setattr(PeerSubscriber, "recheck", record_recheck)
        fake_llm.push([head, before, marker, finish, tail])
        created = await editor.post(f"{world.base}/wizard/{kind}", json={})
        assert created.status_code == 202, created.text
        task = created.json()
        for barrier in (before, finish):
            stack.callback(barrier.release)
        stack.callback(held.release.set)
        await before.wait_reached(within=15)
        remote = _ProdStream(pair.remote_url, editor, task)
        stack.push_async_callback(remote.aclose)
        await remote.wait_for(visible(head))
        before.release()
        await asyncio.wait_for(held.ready.wait(), 10)
        assert held.frame.proof["business"]["business"]["branch"] == "pending"
        finish.release()
        if saved is None:
            assert (await _settled(pair.source, task["id"])).status == "succeeded"
        else:
            await asyncio.wait_for(saved.wait(), 10)
        async with pair.source.state.sessionmaker() as db:
            project = await db.get(Project, world.project["id"])
            if kind == "outline":
                row = await db.get(Task, task["id"])
                assert row.status == "running" and "_wizard_completion" in row.checkpoint
                assert project.ai_history_epoch == held.frame.proof["visibility"][2]
            else:
                assert project.ai_history_epoch > held.frame.proof["visibility"][2]
            if third_party:
                project.ai_visibility_revision += 1
                project.ai_history_epoch += 1
                await db.commit()
        held.release.set()
        if saved is not None:
            # 此时尚未登记终态，B 只能离开租约请求 A 重核同一帧。
            await asyncio.wait_for(repeated.wait(), 3)
            allow_done.set()
        frames = await remote.finish()
        if third_party:
            assert terminal(frames) == [("error", DENIED)] and marker not in remote.text
        else:
            assert terminal(frames)[0][0] == "done" and _text(frames) == ANSWERS[kind]


@pytest.mark.parametrize("discard", [False, True])
async def test_rich_review_still_pending_at_second_hop(tmp_path, fake_llm, monkeypatch, discard):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, AsyncExitStack() as stack:
        owner, editor = [await stack.enter_async_context(httpx.AsyncClient(base_url=pair.remote_url, trust_env=False))
                         for _ in range(2)]
        url, scene, source = await _scene_review_world(pair.source, owner, editor)
        finish = Gate()
        fake_llm.push(["完全不同的", finish, "候选稿正文"])
        held = Received(monkeypatch, pair, lambda frame: frame.event == "paused")
        created = await editor.post(f"{url}/scenes/{scene['id']}/generate", json={
            "expected_content_revision": source["content_revision"],
            "expected_planning_revision": source["planning_revision"], "target_words": 300,
        })
        assert created.status_code == 202, created.text
        task = created.json()
        stack.callback(finish.release)
        stack.callback(held.release.set)
        await finish.wait_reached(within=15)
        remote = _ProdStream(pair.remote_url, editor, task)
        stack.push_async_callback(remote.aclose)
        await remote.wait_for(visible("完全不同的"))
        finish.release()
        await asyncio.wait_for(held.ready.wait(), 10)
        assert held.frame.proof["phase"] == "paused" and held.frame.proof["source"] == "publication"
        if discard:
            async with pair.source.state.sessionmaker() as db:
                changed = await db.execute(update(SceneReviewProposal).where(
                    SceneReviewProposal.task_id == task["id"], SceneReviewProposal.status == "pending",
                ).values(status="discarded"))
                assert changed.rowcount == 1
                await db.commit()
        held.release.set()
        end, = terminal(await remote.finish())
        if discard:
            assert end == ("error", DENIED) and "rich_document_review" not in remote.text
        else:
            assert end[0] == "paused" and end[1]["result"]["task_key"] == task["task_key"]


async def test_batch_attempt_requires_original_chapter_claim_at_second_hop(tmp_path, fake_llm, monkeypatch):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=True,
    ) as (owner, editor, project, _, _), AsyncExitStack() as stack:
        first = await chapter(owner, project, 1)
        second = await chapter(owner, project, 2)
        before, finish = Gate(), Gate()
        marker = "与原章节占用绑定的在途正文。"
        fake_llm.push(["开头。", before, marker, finish, "结尾。"])
        held = Received(monkeypatch, pair, lambda frame: frame.event == "delta" and frame.data["text"] == marker)
        created = await start_batch(editor, f"/api/v1/projects/{project['id']}", first, second)
        assert created.status_code == 202, created.text
        task = created.json()
        for barrier in (before, finish):
            stack.callback(barrier.release)
        stack.callback(held.release.set)
        await before.wait_reached(within=15)
        remote = _ProdStream(pair.remote_url, editor, task)
        stack.push_async_callback(remote.aclose)
        await remote.wait_for(visible("开头。"))
        before.release()
        await asyncio.wait_for(held.ready.wait(), 10)
        assert held.frame.proof["business"]["business"]["branch"] == "attempt"
        async with pair.source.state.sessionmaker() as db:
            updated = await db.execute(update(Chapter).where(
                Chapter.id == first["id"], Chapter.generation_claim_task_key == task["task_key"],
            ).values(generation_claim_key=str(uuid4())))
            assert updated.rowcount == 1
            await db.commit()
        held.release.set()
        assert terminal(await remote.finish()) == [("error", DENIED)] and marker not in remote.text


@pytest.mark.parametrize("identity_changed", [False, True])
async def test_replay_reprojects_private_effect_from_original_at_destination(
    tmp_path, fake_llm, monkeypatch, identity_changed,
):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=True,
    ) as (_, _, project, user, member):
        app = pair.source
        grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
        root = await _finished(app, user["id"], project["id"], grant=grant)
        binding = await _binding(app, user["id"], *root)
        effect_id, effect_key = await _finished(app, user["id"], project["id"], grant=grant)
        effect = {"table": "tasks", "pk": {"id": effect_id}, "after": {"id": effect_id},
                  "_private_identity": {"actor_user_id": user["id"], "statistics_key": effect_key}}
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, binding.task_id)
            task.result = {"effects": [effect]}
            await db.commit()

        event, data, evidence = (await _collect(app, binding))[-1]
        public_effect = {key: value for key, value in effect.items() if key != "_private_identity"}
        assert event == "done" and data["result"]["effects"] == [public_effect]

        async def serve(_request, publish):
            async def send(name, values, proof):
                await publish.send({"event": name, "data": values, "proof": proof})

            outcome = await send_peer_source_frame(
                app.state.sessionmaker, binding, event, data, evidence, send, source_worker=publish.target_key,
            )
            assert outcome == "sent"
            await publish.confirmation()

        source, destination = app.state.task_engine, pair.destination.state.task_engine
        source.peer.on_subscribe = serve
        target = await destination.peer.target(source.lease.worker_key)
        request = {"binding": _binding_fields(binding), "execution_key": None, "handoff": True}
        async with destination.peer.subscribe(target, request) as subscribe:
            packet = await subscribe.receive()
            if identity_changed:
                async with app.state.sessionmaker() as db:
                    linked = await db.get(Task, effect_id)
                    linked.statistics_key = str(uuid4())
                    await db.commit()
            frame = peer_guard.received_frame(packet, ctx=destination.ctx, subscriber=subscribe)
            frames = await _send(pair.destination, binding, [(packet.event, packet.data, frame)])
            assert frames == ([("error", DENIED)] if identity_changed else [(event, data)])
            await subscribe.ack()
