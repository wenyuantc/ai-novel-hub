"""E4i-e：协作者 agent.turn 的撤权受限收尾（裁决 11）、续跑时冻结向量选择的锁内重验、订阅保持 404（裁决 10）。

撤权种类 × 轮次状态（全部经真实 HTTP 入口与真实任务引擎）：
- 种类：撤权（revoke）、降为 viewer、关闭作者模型授权（编辑者本人无模型，回退作者模型）、撤权后重邀（轮次变化）。
- 状态：排队（queued）、待确认（paused 且有 proposed 提案）、待审核但已无待确认提案（paused、提案已处理、未续跑）、
  运行中停在断点（提案已落库、暂停断点写入之前）。

断言：任务 failed（固定协作文案）；本轮仍为 proposed 的提案改为 failed + 固定作废文案、会话内可见且不能再确认；已执行
效果保留；撤权之后零模型调用；作者轮次与授权仍有效的取消不受影响；外部调用未知屏障保留。
"""

import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import AgentChange, AgentConversation, AIConfig, GenerationLog, Project, Task
from app.services import project_service
from app.services.agent import runner, service
from app.services.task_engine import engine as engine_module
from tests.helpers import create_ai_config, create_project, register, task_params, wait_task
from tests.test_agent_entries_e4id import _detail, _invite, _new_conversation, _send
from tests.test_agent_mcp_skills import change_action
from tests.test_agent_runtime import call
from tests.worker_helpers import fence_worker

OWNER_KEY = "sk-owner-e4ie-000001"
ORIGINAL = {"synopsis": "原简介", "theme": "原主题"}
KINDS = ["revoke", "viewer", "model_off", "reinvite"]
STOP = ("paused", "succeeded", "failed", "cancelled", "interrupted")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
COLLAB_FAILED = engine_module.COLLABORATION_FAILURE_MESSAGES["collaboration_revoked"]
VOIDED = service.COLLABORATION_CHANGE_VOIDED


# ---------------- 公共夹具 ----------------


async def _world(app, client, make_client, *, owner_name="e4ie-owner", editor_name="e4ie-editor"):
    """编辑者本人无模型、作者授权回退：四种撤权都对该轮有意义（含关闭作者模型授权）。"""
    owner = await register(client, owner_name)
    await create_ai_config(client, api_key=OWNER_KEY)
    project = await create_project(client)
    patched = await client.patch(f"/api/v1/projects/{project['id']}", json=ORIGINAL)
    assert patched.status_code == 200, patched.text
    editor_client = make_client()
    editor = await register(editor_client, editor_name)
    member = await _invite(client, editor_client, project["id"], editor_name, allow_owner_ai=True)
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)
    return SimpleNamespace(owner=owner, owner_client=client, editor=editor, editor_client=editor_client,
                           editor_name=editor_name, project=project, member=member, base=base, cid=cid)


async def _apply(world, kind):
    pid, uid = world.project["id"], world.editor["id"]
    path = f"/api/v1/projects/{pid}/collaborators/{uid}"
    revision = world.member["revision"]
    if kind in ("revoke", "reinvite"):
        response = await world.owner_client.delete(path, params={"expected_revision": revision})
        assert response.status_code == 204, response.text
        if kind == "reinvite":
            world.member = await _invite(world.owner_client, world.editor_client, pid, world.editor_name,
                                         allow_owner_ai=True)
        return
    changes = {"role": "viewer"} if kind == "viewer" else {"allow_owner_ai": False}
    response = await world.owner_client.patch(path, json={"expected_revision": revision, **changes})
    assert response.status_code == 200, response.text


async def _task(app, task_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Task, task_id)


async def _wait_db(app, task_id, statuses, within=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        row = await _task(app, task_id)
        if row.status in statuses:
            return row
        if loop.time() > deadline:
            raise AssertionError(f"任务 {task_id} 未进入 {statuses}：{row.status}")
        await asyncio.sleep(0.01)


async def _changes(app, task_id):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(AgentChange).where(
            AgentChange.turn_task_id == task_id,
        ).order_by(AgentChange.id)))


