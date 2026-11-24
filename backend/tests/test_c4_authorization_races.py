"""A 已签发的真实协作者帧在 B 写给公网之前仍服从 B 的授权事务。"""

import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest
from sqlalchemy import delete, event, update

from app.models import AgentChange, User
from app.schemas.auth import Credentials
from app.services import auth_service, collaboration_service
from app.services.task_engine import peer_protocol, peer_stream
from tests.helpers import Gate
from tests.test_agent_entries_e4id import _invite, _member_patch, _new_conversation, _revoke, _send
from tests.test_agent_runtime import call
from tests.test_c4_multiapp_streams import (
    accounts,
    chapter,
    public_pair,
    running_task,
    start_generation,
    terminal,
    visible,
)
from tests.test_collaboration_stream_agent import DENIED, PRIVATE_TOKENS, _ProdStream
from tests.test_collaboration_stream_running_e5c1 import _serve
from tests.test_kb_index_guarded_e6b import _world as knowledge_base

FIRST = "已核准的开头。"
IN_FLIGHT = "只允许原授权发送的在途正文。"
NEXT = "应被复核的下一段。"


class Received:
    """在真实协议读取、完整性与序号检查通过后，暂停交付给 B。"""

    def __init__(self, monkeypatch, pair, predicate):
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.frame = None
        receive = peer_stream._receive

        async def hold(engine, binding, subscriber, wake):
            frame = await receive(engine, binding, subscriber, wake)
            if frame is not None and self.frame is None and predicate(frame):
                assert engine is pair.destination.state.task_engine
                assert peer_protocol.is_authenticated_frame(frame)
                assert frame.source_key == pair.source.state.task_engine.lease.worker_key
                assert frame.target_key == pair.destination.state.task_engine.request_worker.worker_key
                assert frame.proof is not None
                self.frame = frame
                self.ready.set()
                await self.release.wait()
            return frame

        monkeypatch.setattr(peer_stream, "_receive", hold)


async def change_access(pair, owner, editor, project, user, member, change):
    pid, uid, revision = project["id"], user["id"], member["revision"]
    if change in {"revoke", "admin-revoke", "reinvite"}:
        await _revoke(owner, pid, uid, revision)
        if change == "reinvite":
            renewed = await _invite(owner, editor, pid, user["username"], allow_owner_ai=True)
            assert renewed["invitation_id"] != member["invitation_id"]
    elif change == "viewer":
        await _member_patch(owner, pid, uid, revision, role="viewer")
    elif change == "owner-model":
        await _member_patch(owner, pid, uid, revision, allow_owner_ai=False)
    elif change == "password":
        result = await editor.post("/api/v1/auth/change-password", json={
            "old_password": "secret123", "new_password": "updated-secret!",
        })
        assert result.status_code == 204, result.text
    elif change == "disabled":
        credentials = Credentials(username="acceptance-admin", password="secret123")
        async with pair.source.state.sessionmaker() as db:
            await auth_service.create_user(db, credentials, is_admin=True)
        async with httpx.AsyncClient(base_url=pair.direct_url, trust_env=False) as admin:
            result = await admin.post("/api/v1/auth/login", json=credentials.model_dump())
            assert result.status_code == 200, result.text
            result = await admin.patch(f"/api/v1/admin/users/{uid}", json={"is_active": False})
            assert result.status_code == 200 and result.json()["is_active"] is False, result.text
    elif change == "model-selection":
        configs = await owner.get("/api/v1/ai-configs")
        assert configs.status_code == 200, configs.text
        config, = configs.json()
        result = await owner.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "new-model"})
        assert result.status_code == 200, result.text
    elif change == "visibility":
        api = f"/api/v1/projects/{pid}"
        created = await owner.post(f"{api}/characters", json={"name": "林远", "role_type": "protagonist"})
        assert created.status_code == 201, created.text
        entry = f"{api}/setting-entries/character/{created.json()['id']}/ai-settings"
        before = await owner.get(entry)
        assert before.status_code == 200, before.text
        result = await owner.patch(entry, json={
            "expected_ai_visibility_revision": before.json()["ai_visibility_revision"], "ai_mode": "never",
        })
        assert result.status_code == 200, result.text
    else:
        raise AssertionError(change)


