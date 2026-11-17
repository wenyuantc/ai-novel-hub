"""修改影响的协作权限（T7 / AC5）：查看者只读、非协作者 404、编辑者可评估与确认，
以及发送中、保存前撤权 / 降级 / 关作者模型授权时失败关闭：不再发送、不保存影响记录、任务不停在 running、
活动资源键释放；已在锁内重验后提交的台账保留。

协作世界与撤权动作复用大纲协作测试的搭建（同一套受控受理 / 发送 lease / 写回重验模板）。
"""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.models import ConsistencyImpact, ConsistencyLedger, Task
from app.services.consistency import impact_task
from tests.consistency_helpers import add_ledger, extraction, fact
from tests.helpers import Gate, parse_sse
from tests.test_collaboration_outline_entry_e4g3 import EDITOR_KEY, STOPPED, _change, _elsewhere, _terminal, _world
from tests.test_consistency_collaboration import _chapters

INTACT = "林远的佩剑完好地挂在腰间。"
BROKEN = "😀 林远的佩剑在山门前断成两截。"
USED = "林远拔出佩剑，一剑逼退来敌。"
VERDICT = json.dumps({"changes": [{"kind": "changed", "before_id": "A1", "after_id": "B1"}]})


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def _sword() -> str:
    return extraction({"category": "item", "subject": "佩剑", "statement": "林远的佩剑断成两截",
                       "quote": "佩剑在山门前断成两截"})


async def _prepare(app, owner, world) -> dict[int, dict]:
    """第 1 章有台账后被改动（可评估），第 2 章有新鲜台账。"""
    chapters = await _chapters(owner, world, {1: INTACT, 2: USED})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, world.project["id"], chapters[1],
                         [fact("1", INTACT, "佩剑完好地挂在腰间", statement="林远的佩剑完好")])
        await add_ledger(db, world.project["id"], chapters[2], [fact("1", USED, "拔出佩剑")])
        await db.commit()
    resp = await owner.patch(f"{world.base}/chapters/{chapters[1]['id']}", json={"content": BROKEN})
    assert resp.status_code == 200, resp.text
    return chapters


def _assess(client, world, chapter):
    return client.post(f"{world.base}/consistency/impacts/{chapter['id']}/assess")


async def _state(app, world, chapter):
    async with app.state.sessionmaker() as db:
        impacts = await db.scalar(select(func.count(ConsistencyImpact.id)).where(
            ConsistencyImpact.project_id == world.project["id"]))
        ledger = await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == chapter["id"]))
        return impacts, ledger