async def _project_values(app, project_id):
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, project_id)
        return {key: getattr(project, key) for key in ORIGINAL}


async def _paused_turn(app, world, fake_llm, calls):
    fake_llm.push_tools(calls)
    turn = await _send(world.editor_client, world.base, world.cid)
    done = await wait_task(world.editor_client, turn, within=8.0, statuses=STOP)
    assert done["status"] == "paused", done
    return turn, (await _detail(world.editor_client, world.base, world.cid))["changes"]


def _two_proposals():
    return [call("update_project", {"synopsis": "Agent 简介"}, "p1"),
            call("update_project", {"theme": "Agent 主题"}, "p2")]


async def _assert_voided_and_visible(app, world, task_id, *, expected_ids):
    rows = await _changes(app, task_id)
    voided = [row for row in rows if row.id in expected_ids]
    assert [(row.status, row.error, row.result) for row in voided] == [
        ("failed", VOIDED, {"error": VOIDED}),
    ] * len(expected_ids)
    detail = await world.editor_client.get(f"{world.base}/conversations/{world.cid}")
    if world.kind == "revoke":
        # 撤权后私人作用域不可见（裁决 7 之外的已撤权身份）。
        assert detail.status_code == 404, detail.text
        return
    # 降级 / 关闭模型授权 / 重邀后仍是 accepted 成员：本人旧会话可读，作废状态与原因可见。
    assert detail.status_code == 200, detail.text
    shown = {item["id"]: item for item in detail.json()["changes"]}
    for ident in expected_ids:
        assert (shown[ident]["status"], shown[ident]["error"]) == ("failed", VOIDED)


async def _assert_cannot_confirm(world, view):
    response = await world.editor_client.post(f"{world.base}/changes/{view['id']}/confirm", json=change_action(view))
    if world.kind == "revoke":
        assert response.status_code == 404, response.text
    else:
        assert response.status_code == 409, response.text


async def test_only_current_recovery_worker_can_void_revoked_turn(app, second_app, client, make_client, fake_llm):
    from app.services.task_engine.registry import get_handler

    world = await _world(app, client, make_client)
    turn, _ = await _paused_turn(app, world, fake_llm, _two_proposals())
    engine = app.state.task_engine
    await engine.stop()
    old = await engine.lease.register()
    peer = second_app.state.task_engine
    new = await peer.lease.register()
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, turn["id"])
        original.status, original.execution_key, original.worker_key = "running", str(uuid4()), old
        await db.commit()
    await fence_worker(engine, mirror=False)
    await _apply(world, "revoke")
    handler = get_handler("agent.turn")
    await handler.on_abort(engine.ctx, original)
    assert [row.status for row in await _changes(app, turn["id"])] == ["proposed", "proposed"]
    async with app.state.sessionmaker() as db:
        adopted = await db.get(Task, turn["id"])
        adopted.worker_key = new
        await db.commit()
    await handler.on_abort(engine.ctx, original)
    assert [row.status for row in await _changes(app, turn["id"])] == ["proposed", "proposed"]
    await handler.on_abort(peer.ctx, adopted)
    assert [(row.status, row.error) for row in await _changes(app, turn["id"])] == [("failed", VOIDED)] * 2


# ---------------- 待确认（paused + proposed） ----------------


