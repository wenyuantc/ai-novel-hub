"""执行端签发后才改变业务依赖；HTTP 端须在真实 SSE 写出前自行拒绝。"""

import asyncio
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.models import AgentConversation, AIConfig, Chapter, Character, Project, SceneReviewAnalysisReceipt, Task
from app.services.task_engine.peer_protocol import PeerSubscriber
from tests.helpers import Gate, analysis_json, wait_until
from tests.test_agent_entries_e4id import _new_conversation, _send
from tests.test_batch_generate import _start as start_batch
from tests.test_c4_multiapp_streams import accounts, chapter, public_pair, terminal
from tests.test_collaboration_editor_proposal import _editor_proposal
from tests.test_collaboration_entity_entry_e4g3b import _answer
from tests.test_collaboration_stream_agent import DENIED, _ProdStream
from tests.test_collaboration_stream_wizard_e5c3 import RESUME_CASES, _launch_resume, _resume_source

PRIVATE_INSTRUCTION = "只存在于原任务的请求说明"
PRIVATE_SOURCE = "构建提示词时使用的原始角色文字"


def test_peer_predicates_cover_registered_stream_kinds():
    from app.services.task_engine import peer_predicates, registry, stream_evidence

    assert peer_predicates._SUPPORTED == {kind for kind in registry._HANDLERS if stream_evidence.stream_kind_open(kind)}


def hold_peer_snapshot(monkeypatch):
    """停在已认证的完整记录返回后、创建 B 的物理发送租约之前。"""
    caught, proceed, packets = asyncio.Event(), asyncio.Event(), []
    original = PeerSubscriber.receive

    async def receive(client):
        packet = await original(client)
        if packet is not None and packet.event == "snapshot" and packet.proof:
            packets.append(packet)
            caught.set()
            await proceed.wait()
        return packet

    monkeypatch.setattr(PeerSubscriber, "receive", receive)
    return caught, proceed, packets