async def _assert_released(app, owner, world, chapter, fake_llm, responses) -> None:
    """活动资源键已释放：作者可立即对同一章评估并成功。"""
    for response in responses:
        fake_llm.push(response)
    author = await _assess(owner, world, chapter)
    assert author.status_code == 202, author.text
    row = await _terminal(app, author.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.owner["id"] and "_collaboration_grant" not in row.input
    assert (await _state(app, world, chapter))[0] == 1


@pytest.mark.parametrize("case", ["viewer", "pending"])
async def test_viewer_is_read_only_and_non_members_get_404(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(client, guest, role="viewer", accept=case == "viewer", allow_owner_ai=True)
    chapters = await _prepare(app, client, world)
    base = f"{world.base}/consistency/impacts"
    for path in (base, f"{base}/running", f"{base}/{chapters[1]['id']}"):
        resp = await guest.get(path)
        assert resp.status_code == (200 if case == "viewer" else 404), (path, resp.text)
    writes = [
        ("patch", f"{base}/{chapters[1]['id']}", {"revision": 0, "acknowledged": []}),
        ("post", f"{base}/{chapters[1]['id']}/assess", None),
    ]
    for method, path, body in writes:
        resp = await getattr(guest, method)(path, **({"json": body} if body is not None else {}))
        if case == "viewer":
            assert resp.status_code == 403 and resp.json()["code"] == "project_readonly", (path, resp.text)
        else:
            assert resp.status_code == 404, (path, resp.text)
        assert "佩剑" not in resp.text
    if case == "viewer":
        detail = (await guest.get(f"{base}/{chapters[1]['id']}")).json()
        assert (detail["can_assess"], detail["ledger_state"]) == (True, "stale")
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
    assert (await _state(app, world, chapters[1]))[0] == 0 and fake_llm.calls == []


async def test_editor_assesses_with_own_model_and_confirms(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    chapters = await _prepare(app, client, world)
    fake_llm.push(_sword())
    fake_llm.push(VERDICT)
    created = await _assess(guest, world, chapters[1])
    assert created.status_code == 202, created.text
    task = created.json()
    # 编辑者的任务流走受控管线：完整收到进度与完成事件（恰在领取瞬间订阅可能被拒，与前端一致重新订阅）
    for _ in range(3):
        resp = await asyncio.wait_for(
            guest.get(f"/api/v1/tasks/{task['id']}/stream", params={"task_key": task["task_key"]}), 30)
        events = parse_sse(resp.text)
        if events[-1][0] != "error" or events[-1][1].get("code") != "permission_denied":
            break
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert events[-1][0] == "done" and events[-1][1]["result"]["items"] == 1, events
    assert row.user_id == world.guest["id"] and row.input["_collaboration_grant"]["invitation_id"]
    # 本人有文本配置：即使作者授权开启也只用本人密钥
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY] * 2
    async with app.state.sessionmaker() as db:
        saved = await db.scalar(select(ConsistencyImpact).where(ConsistencyImpact.chapter_id == chapters[1]["id"]))
        assert (saved.actor_id, saved.task_id, len(saved.items)) == (world.guest["id"], row.id, 1)
    detail = (await guest.get(f"{world.base}/consistency/impacts/{chapters[1]['id']}")).json()
    patched = await guest.patch(f"{world.base}/consistency/impacts/{chapters[1]['id']}", json={
        "revision": detail["revision"], "items": [{"chapter_id": chapters[2]["id"], "status": "dismissed"}]})
    assert patched.status_code == 200 and patched.json()["assessment"]["items"][0]["status"] == "dismissed"
    # 已完成的结果不因随后撤权回滚
    await _change(app, client, world, "revoked")
    assert (await _state(app, world, chapters[1]))[0] == 1
    assert (await guest.get(f"{world.base}/consistency/impacts")).status_code == 404
    owner_view = (await client.get(f"{world.base}/consistency/impacts/{chapters[1]['id']}")).json()
    assert owner_view["assessment"]["items"][0]["status"] == "dismissed"


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "owner_ai_direct", "reinvite"])
async def test_change_during_send_stops_and_saves_nothing(app, client, make_client, fake_llm, change):
    """重抽发送中撤权：结果不落库（台账不动、没有上一版、没有影响记录），不再发送判定请求。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not change.startswith("owner_ai"))
    chapters = await _prepare(app, client, world)
    _, before = await _state(app, world, chapters[1])
    gate = Gate()
    fake_llm.push([gate, _sword()])
    created = await _assess(guest, world, chapters[1])
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    changes: list = []
    await _elsewhere(app, client, world, change, changes)()
    gate.release()
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert row.status in ("failed", "cancelled") and row.result is None, (row.status, row.error)
    if row.status == "failed":
        assert row.error in STOPPED, row.error
    assert len(fake_llm.calls) == 1
    impacts, after = await _state(app, world, chapters[1])
    assert impacts == 0
    assert (after.facts, after.source_hash, after.prev_facts) == (before.facts, before.source_hash, None)
    await _assert_released(app, client, world, chapters[1], fake_llm, [_sword(), VERDICT])


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_change_before_save_writes_no_impact(app, client, make_client, fake_llm, monkeypatch, change):
    """判定已返回、影响记录落库事务开始前撤权：锁内重验失败，不写影响记录；此前已提交的台账（含上一版）保留。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    chapters = await _prepare(app, client, world)
    original_complete = impact_task.complete
    changes: list = []
    revoke = _elsewhere(app, client, world, change, changes)
    sent = 0

    async def change_after_verdict(target, messages):
        nonlocal sent
        text = await original_complete(target, messages)
        sent += 1
        if sent == 2:
            await revoke()
        return text

    monkeypatch.setattr(impact_task, "complete", change_after_verdict)
    fake_llm.push(_sword())
    fake_llm.push(VERDICT)
    created = await _assess(guest, world, chapters[1])
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert sent == 2 and changes
    assert row.status in ("failed", "cancelled") and row.result is None, (row.status, row.error)
    assert len(fake_llm.calls) == 2
    impacts, ledger = await _state(app, world, chapters[1])
    assert impacts == 0
    assert ledger.facts[0]["statement"] == "林远的佩剑断成两截"
    assert ledger.prev_facts[0]["statement"] == "林远的佩剑完好"
    # 作者重新评估：台账已新鲜，只做判定
    await _assert_released(app, client, world, chapters[1], fake_llm, [VERDICT])
    assert len(fake_llm.calls) == 3