@pytest.mark.parametrize("kind", KINDS)
async def test_paused_awaiting_confirm_turn_fails_and_voids_proposals(app, client, make_client, fake_llm, kind):
    world = await _world(app, client, make_client)
    world.kind = kind
    turn, views = await _paused_turn(app, world, fake_llm, _two_proposals())
    calls = len(fake_llm.tool_calls)
    await _apply(world, kind)
    # 通知已触发后台复查；显式复查一次保证确定性（同一把扫描锁串行，幂等）。
    await app.state.task_engine.scan_collaboration_paused()
    row = await _task(app, turn["id"])
    assert (row.status, row.error) == ("failed", COLLAB_FAILED)
    # 待审核结果摘要保留（与本人取消待审核一致）。
    assert row.result == {"reason": "confirm", "change_ids": [item["id"] for item in views]}
    await _assert_voided_and_visible(app, world, turn["id"], expected_ids=[item["id"] for item in views])
    await _assert_cannot_confirm(world, views[0])
    assert await _project_values(app, world.project["id"]) == ORIGINAL
    assert len(fake_llm.tool_calls) == calls
    # 会话不再被待确认轮次占用。
    async with app.state.sessionmaker() as db:
        assert await service.active_turn(db, world.cid) is None
    # agent.turn 已接入受控管线：撤权后 404；降级/关模型授权/重邀后仍是 accepted 成员，可回放本人已结束轮次。
    stream = await world.editor_client.get(f"/api/v1/tasks/{turn['id']}/stream", params=task_params(turn))
    if kind == "revoke":
        assert stream.status_code == 404, stream.text
    else:
        assert stream.status_code == 200 and COLLAB_FAILED in stream.text, stream.text


# ---------------- 待审核但已无待确认提案（paused，未续跑） ----------------


@pytest.mark.parametrize("kind", KINDS)
async def test_paused_without_pending_changes_fails_and_keeps_executed_effects(
    app, client, make_client, fake_llm, monkeypatch, kind,
):
    world = await _world(app, client, make_client)
    world.kind = kind
    turn, views = await _paused_turn(app, world, fake_llm, [call("update_project", {"synopsis": "已采纳"}, "p1")])
    original = service.maybe_resume

    async def no_resume(*args, **kwargs):
        return None

    # 确认后不续跑：轮次停在 paused、本轮已无 proposed（续跑失败 / 进程退出的同形状态）。
    monkeypatch.setattr(service, "maybe_resume", no_resume)
    confirmed = await world.editor_client.post(f"{world.base}/changes/{views[0]['id']}/confirm",
                                               json=change_action(views[0]))
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
    monkeypatch.setattr(service, "maybe_resume", original)
    assert (await _task(app, turn["id"])).status == "paused"
    calls = len(fake_llm.tool_calls)
    await _apply(world, kind)
    await app.state.task_engine.scan_collaboration_paused()
    row = await _task(app, turn["id"])
    assert (row.status, row.error) == ("failed", COLLAB_FAILED)
    # 已提交的合法结果不因撤权回滚（R3）。
    assert [(item.status, item.error) for item in await _changes(app, turn["id"])] == [("executed", "")]
    assert (await _project_values(app, world.project["id"]))["synopsis"] == "已采纳"
    assert len(fake_llm.tool_calls) == calls


# ---------------- 排队中 ----------------


@pytest.mark.parametrize("kind", KINDS)
async def test_queued_turn_fails_at_claim_without_model_calls(app, client, make_client, fake_llm, kind):
    world = await _world(app, client, make_client)
    engine = app.state.task_engine
    await engine.stop()
    try:
        turn = await _send(world.editor_client, world.base, world.cid)
        assert (await _task(app, turn["id"])).status == "queued"
        await _apply(world, kind)
    finally:
        await engine.start()
    row = await _wait_db(app, turn["id"], TERMINAL)
    assert (row.status, row.error) == ("failed", COLLAB_FAILED)
    assert fake_llm.tool_calls == []
    assert await _changes(app, turn["id"]) == []


# ---------------- 运行中停在断点 ----------------