@pytest.mark.parametrize(("changed", "case"), [
    (False, "entity"), (False, "wizard"), (False, "analysis"), (False, "batch"),
    (True, "entity"), (True, "wizard"), (True, "analysis"), (True, "scene"),
    (True, "chapter"), (True, "model"), (True, "agent"), (True, "batch"),
], ids=lambda arg: ("changed" if arg else "current") if type(arg) is bool else arg)
async def test_business_conditions_between_authenticated_frame_and_browser(
    tmp_path, fake_llm, monkeypatch, case, changed,
):
    async with public_pair(tmp_path, fake_llm, monkeypatch, auto_analyze=case == "batch") as pair, accounts(
        pair, collaborator=True,
    ) as (owner, actor, project, _, _), AsyncExitStack() as stack:
        gate = Gate()
        stack.callback(gate.release)
        base = f"/api/v1/projects/{project['id']}"
        source, book_chapter = None, None
        if case == "entity":
            response = await owner.post(f"{base}/characters", json={"name": "山雨", "profile": PRIVATE_SOURCE})
            assert response.status_code == 201, response.text
            source = response.json()["id"]
            async with pair.source.state.sessionmaker.begin() as db:
                await db.execute(update(Character).where(Character.id == source).values(ai_mode="always"))
            answer = _answer("character.generate")
            fake_llm.push([answer[:12], gate, answer[12:]])
            response = await actor.post(f"{base}/characters/generate", json={"instruction": PRIVATE_INSTRUCTION})
        elif case == "batch":
            book_chapter = await chapter(owner, project, 1)
            async with pair.source.state.sessionmaker.begin() as db:
                await db.execute(update(Project).where(Project.id == project["id"]).values(auto_analyze=True))
            fake_llm.push("批次第一章")
            answer = analysis_json(summary="批次收据分析")
            fake_llm.push([answer[:10], gate, answer[10:]])
            response = await start_batch(actor, base, book_chapter, book_chapter)
        elif case == "agent":
            source = await _new_conversation(actor, f"{base}/agent")
            fake_llm.push_tools([("text", {"text": "这一轮的会话回答"}), gate, ("text", {"text": "尾声。"})])
            task = await _send(actor, f"{base}/agent", source)
        elif case == "wizard":
            world = SimpleNamespace(base=base)
            source = await _resume_source(pair.source, actor, world, fake_llm, "characters")
            _, head, marker, tail, _ = RESUME_CASES["characters"]
            task = await _launch_resume(actor, world, fake_llm, source, [gate, head, marker, tail], "characters")
        else:
            book_chapter = await chapter(owner, project, 1)
            if case == "analysis":
                async with pair.source.state.sessionmaker.begin() as db:
                    await db.execute(update(Chapter).where(Chapter.id == book_chapter["id"]).values(content="正文。"))
                answer = analysis_json(summary="公开分析")
            else:
                answer = "山雨将至，主人公抬头望向远方。"
            fake_llm.push([answer[:9], gate, answer[9:]])
            response = await actor.post(f"/api/v1/chapters/{book_chapter['id']}/"
                                        + ("analyze" if case == "analysis" else "generate"), json={})
        if case not in {"wizard", "agent"}:
            assert response.status_code == 202, response.text
            task = response.json()
        await gate.wait_reached(within=15)
        hub = pair.source.state.task_engine.hubs[task["id"]]
        if case == "batch":
            await wait_until(lambda: hub.source is not None and hub.source.guard._batch_stream_shape() == "analysis")
        await wait_until(lambda: bool(hub.text))
        caught, proceed, packets = hold_peer_snapshot(monkeypatch)
        stack.callback(proceed.set)
        remote = _ProdStream(pair.remote_url, actor, task)
        stack.push_async_callback(remote.aclose)
        try:
            await asyncio.wait_for(caught.wait(), 10)
            proof = packets[0].proof
            assert proof["source"] == "publication"
            raw = json.dumps(proof, ensure_ascii=False)
            assert PRIVATE_INSTRUCTION not in raw and PRIVATE_SOURCE not in raw
            assert "_project_model" not in raw and "api_key" not in raw and hub.text not in raw
            if changed:
                async with pair.destination.state.sessionmaker.begin() as db:
                    if case == "batch":
                        assert proof["business"]["business"]["branch"] == "analysis"
                        q = update(SceneReviewAnalysisReceipt).where(
                            SceneReviewAnalysisReceipt.task_key == task["task_key"],
                        ).values(status="failed")
                    elif case == "agent":
                        q = update(AgentConversation).where(AgentConversation.id == source).values(
                            ai_history_epoch=AgentConversation.ai_history_epoch + 1,
                        )
                    elif case == "entity":
                        groups = proof["business"]["business"]["groups"]
                        assert any(g["kind"] == "character" and source in g["parents"] for g in groups)
                        q = update(Character).where(Character.id == source).values(profile="原来源已更新")
                    elif case == "wizard":
                        q = update(Task).where(Task.id == source.id).values(output_text="原向导输出已更新")
                    elif case == "model":
                        q = update(AIConfig).where(AIConfig.model_type == "text").values(model="different-model")
                    elif case == "analysis":
                        q = update(Task).where(Task.id == task["id"]).values(checkpoint={})
                    elif case == "scene":
                        q = update(Chapter).where(Chapter.id == book_chapter["id"]).values(
                            content_revision=Chapter.content_revision + 1,
                        )
                    else:
                        q = update(Chapter).where(Chapter.id == book_chapter["id"]).values(
                            generation_claim_key=str(uuid4()),
                        )
                    assert (await db.execute(q)).rowcount == 1
                proceed.set()
                assert await remote.finish() == [("error", DENIED)]
                assert hub.text not in remote.text
            else:
                proceed.set()
                await remote.wait_for(lambda frames: any(k == "snapshot" and v["text"] == hub.text for k, v in frames))
                gate.release()
                result = terminal(await remote.finish())
                assert len(result) == 1 and result[0][0] == "done", result
        finally:
            proceed.set()
            gate.release()