@pytest.mark.parametrize("wal", [False, True], ids=["configured-db", "sqlite-wal"])
@pytest.mark.parametrize("change", [
    "revoke", "admin-revoke", "viewer", "owner-model", "password", "disabled", "reinvite", "model-selection",
    "visibility",
])
async def test_change_committed_between_hops_blocks_already_authenticated_delta(tmp_path, fake_llm, monkeypatch,
                                                                               wal, change):
    async with public_pair(tmp_path, fake_llm, monkeypatch, wal=wal) as pair, accounts(
        pair, collaborator=True,
    ) as (owner, editor, project, user, member), AsyncExitStack() as cleanup:
        if change == "admin-revoke":
            async with pair.source.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == user["id"]).values(is_admin=True))
                await db.commit()
            me = await editor.get("/api/v1/auth/me")
            assert me.status_code == 200 and me.json()["is_admin"] is True, me.text
        before, after = Gate(), Gate()
        fake_llm.push([FIRST, before, IN_FLIGHT, after, NEXT])
        cleanup.callback(before.release)
        cleanup.callback(after.release)
        task = await start_generation(editor, await chapter(owner, project, 1))
        await before.wait_reached(within=15)
        await running_task(pair, task)
        held = Received(monkeypatch, pair, lambda frame: frame.event == "delta" and frame.data["text"] == IN_FLIGHT)
        stream = _ProdStream(pair.remote_url, editor, task)
        cleanup.push_async_callback(stream.aclose)
        cleanup.callback(held.release.set)
        await stream.wait_for(visible(FIRST))
        before.release()
        await asyncio.wait_for(held.ready.wait(), 10)
        await after.wait_reached(within=10)
        assert IN_FLIGHT not in stream.text
        prefix = stream.events()
        await change_access(pair, owner, editor, project, user, member, change)
        # 变更已提交，唯一仍待发送的是 A 以合法原身份签过的帧。
        held.release.set()
        frames = await stream.finish()
        assert frames == [*prefix, ("error", DENIED)], frames
        assert IN_FLIGHT not in stream.text and NEXT not in stream.text
        assert all(token not in stream.text for token in PRIVATE_TOKENS)


@pytest.mark.parametrize("collaborator", [False, True], ids=["owner", "editor"])
async def test_running_knowledge_index_keeps_original_reader_scope(tmp_path, fake_llm, monkeypatch, collaborator):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, AsyncExitStack() as cleanup:
        owner = await cleanup.enter_async_context(httpx.AsyncClient(base_url=pair.remote_url, trust_env=False))
        editor = await cleanup.enter_async_context(httpx.AsyncClient(base_url=pair.remote_url, trust_env=False))
        world = await knowledge_base(pair.source, owner, editor, sections=2)
        actor = editor if collaborator else owner
        gate = Gate()
        fake_llm.embed_before_gates = [gate]
        cleanup.callback(gate.release)
        result = await actor.post(f"{world['base']}/kb/documents/{world['doc']}/reindex")
        assert result.status_code == 202, result.text
        task = result.json()
        await gate.wait_reached(within=15)
        await running_task(pair, task)
        stream = _ProdStream(pair.remote_url, actor, task)
        cleanup.push_async_callback(stream.aclose)
        if collaborator:
            assert await stream.finish() == []
            assert stream.status == 404 and "event:" not in stream.text
        else:
            await stream.wait_for(lambda frames: any(name == "snapshot" for name, _ in frames))
            assert pair.source.state.task_engine.hubs[task["id"]].subscriber_count == 1
            gate.release()
            frames = await stream.finish()
            assert [name for name, _ in terminal(frames)] == ["done"], frames
            assert terminal(frames)[0][1]["task"]["status"] == "succeeded"