@pytest.mark.parametrize("notified", [True, False], ids=["notified", "unnotified"])
@pytest.mark.parametrize("kind", KINDS)
async def test_running_turn_at_checkpoint_fails_and_voids_new_proposals(
    app, client, make_client, fake_llm, monkeypatch, kind, notified,
):
    """提案已落库、暂停断点写入之前撤权：有通知时由撤权复查取消执行体；无通知（跨实例）时由断点锁内复核终止。

    两条路径都经 on_abort 在锁内复核后作废本轮提案，任务以失败结束，不进入 paused。
    """
    world = await _world(app, client, make_client)
    world.kind = kind
    reached, release = asyncio.Event(), asyncio.Event()
    # 第二提案已提交并交付，最终写门禁前暂停；此时撤权请求可以提交。
    # _checkpoint 的调用者已持锁，不能在其入口等待撤权完成。
    original = runner._process_calls

    async def held(job, ctx, checkpoint, sessions, scope):
        proposal_count = 0
        async with aclosing(original(job, ctx, checkpoint, sessions, scope)) as events:
            async for event in events:
                yield event
                if event[0] == "proposal" and job.user_id == world.editor["id"]:
                    proposal_count += 1
                    if proposal_count == 2 and not reached.is_set():
                        reached.set()
                        await asyncio.wait_for(release.wait(), 8)

    monkeypatch.setattr(runner, "_process_calls", held)
    if not notified:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *args, **kwargs: None)
    fake_llm.push_tools(_two_proposals())
    turn = await _send(world.editor_client, world.base, world.cid)
    try:
        await asyncio.wait_for(reached.wait(), 8)
        calls = len(fake_llm.tool_calls)
        await _apply(world, kind)
    finally:
        release.set()
    row = await _wait_db(app, turn["id"], TERMINAL)
    assert row.status == "failed", (row.status, row.error)
    if notified:
        assert row.error == COLLAB_FAILED
    proposals = await _changes(app, turn["id"])
    assert len(proposals) == 2
    await _assert_voided_and_visible(app, world, turn["id"], expected_ids=[item.id for item in proposals])
    assert await _project_values(app, world.project["id"]) == ORIGINAL
    assert len(fake_llm.tool_calls) == calls


# ---------------- 不误伤 ----------------


async def test_author_turn_and_valid_editor_cancel_are_unaffected(app, client, make_client, fake_llm):
    """作者轮次不进入收尾；编辑者授权仍有效时本人取消待确认轮次，提案保持原语义（不作废）。"""
    world = await _world(app, client, make_client)
    world.kind = "revoke"
    author_cid = await _new_conversation(client, world.base, "作者会话")
    fake_llm.push_tools([call("update_project", {"synopsis": "作者 Agent"}, "a1")])
    author_turn = await _send(client, world.base, author_cid)
    assert (await wait_task(client, author_turn, within=8.0, statuses=STOP))["status"] == "paused"
    author_change = (await _detail(client, world.base, author_cid))["changes"][0]

    editor_turn, views = await _paused_turn(app, world, fake_llm, _two_proposals())
    cancelled = await world.editor_client.post(f"/api/v1/tasks/{editor_turn['id']}/cancel",
                                               params=task_params(editor_turn))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
    assert [row.status for row in await _changes(app, editor_turn["id"])] == ["proposed", "proposed"]

    await _apply(world, "revoke")
    assert await app.state.task_engine.scan_collaboration_paused() == 0
    # 已取消轮次（授权有效时的本人取消）不被事后作废；作者轮次保持待确认，可照常确认并续跑。
    assert [row.status for row in await _changes(app, editor_turn["id"])] == ["proposed", "proposed"]
    assert (await _task(app, author_turn["id"])).status == "paused"
    assert [row.status for row in await _changes(app, author_turn["id"])] == ["proposed"]
    confirmed = await client.post(f"{world.base}/changes/{author_change['id']}/confirm",
                                  json=change_action(author_change))
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
    assert (await wait_task(client, author_turn, within=8.0, statuses=STOP))["status"] == "succeeded"