@pytest.mark.parametrize("changed", [False, True], ids=["current", "changed"])
async def test_peer_analysis_rechecks_applied_proposal_chapter(tmp_path, fake_llm, monkeypatch, changed):
    async with public_pair(tmp_path, fake_llm, monkeypatch, auto_analyze=True) as pair, AsyncExitStack() as stack:
        owner, editor = [await stack.enter_async_context(httpx.AsyncClient(base_url=pair.remote_url, trust_env=False))
                         for _ in range(2)]
        _, target, user, _, _, _, parent, identity, decision = await _editor_proposal(pair.source, owner, editor)
        accepted = await editor.post(f"/api/v1/tasks/{parent.task_id}/rich-proposals/{identity['token']}/accept",
                                     json=decision)
        assert accepted.status_code == 200, accepted.text
        async with pair.destination.state.sessionmaker() as db:
            child = await db.scalar(select(Task).where(Task.kind == "chapter.analyze", Task.user_id == user["id"]))
            assert child.status == "queued" and "_analysis_proposal_source" in child.input
            task = {"id": child.id, "task_key": child.statistics_key}
        gate = Gate()
        answer = analysis_json(summary="来自已采纳的提案")
        fake_llm.push([answer[:10], gate, answer[10:]])
        await pair.source.state.task_engine.start()
        caught, proceed, packets = hold_peer_snapshot(monkeypatch)
        try:
            await gate.wait_reached(within=15)
            hub = pair.source.state.task_engine.hubs[task["id"]]
            await wait_until(lambda: hub.text == answer[:10])
            remote = _ProdStream(pair.remote_url, editor, task)
            stack.push_async_callback(remote.aclose)
            await asyncio.wait_for(caught.wait(), 10)
            assert packets[0].proof["business"]["kind"] == "chapter.analyze"
            if changed:
                async with pair.destination.state.sessionmaker.begin() as db:
                    await db.execute(update(Chapter).where(Chapter.id == target["id"]).values(
                        content_revision=Chapter.content_revision + 1,
                    ))
                proceed.set()
                assert await remote.finish() == [("error", DENIED)]
            else:
                proceed.set()
                await remote.wait_for(lambda frames: any(k == "snapshot" and d["text"] == answer[:10]
                                                         for k, d in frames))
                gate.release()
                assert [event for event, _ in terminal(await remote.finish())] == ["done"]
        finally:
            proceed.set()
            gate.release()


async def test_entity_read_digest_allows_same_project_relationship_edit_and_blocks_cross_project_move(app, client):
    from app.models import RelationshipType
    from app.services import entity_ai_service as entities
    from app.services.ai_dispatch import VisibilityPolicyError
    from tests.helpers import create_project, register

    await register(client)
    project, other = await create_project(client), await create_project(client, title="另一作品")
    response = await client.post(f"/api/v1/projects/{project['id']}/relationship-types",
                                 json={"role_a": "引路人", "role_b": "同行者"})
    assert response.status_code == 201, response.text
    type_id = response.json()["id"]
    async with app.state.sessionmaker() as db:
        rows = await entities._source_rows(db, project["id"], "type", (), (), for_update=False)
        group = entities._EntitySourceGroup("type", (), (), tuple((r.id, entities._source_digest("type", r))
                                                                 for r in rows))
        read_group = entities.EntityStreamGroup.from_source(group)
    assert type_id in read_group.prior_ids and len(read_group.fingerprint) == 64
    statement = update(RelationshipType).where(RelationshipType.id == type_id)
    async with app.state.sessionmaker.begin() as db:
        await db.execute(statement.values(role_a="同书修改"))
    async with app.state.sessionmaker() as db:
        await entities._require_source_groups(db, project["id"], (read_group,), for_update=False, allow_changes=True)
        with pytest.raises(VisibilityPolicyError):
            await entities._require_source_groups(db, project["id"], (read_group,), for_update=True, allow_changes=True)
    async with app.state.sessionmaker.begin() as db:
        await db.execute(statement.values(project_id=other["id"]))
    async with app.state.sessionmaker() as db:
        with pytest.raises(VisibilityPolicyError):
            await entities._require_source_groups(db, project["id"], (read_group,),
                                                  for_update=False, allow_changes=True)
