"""已鉴权对端的来源与本地发送租约分别验证，同一任务键不足以替代原订阅。"""

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import replace
from uuid import uuid4

import pytest

from app.core.errors import NotFoundError
from app.models import Task
from app.services.task_engine import peer_guard
from app.services.task_engine.peer_protocol import PeerFrame
from app.services.task_engine.peer_stream import _binding_fields
from app.services.task_engine.stream_lease import send_peer_source_frame
from tests.helpers import Gate
from tests.test_c4_authorization_races import Received
from tests.test_c4_multiapp_streams import accounts, chapter, public_pair, start_generation, terminal
from tests.test_c4_transport import listeners
from tests.test_collaboration_stream_agent import DENIED, _ProdStream
from tests.test_collaboration_stream_replay_e5b import _collect, _finished, _send
from tests.test_collaboration_task_privacy import _setup
from tests.test_task_sse_scope import _binding


async def _replay(app, client, make_client):
    guest = make_client()
    _, editor, project, member = await _setup(app, client, guest)
    task = await _finished(app, editor["id"], project["id"], grant={
        "invitation_id": member["invitation_id"], "revision": member["revision"],
    })
    binding = await _binding(app, editor["id"], *task)
    done = (await _collect(app, binding))[-1]
    return binding, done


@pytest.mark.parametrize("fault", [
    "none", "changed_payload", "retired_packet", "wrong_binding", "wrong_request",
    "wrong_local_source", "wrong_worker", "wrong_context",
])
async def test_authenticated_replay_still_needs_exact_local_binding_and_payload(app, second_app, client, make_client,
                                                                               fault):
    binding, (event, data, source) = await _replay(app, client, make_client)
    outcomes = []

    async def serve(_request, pub):
        async def write(name, values, conditions):
            await pub.send({"event": name, "data": values, "proof": conditions})

        outcome = await send_peer_source_frame(
            app.state.sessionmaker, binding, event, data, source, write,
            source_worker=str(uuid4()) if fault == "wrong_worker" else pub.target_key,
        )
        outcomes.append(outcome)
        await pub.confirmation()

    if fault == "wrong_local_source":
        source = object()
    request = {"binding": _binding_fields(binding), "execution_key": None, "handoff": True}
    if fault == "wrong_request":
        request["binding"]["active"] = not request["binding"]["active"]
    async with listeners(app, second_app) as (a, b):
        a.peer.on_subscribe = serve
        target = await b.peer.target(a.lease.worker_key)
        async with b.peer.subscribe(target, request) as sub:
            packet = await sub.receive()
            if fault in {"wrong_local_source", "wrong_worker"}:
                assert packet.proof is None and packet.data["code"] == "permission_denied"
                await sub.ack()
                assert outcomes == ["error"]
                return
            if fault == "wrong_context":
                with pytest.raises(NotFoundError):
                    peer_guard.received_frame(packet, ctx=a.ctx, subscriber=sub)
                await sub.ack()
                return
            frame = peer_guard.received_frame(packet, ctx=b.ctx, subscriber=sub)
            if fault == "retired_packet":
                sub.invalidate()
            elif fault == "changed_payload":
                packet.data["new"] = "旧帧不能认证的新正文"
            elif fault == "wrong_binding":
                binding = replace(binding, active_grant_required=not binding.active_grant_required)
            sent = await _send(second_app, binding, [(packet.event, packet.data, frame)])
            if fault == "none":
                assert sent == [(event, data)]
                await sub.ack()
                assert outcomes == ["sent"]
            else:
                assert len(sent) == 1 and sent[0][0] == "error" and sent[0][1]["code"] == "permission_denied"


def test_received_frame_needs_protocol_receipt():
    candidate = PeerFrame("delta", {"text": "未经认证"}, {}, {}, "a", "b", 1, "subscriber")
    with pytest.raises(NotFoundError):
        peer_guard.received_frame(candidate, ctx=None, subscriber=None)