async def test_restricted_finish_keeps_external_unknown_barrier(app, client, make_client, fake_llm):
    """编辑者无 MCP；即使轮次里残留执行中 / 结果未知的外部提案（及作者会话的未知外部结果），收尾也不改它们。"""
    world = await _world(app, client, make_client)
    world.kind = "viewer"
    turn, views = await _paused_turn(app, world, fake_llm, _two_proposals())
    pid = world.project["id"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, turn["id"])
        author_conversation = AgentConversation(user_id=world.owner["id"], project_id=pid)
        db.add(author_conversation)
        await db.flush()
        markers = [
            AgentChange(conversation_id=world.cid, project_id=pid, turn_task_id=task.id,
                        turn_task_key=task.statistics_key, change_key="00000000-0000-4000-8000-0000000000e1",
                        tool_call_id="x-executing", tool_name="mcp_call", arguments={}, kind="external",
                        preview={}, status="executing", seq=90,
                        result={"_external_claim_id": "claim-1", "_external_task_key": task.statistics_key}),
            AgentChange(conversation_id=world.cid, project_id=pid, turn_task_id=task.id,
                        turn_task_key=task.statistics_key, change_key="00000000-0000-4000-8000-0000000000e2",
                        tool_call_id="x-proposed-marked", tool_name="mcp_call", arguments={}, kind="external",
                        preview={}, status="proposed", seq=91,
                        result={"_external_outcome_unknown": True, "error": service.EXTERNAL_INTERRUPTED}),
            AgentChange(conversation_id=author_conversation.id, project_id=pid, turn_task_id=None,
                        turn_task_key=None, change_key="00000000-0000-4000-8000-0000000000e3",
                        tool_call_id="x-author", tool_name="mcp_call", arguments={}, kind="external",
                        preview={}, status="failed", error=service.EXTERNAL_INTERRUPTED, seq=1,
                        result={"_external_outcome_unknown": True, "error": service.EXTERNAL_INTERRUPTED}),
        ]
        db.add_all(markers)
        await db.commit()
        marker_rows = {row.id: (row.status, row.error, row.result) for row in markers}
        assert await project_service.has_unsettled_external_change(db, project_id=pid)
    await _apply(world, "viewer")
    await app.state.task_engine.scan_collaboration_paused()
    assert (await _task(app, turn["id"])).status == "failed"
    async with app.state.sessionmaker() as db:
        for ident, before in marker_rows.items():
            row = await db.get(AgentChange, ident)
            assert (row.status, row.error, row.result) == before
        assert await project_service.has_unsettled_external_change(db, project_id=pid)
        statuses = {row.id: row.status for row in await db.scalars(select(AgentChange).where(
            AgentChange.id.in_([item["id"] for item in views])))}
    assert set(statuses.values()) == {"failed"}


async def test_void_helper_only_touches_clean_proposed_rows_of_exact_turn(app, client, make_client, fake_llm):
    """单元：只作废本轮（id + UUID 精确匹配）的干净 proposed；其他轮次 / 其他状态不动。"""
    world = await _world(app, client, make_client)
    turn, views = await _paused_turn(app, world, fake_llm, _two_proposals())
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, turn["id"])
        wrong_key = await service.void_revoked_turn_changes(
            db, task_id=task.id, task_key="00000000-0000-4000-8000-000000000000", project_id=task.project_id,
        )
        assert wrong_key == []
        voided = await service.void_revoked_turn_changes(
            db, task_id=task.id, task_key=task.statistics_key, project_id=task.project_id,
        )
        await db.commit()
    assert voided == [item["id"] for item in views]
    async with app.state.sessionmaker() as db:
        again = await service.void_revoked_turn_changes(
            db, task_id=task.id, task_key=task.statistics_key, project_id=task.project_id,
        )
    assert again == []


# ---------------- 续跑时冻结向量选择的锁内重验（E4i-c 遗留） ----------------


async def _vector_paused_turn(app, client, make_client, fake_llm):
    from tests.test_agent_handler_e4ib import _send as admit
    from tests.test_agent_tools_e4ic import _retrieval_world

    editor_client = make_client()
    world = await _retrieval_world(app, client, editor_client, allow_owner_ai=True)
    world.editor_client = editor_client
    world.base = f"/api/v1/projects/{world.project['id']}/agent"
    fake_llm.push_tools([call("update_project", {"synopsis": "续跑前采纳"}, "v1")])
    turn = await admit(app, world.editor["id"], world.project["id"], world.conversation_id)
    assert (await wait_task(editor_client, turn, within=8.0, statuses=STOP))["status"] == "paused"
    stored = await _task(app, turn["id"])
    assert stored.input["_shared_embedding"]["mode"] == "vector"
    view = (await _detail(editor_client, world.base, world.conversation_id))["changes"][0]
    return world, turn, view