@pytest.mark.parametrize("wal", [False, True], ids=["configured-db", "sqlite-wal"])
async def test_public_send_holds_member_lock_until_the_bytes_are_written(tmp_path, fake_llm, monkeypatch, wal):
    async with public_pair(tmp_path, fake_llm, monkeypatch, wal=wal) as pair, accounts(
        pair, collaborator=True,
    ) as (owner, editor, project, user, member), AsyncExitStack() as cleanup:
        before, after = Gate(), Gate()
        fake_llm.push([FIRST, before, IN_FLIGHT, after, NEXT])
        cleanup.callback(before.release)
        cleanup.callback(after.release)
        task = await start_generation(editor, await chapter(owner, project, 1))
        await before.wait_reached(within=15)
        await running_task(pair, task)
        entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        async def slow_public_write(scope, receive, send):
            async def slow(message):
                if IN_FLIGHT.encode() in message.get("body", b""):
                    entered.set()
                    await release.wait()
                    await send(message)
                    order.append("sent")
                    return
                await send(message)
            await pair.destination(scope, receive, slow)

        revocation_task = None
        change_member = collaboration_service.change_member

        async def record_revoke(db, *args, **kwargs):
            nonlocal revocation_task
            revocation_task = asyncio.current_task()
            event.listen(db.sync_session, "after_commit", lambda _session: order.append("committed"), once=True)
            return await change_member(db, *args, **kwargs)

        def observe_writer(_conn, _cursor, statement, _parameters, _context, _many):
            if asyncio.current_task() is not revocation_task:
                return
            sql = statement.upper()
            if sql.startswith(("UPDATE ", "BEGIN IMMEDIATE")) or " FOR UPDATE" in sql:
                attempted.set()

        monkeypatch.setattr(collaboration_service, "change_member", record_revoke)
        engine = pair.source.state.engine.sync_engine
        event.listen(engine, "before_cursor_execute", observe_writer)
        cleanup.callback(event.remove, engine, "before_cursor_execute", observe_writer)
        owner_at_source = await cleanup.enter_async_context(httpx.AsyncClient(
            base_url=pair.direct_url, cookies=owner.cookies, trust_env=False, timeout=25,
        ))
        tcp_url = await cleanup.enter_async_context(_serve(slow_public_write))
        stream = _ProdStream(tcp_url, editor, task)
        cleanup.push_async_callback(stream.aclose)
        cleanup.callback(release.set)
        await stream.wait_for(visible(FIRST))
        before.release()
        await asyncio.wait_for(entered.wait(), 10)
        writer = asyncio.create_task(_revoke(owner_at_source, project["id"], user["id"], member["revision"]))
        try:
            await asyncio.wait_for(attempted.wait(), 3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(writer), 0.15)
            assert IN_FLIGHT not in stream.text
            release.set()
            await asyncio.wait_for(writer, 5)
            assert order == ["sent", "committed"]
            after.release()
            frames = await stream.finish()
            assert stream.text.count(IN_FLIGHT) == 1
            assert NEXT not in stream.text
            assert terminal(frames) == [("error", DENIED)], frames
        finally:
            release.set()
            await asyncio.gather(writer, return_exceptions=True)


@pytest.mark.parametrize("discard", [False, True], ids=["valid-proposal", "deleted-between-hops"])
async def test_peer_proposal_is_bound_to_exact_change_at_public_send(tmp_path, fake_llm, monkeypatch, discard):
    async with public_pair(tmp_path, fake_llm, monkeypatch) as pair, accounts(
        pair, collaborator=True,
    ) as (_owner, editor, project, _user, _member), AsyncExitStack() as cleanup:
        api = f"/api/v1/projects/{project['id']}/agent"
        conversation = await _new_conversation(editor, api)
        gate = Gate()
        fake_llm.push_tools([("text", {"text": FIRST}), gate,
                             call("update_project", {"synopsis": "新的简介。"}, "update-1")])
        cleanup.callback(gate.release)
        task = await _send(editor, api, conversation)
        await gate.wait_reached(within=15)
        await running_task(pair, task)
        held = Received(monkeypatch, pair, lambda frame: frame.event == "proposal")
        stream = _ProdStream(pair.remote_url, editor, task)
        cleanup.push_async_callback(stream.aclose)
        cleanup.callback(held.release.set)
        await stream.wait_for(visible(FIRST))
        gate.release()
        await asyncio.wait_for(held.ready.wait(), 10)
        change = held.frame.data["change"]
        assert change["turn_task_key"] == task["task_key"] and change["conversation_id"] == conversation
        if discard:
            async with pair.source.state.sessionmaker() as db:
                result = await db.execute(delete(AgentChange).where(
                    AgentChange.id == change["id"], AgentChange.change_key == change["change_key"],
                ))
                assert result.rowcount == 1
                await db.commit()
        held.release.set()
        frames = await stream.finish()
        proposals = [data["change"] for name, data in frames if name == "proposal"]
        if discard:
            assert not proposals and terminal(frames) == [("error", DENIED)], frames
        else:
            assert proposals == [change] and [name for name, _ in terminal(frames)] == ["paused"], frames
        assert all(token not in stream.text for token in PRIVATE_TOKENS)