async def test_refreshed_proof_cannot_cross_subscriptions(app, second_app, monkeypatch):
    async def serve(request, pub):
        await pub.send({"event": "delta", "data": {"text": "同样正文"}, "proof": {"version": 1}})
        await pub.confirmation()

    async with listeners(app, second_app) as (a, b):
        a.peer.on_subscribe = serve
        target = await b.peer.target(a.lease.worker_key)
        async with b.peer.subscribe(target, {}) as sub, b.peer.subscribe(target, {}) as other:
            first = await sub.receive()
            with pytest.raises(NotFoundError):
                peer_guard.received_frame(first, ctx=b.ctx, subscriber=other)
            current = peer_guard.received_frame(first, ctx=b.ctx, subscriber=sub)
            monkeypatch.setattr(sub, "receive", other.receive)
            with pytest.raises(NotFoundError):
                await current.refresh()


async def test_refresh_preserves_payload_and_binds_new_receipt_to_the_same_subscriber(app, second_app):
    data = {"event": "delta", "data": {"text": "原帧"}, "proof": {"version": 1}}

    async def serve(request, pub):
        await pub.send(data)
        assert await pub.confirmation() == "recheck"
        await pub.send({**data, "proof": {"version": 2}})
        assert await pub.confirmation() == "ack"

    async with listeners(app, second_app) as (a, b):
        a.peer.on_subscribe = serve
        target = await b.peer.target(a.lease.worker_key)
        async with b.peer.subscribe(target, {}) as sub:
            first = await sub.receive()

            old = peer_guard.received_frame(first, ctx=b.ctx, subscriber=sub)
            fresh = await old.refresh()
            assert fresh.current() and not old.current()
            assert fresh.packet.data == first.data and fresh.packet.data is not first.data
            assert fresh.packet.proof == {"version": 2}
            await sub.ack()


@pytest.mark.parametrize("replaced", [False, True], ids=["same-instance", "different-instance"])
async def test_editor_live_json_effect_needs_original_instance(tmp_path, fake_llm, monkeypatch, replaced):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=True,
    ) as (owner, editor, project, user, member), AsyncExitStack() as cleanup:
        grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
        pk, key = await _finished(pair.source, user["id"], project["id"], grant=grant)
        effect = {"table": "tasks", "pk": {"id": pk}, "after": {"id": pk}}
        original = json.dumps([{**effect, "_private_identity": {"actor_user_id": user["id"], "statistics_key": key}}])
        before, after = Gate(), Gate()
        cleanup.callback(before.release)
        cleanup.callback(after.release)
        fake_llm.push([before, original, after])
        task = await start_generation(editor, await chapter(owner, project, 1))
        await before.wait_reached(within=15)
        held = Received(monkeypatch, pair, lambda frame: frame.event == "delta")
        remote = _ProdStream(pair.remote_url, editor, task)
        cleanup.push_async_callback(remote.aclose)
        cleanup.callback(held.release.set)
        await remote.wait_for(lambda frames: any(e == "snapshot" for e, _ in frames))
        before.release()
        await asyncio.wait_for(held.ready.wait(), 10)
        assert json.loads(held.frame.data["text"]) == [effect]
        assert key not in json.dumps(held.frame.proof) and "_private_identity" not in json.dumps(held.frame.proof)
        if replaced:
            async with pair.source.state.sessionmaker.begin() as db:
                linked = await db.get(Task, pk)
                linked.statistics_key = str(uuid4())
        held.release.set()
        if replaced:
            frames = await remote.finish()
            assert terminal(frames) == [("error", DENIED)] and all(e != "delta" for e, _ in frames)
        else:
            await remote.wait_for(lambda frames: any(e == "delta" for e, _ in frames))
            after.release()
            frames = await remote.finish()
            assert [json.loads(d["text"]) for e, d in frames if e == "delta"] == [[effect]]
            assert [e for e, _ in terminal(frames)] == ["done"]