async def _log_count(app, task_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count(GenerationLog.id)).where(GenerationLog.task_id == task_id))


async def _change_owner_embedding(app, world):
    async with app.state.sessionmaker() as db:
        config = await db.scalar(select(AIConfig).where(
            AIConfig.user_id == world.owner["id"], AIConfig.model_type == "embedding",
        ))
    response = await world.owner_client.patch(f"/api/v1/ai-configs/{config.id}", json={"model": "owner-embed-v2"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("change", ["config_while_paused", "config_after_resume", "revoke", "model_off"])
async def test_resumed_turn_rechecks_frozen_embedding_inside_lock(app, client, make_client, fake_llm, change):
    """paused → queued → running：作者向量配置改动在构建锁内失败关闭（runner 不启动），授权变化在领取锁内收尾。

    两种情况都零作者向量调用、零模型调用，也不新增生成记录。
    """
    world, turn, view = await _vector_paused_turn(app, client, make_client, fake_llm)
    world.owner_client = client
    engine = app.state.task_engine
    embeds, calls, logs = len(fake_llm.embed_calls), len(fake_llm.tool_calls), await _log_count(app, turn["id"])
    if change == "config_while_paused":
        await _change_owner_embedding(app, world)
    await engine.stop()
    try:
        confirmed = await world.editor_client.post(f"{world.base}/changes/{view['id']}/confirm",
                                                   json=change_action(view))
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
        assert (await _task(app, turn["id"])).status == "queued"
        if change == "config_after_resume":
            await _change_owner_embedding(app, world)
        elif change in ("revoke", "model_off"):
            world.editor_name = "e4ic-editor"
            await _apply(world, change)
    finally:
        await engine.start()
    row = await _wait_db(app, turn["id"], TERMINAL)
    assert row.status == "failed", (row.status, row.error)
    if change.startswith("config"):
        # 构建锁内重验：作者配置指纹变化 → 模型配置失效（不重选配置，E6 裁决 20②）。
        assert row.error == "模型配置已失效，请重新选择"
    else:
        assert row.error == COLLAB_FAILED
    assert len(fake_llm.embed_calls) == embeds
    assert len(fake_llm.tool_calls) == calls
    # runner 未启动：本次执行没有任何发送尝试的生成记录。
    assert await _log_count(app, turn["id"]) == logs
    assert (await _project_values(app, world.project["id"]))["synopsis"] == "续跑前采纳"


async def test_resumed_turn_with_unchanged_frozen_embedding_still_runs(app, client, make_client, fake_llm):
    """正向对照：冻结选择未变时续跑照常完成（重验不误伤）。"""
    world, turn, view = await _vector_paused_turn(app, client, make_client, fake_llm)
    confirmed = await world.editor_client.post(f"{world.base}/changes/{view['id']}/confirm", json=change_action(view))
    assert confirmed.status_code == 200, confirmed.text
    row = await _wait_db(app, turn["id"], TERMINAL)
    assert row.status == "succeeded", (row.status, row.error)


# ---------------- 取消打断 SQLite 可见性写锁（复核修复） ----------------


async def test_cancel_during_sqlite_visibility_lock_is_not_masked(app, client):
    """撤权复查取消执行体时可能正落在 `_lock_sqlite` 的 no-op UPDATE 上：连接被作废后 finally 恢复 busy_timeout
    会抛 PendingRollbackError 并掩盖取消，任务被记成「任务执行失败：服务器内部错误」而非协作失效文案。取消必须原样传递。
    """
    import sqlite3

    from sqlalchemy.exc import PendingRollbackError

    from app.services.ai_dispatch import lock_visibility_write

    await register(client, "e4ie-lock-owner")
    project = await create_project(client)
    path = app.state.engine.url.database
    holder = sqlite3.connect(path, timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")
    started = asyncio.Event()

    async def locking():
        async with app.state.sessionmaker() as db:
            await db.scalar(select(Project.id).where(Project.id == project["id"]))
            started.set()
            await lock_visibility_write(db, project["id"])

    work = asyncio.create_task(locking())
    try:
        await started.wait()
        await asyncio.sleep(0.2)  # 已在 busy_timeout 内等待写锁
        assert not work.done()
        work.cancel()
        outcome = (await asyncio.gather(work, return_exceptions=True))[0]
    finally:
        holder.rollback()
        holder.close()
    assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
    assert not isinstance(outcome, PendingRollbackError)
    # 作废的连接没有留下僵尸写锁：其他会话仍能取得同一写锁。
    async with app.state.sessionmaker() as db:
        await lock_visibility_write(db, project["id"])
        await db.rollback()


# ---------------- 断点已提交、暂停终态核验前撤权（复核修复） ----------------


@pytest.mark.parametrize("notified", [True, False], ids=["notified", "unnotified"])
@pytest.mark.parametrize("kind", KINDS)
async def test_revocation_between_checkpoint_and_paused_commit_fails_turn(
    app, client, make_client, fake_llm, monkeypatch, kind, notified,
):
    """待确认断点已提交、paused 终态核验（verify_finalization 按原 grant 重验）之前撤权：

    修复前终态核验抛错后任务停在 running（带 execution_key）、提案仍为 proposed、会话被占用直到进程重启；
    待审核复查只看 paused，永远不会收尾。现在按锁内复核受限收尾为 failed 并作废本轮提案。
    """
    world = await _world(app, client, make_client)
    world.kind = kind
    engine = app.state.task_engine
    reached, release = asyncio.Event(), asyncio.Event()
    original = engine.complete_state

    async def held(task, **kwargs):
        if (kwargs.get("status") == "paused" and task is not None and task.user_id == world.editor["id"]
                and not reached.is_set()):
            reached.set()
            await release.wait()
        return await original(task, **kwargs)

    monkeypatch.setattr(engine, "complete_state", held)
    if not notified:
        monkeypatch.setattr(engine, "notify_collaboration_change", lambda *args, **kwargs: None)
    fake_llm.push_tools(_two_proposals())
    turn = await _send(world.editor_client, world.base, world.cid)
    try:
        await asyncio.wait_for(reached.wait(), 8)
        calls = len(fake_llm.tool_calls)
        proposals = await _changes(app, turn["id"])
        assert [row.status for row in proposals] == ["proposed", "proposed"]
        views = (await _detail(world.editor_client, world.base, world.cid))["changes"]
        await _apply(world, kind)
    finally:
        release.set()
    row = await _wait_db(app, turn["id"], TERMINAL)
    assert (row.status, row.error, row.execution_key) == ("failed", COLLAB_FAILED, None)
    await _assert_voided_and_visible(app, world, turn["id"], expected_ids=[item.id for item in proposals])
    await _assert_cannot_confirm(world, views[0])
    assert await _project_values(app, world.project["id"]) == ORIGINAL
    assert len(fake_llm.tool_calls) == calls
    async with app.state.sessionmaker() as db:
        assert await service.active_turn(db, world.cid) is None
    assert await engine.scan_collaboration_paused() == 0


@pytest.mark.parametrize("kind", ["revoke", "viewer"])
async def test_paused_turn_finished_by_change_notification_alone(app, client, make_client, fake_llm, kind):
    """不显式复查：撤权 / 降级的通知本身触发的待审核复查即会收尾 agent.turn（其余用例的显式复查只为快照确定）。"""
    world = await _world(app, client, make_client)
    world.kind = kind
    turn, views = await _paused_turn(app, world, fake_llm, _two_proposals())
    await _apply(world, kind)
    row = await _wait_db(app, turn["id"], TERMINAL)
    assert (row.status, row.error) == ("failed", COLLAB_FAILED)
    await _assert_voided_and_visible(app, world, turn["id"], expected_ids=[item["id"] for item in views])
