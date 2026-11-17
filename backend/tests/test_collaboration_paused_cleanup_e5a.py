"""E5-a：降级/撤权/轮次失效后，编辑者名下的待审核（paused）协作任务由系统受限收尾为 failed。

规则同 E4-a：按原 Task UUID + queued_at + grant 在锁内 CAS；已提交的章节与断点保留；作品级批量占用随终态释放；
待审核富文档提案丢弃；不写新内容、不派生、记统计。触发点为协作变更通知后的数据库复查，以及调度器的全量复查
（进程启动后第一轮立即执行，覆盖重启与跨实例未收到通知）。作者任务、管理员取消意图、轮次已变化、仍有占用或
待核对收据的任务不得被误伤。
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.models import Chapter, ProjectCollaborator, SceneReviewProposal, Task, User
from app.models.dashboard import TaskStatistic
from app.services.collaboration_task_access import task_grant_identity
from app.services.task_engine import engine as engine_module
from tests.helpers import drain_collaboration_checks as _drain_checks
from tests.helpers import task_params
from tests.test_collaboration_batch_entry_e4g2 import (
    _assert_saved,
    _assert_untouched,
    _change,
    _start,
    _world,
)
from tests.test_collaboration_review_read import make_review, review_request

MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def _wait_status(app, task_id, statuses, within=15.0):
    async with asyncio.timeout(within):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
            if row.status in statuses and row.execution_key is None:
                return row
            await asyncio.sleep(0.02)


async def _row(app, task_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Task, task_id)


async def _paused_batch(app, owner, guest, fake_llm, **world_kw):
    """编辑者发起三章批量、每章一审：第一章保存后停在审核点。"""
    world = await _world(owner, guest, count=3, **world_kw)
    fake_llm.push("审核点前的第一章")
    created = await _start(guest, world, review_every=1)
    assert created.status_code == 202, created.text
    row = await _wait_status(app, created.json()["id"], {"paused", "failed", "succeeded"})
    assert row.status == "paused" and row.step_cursor == 1, (row.status, row.error)
    return world, row


async def _assert_failed_cleanly(app, world, row, message, fake_llm, calls):
    failed = await _row(app, row.id)
    assert failed.status == "failed" and failed.error == message, (failed.status, failed.error)
    assert failed.statistics_key == row.statistics_key and failed.queued_at == row.queued_at
    assert failed.execution_key is None and failed.finished_at is not None
    # 已提交的第一章与断点保留，后续章节未写、无占用；没有任何新的模型调用或派生任务。
    assert failed.step_cursor == 1 and failed.checkpoint["current"] is None
    assert failed.checkpoint["items"][0]["state"] == "done"
    await _assert_saved(app, world.chapters[0]["id"], "审核点前的第一章", row.id, world.guest["id"])
    await _assert_untouched(app, world.chapters[1]["id"])
    await _assert_untouched(app, world.chapters[2]["id"])
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 1
        stat = await db.get(TaskStatistic, row.statistics_key)
        assert stat is not None and stat.status == "failed"
    return failed


async def _author_can_start_batch(owner, world):
    # 作品级批量互斥随待审核任务结束而释放：作者可立即发起新批量（之前为 409）。
    started = await _start(owner, world, first=1, last=1)
    assert started.status_code == 202, started.text


# ---------------- 通知路径：协作变更后立即复查 ----------------


@pytest.mark.parametrize(("change", "message", "readable"), [
    ("revoked", "collaboration_revoked", 404),
    ("viewer", "collaboration_revoked", 200),
    ("reinvite", "collaboration_revoked", 200),
    # 经 PATCH 关闭作者模型授权会递增 revision：使用本人模型的任务也因原轮次失效而收尾。
    ("owner_ai", "collaboration_revoked", 200),
])
async def test_paused_batch_is_finished_by_system_after_notified_change(
    app, client, make_client, fake_llm, change, message, readable,
):
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm, allow_owner_ai=change == "owner_ai")
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    await _change(app, client, world, change)
    await _drain_checks(engine)
    failed = await _assert_failed_cleanly(app, world, row, MESSAGES[message], fake_llm, 1)
    detail = await guest.get(f"/api/v1/tasks/{row.id}", params=task_params((row.id, row.statistics_key)))
    assert detail.status_code == readable, detail.text
    if readable == 200:
        # 降为 viewer / 重邀后仍是 accepted 成员：可读本人已结束记录，但不能再继续或结束。
        assert detail.json()["status"] == "failed" and detail.json()["error"] == failed.error
        for action in ("resume", "finish"):
            denied = await guest.post(f"/api/v1/tasks/{row.id}/{action}",
                                      params=task_params((row.id, row.statistics_key)))
            assert denied.status_code in (404, 409), denied.text
    assert (await client.get(f"/api/v1/tasks/{row.id}",
                             params=task_params((row.id, row.statistics_key)))).status_code == 404
    await _author_can_start_batch(client, world)


async def test_owner_model_paused_batch_fails_with_model_message_after_direct_toggle(
    app, client, make_client, fake_llm,
):
    """作者模型来源的待审核任务：直接关闭模型授权（不递增 revision、无通知）只能靠全量复查发现。"""
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm, allow_owner_ai=True, editor_model=False)
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    await _change(app, client, world, "owner_ai_direct")
    await _drain_checks(engine)
    assert (await _row(app, row.id)).status == "paused"
    assert await engine.scan_collaboration_paused() == 1
    await _assert_failed_cleanly(app, world, row, MESSAGES["collaboration_model_revoked"], fake_llm, 1)
    await _author_can_start_batch(client, world)


# ---------------- 无通知：全量复查与重启 ----------------


async def _change_without_notification(app, world, change):
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ))
        if change == "viewer":
            member.role, member.revision = "viewer", member.revision + 1
        elif change == "revoked":
            member.status, member.revision = "revoked", member.revision + 1
        elif change == "disabled":
            await db.execute(update(User).where(User.id == world.guest["id"]).values(is_active=False))
        elif change == "token":
            await db.execute(update(User).where(User.id == world.guest["id"]).values(
                token_version=User.token_version + 1,
            ))
        else:
            raise AssertionError(change)
        await db.commit()


@pytest.mark.parametrize("trigger", ["scan", "restart"])
@pytest.mark.parametrize(("change", "message"), [
    ("viewer", "collaboration_revoked"),
    ("revoked", "collaboration_revoked"),
    ("disabled", "collaboration_account_unavailable"),
    ("token", "collaboration_account_unavailable"),
])
async def test_paused_batch_cleanup_without_notification(app, client, make_client, fake_llm, trigger, change,
                                                         message):
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    if trigger == "restart":
        await engine.stop()
    await _change_without_notification(app, world, change)
    if trigger == "scan":
        assert (await _row(app, row.id)).status == "paused"  # 无通知时不会提前变化
        assert await engine.scan_collaboration_paused() == 1
        assert await engine.scan_collaboration_paused() == 0  # 幂等
    else:
        await engine.start()  # 启动后调度器第一轮立即全量复查
        await _wait_status(app, row.id, {"failed"})
        await _drain_checks(engine)
    await _assert_failed_cleanly(app, world, row, MESSAGES[message], fake_llm, 1)
    await _author_can_start_batch(client, world)


# ---------------- 富文档审核点（单章） ----------------


@pytest.mark.parametrize("change", ["downgrade", "revoke"])
async def test_paused_rich_review_is_discarded_by_system(app, client, make_client, change):
    guest = make_client()
    _, user, project, member, identity = await make_review(app, client, guest)
    engine = app.state.task_engine
    await engine.start()
    engine.collaboration_scan_interval = 3600
    await _drain_checks(engine)
    member_path = f"/api/v1/projects/{project['id']}/collaborators/{user['id']}"
    if change == "downgrade":
        response = await client.patch(member_path, json={"expected_revision": member["revision"], "role": "viewer"})
        assert response.status_code == 200, response.text
    else:
        response = await client.delete(member_path, params={"expected_revision": member["revision"]})
        assert response.status_code == 204, response.text
    await _drain_checks(engine)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        assert task.status == "failed" and task.error == MESSAGES["collaboration_revoked"]
        proposal = await db.get(SceneReviewProposal, identity["proposal_id"])
        assert proposal.status == "discarded"
        chapter = await db.get(Chapter, task.chapter_id)
        assert chapter.content == "原文" and chapter.generation_claim_key is None
    path, query = review_request(identity)
    accept = await guest.post(f"{path}/accept", json={**query, "sections": [], "confirm_format_loss": True})
    assert accept.status_code == 404, accept.text
    assert "需要保护的候选正文" not in accept.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, task.chapter_id)).content == "原文"
    detail = await guest.get(f"/api/v1/tasks/{identity['task_id']}",
                             params={"task_key": identity["task_key"]})
    assert detail.status_code == (200 if change == "downgrade" else 404), detail.text


# ---------------- 不误伤 ----------------


async def test_author_and_still_valid_editor_paused_tasks_are_untouched(app, client, make_client, fake_llm):
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    async with app.state.sessionmaker() as db:
        # 作者自己的待审核任务（无协作轮次），以及带有伪造/旧轮次的作者任务，都不属于协作收尾对象。
        own = Task(user_id=world.owner["id"], project_id=world.project["id"], kind="outline.continue",
                   status="paused", input={})
        forged = Task(user_id=world.owner["id"], project_id=world.project["id"], kind="outline.continue",
                      status="paused", input={"_collaboration_grant": {"invitation_id": str(uuid4()), "revision": 1}})
        db.add_all([own, forged])
        await db.commit()
        own_id, forged_id = own.id, forged.id
    # 授权未变化的通知：编辑者任务保持待审核。
    engine.notify_collaboration_change(world.project["id"], world.guest["id"])
    engine.notify_collaboration_change(world.project["id"], world.owner["id"])
    await _drain_checks(engine)
    assert await engine.scan_collaboration_paused() == 0
    for task_id in (row.id, own_id, forged_id):
        assert (await _row(app, task_id)).status == "paused"
    # 关闭作者模型授权对使用本人模型的任务没有影响（revision 不变的直接改库）。
    await _change(app, client, world, "owner_ai_direct")
    assert await engine.scan_collaboration_paused() == 0
    assert (await _row(app, row.id)).status == "paused"
    # 撤销编辑者只影响其本人任务。
    await _change(app, client, world, "revoked")
    await _drain_checks(engine)
    assert (await _row(app, row.id)).status == "failed"
    assert (await _row(app, own_id)).status == "paused" and (await _row(app, forged_id)).status == "paused"


@pytest.mark.parametrize("barrier", ["admin_intent", "admin_requested", "current", "receipt", "chapter_claim"])
async def test_paused_cleanup_keeps_barrier_when_not_provable(app, client, make_client, fake_llm, barrier, caplog,
                                                                monkeypatch):
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    if barrier.startswith("admin_"):
        # 伪造的管理员意图会被调度器的管理员取消排空按其自身路径处理（合法地记为 cancelled）；
        # 本用例只验证协作收尾不越过该屏障，故关闭排空以免与断言竞争。
        async def no_admin_drain():
            return None

        monkeypatch.setattr(engine, "scan_admin_cancellations", no_admin_drain)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, row.id)
        if barrier == "admin_intent":
            task.admin_cancel_operation_id = str(uuid4())
        elif barrier == "admin_requested":
            task.admin_cancel_requested_at = task.queued_at
        elif barrier == "current":
            task.checkpoint = {**task.checkpoint, "current": {"chapter_id": world.chapters[1]["id"]}}
        elif barrier == "receipt":
            task.checkpoint = {**task.checkpoint, "_batch_outcome_unknown": True}
        else:
            await db.execute(update(Chapter).where(Chapter.id == world.chapters[1]["id"]).values(
                status="generating", generation_claim_key=str(uuid4()), generation_claim_task_key=row.statistics_key,
                generation_claim_prev_status="draft",
            ))
        await db.commit()
        before = (task.status, task.error, task.checkpoint)
    await _change(app, client, world, "viewer")
    await _drain_checks(engine)
    assert await engine.scan_collaboration_paused() == 0
    after = await _row(app, row.id)
    assert (after.status, after.error, after.checkpoint) == before
    assert len(fake_llm.calls) == 1


async def test_round_changed_between_read_and_lock_is_not_touched(app, client, make_client, fake_llm):
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    await engine.stop()  # 不经通知/扫描，直接调用锁内收尾
    engine._stopping = False
    await _change_without_notification(app, world, "viewer")
    grant = task_grant_identity(row.input)
    stale_round = row.queued_at - timedelta(microseconds=1)
    assert not await engine._fail_collaboration_paused(
        row.id, row.statistics_key, stale_round, world.guest["id"], world.project["id"], grant,
    )
    assert not await engine._fail_collaboration_paused(
        row.id, str(uuid4()), row.queued_at, world.guest["id"], world.project["id"], grant,
    )
    assert not await engine._fail_collaboration_paused(
        row.id, row.statistics_key, row.queued_at, world.guest["id"], world.project["id"], (str(uuid4()), 2),
    )
    assert (await _row(app, row.id)).status == "paused"
    assert await engine._fail_collaboration_paused(
        row.id, row.statistics_key, row.queued_at, world.guest["id"], world.project["id"], grant,
    )
    assert (await _row(app, row.id)).status == "failed"


# ---------------- 本人操作与系统收尾的先后（SQLite 顺序版；PG 并发见 _postgresql 文件） ----------------


async def test_resume_before_revocation_is_failed_by_claim_not_paused_cleanup(app, client, make_client, fake_llm):
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    await engine.stop()
    resumed = await guest.post(f"/api/v1/tasks/{row.id}/resume", params=task_params((row.id, row.statistics_key)))
    assert resumed.status_code == 202, resumed.text
    await _change(app, client, world, "viewer")
    await engine.start()
    failed = await _wait_status(app, row.id, {"failed", "succeeded"})
    assert failed.status == "failed" and failed.error == MESSAGES["collaboration_revoked"]
    assert len(fake_llm.calls) == 1
    await _assert_untouched(app, world.chapters[1]["id"])
    await _author_can_start_batch(client, world)


async def test_finish_before_revocation_keeps_succeeded(app, client, make_client, fake_llm):
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    finished = await guest.post(f"/api/v1/tasks/{row.id}/finish", params=task_params((row.id, row.statistics_key)))
    assert finished.status_code == 200 and finished.json()["status"] == "succeeded", finished.text
    assert (await _row(app, row.id)).result["ended_early"] is True
    await _change(app, client, world, "revoked")
    await _drain_checks(engine)
    assert await engine.scan_collaboration_paused() == 0
    done = await _row(app, row.id)
    assert done.status == "succeeded" and done.error == ""
    await _author_can_start_batch(client, world)


# ---------------- 全量复查代价：键集分页与类型白名单 ----------------


async def test_full_scan_pages_through_candidates_and_skips_non_cleanable_kinds(
    app, client, make_client, fake_llm, monkeypatch,
):
    """分页大小小于候选数时仍完整收尾；白名单外的失效待审核任务不进入候选，不会每轮反复取写锁。"""
    guest = make_client()
    world, row = await _paused_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    engine.collaboration_scan_batch = 2
    grant = row.input["_collaboration_grant"]
    payload = {"request": {"project_id": world.project["id"]}, "_task_actor_token_version": 0,
               "_collaboration_grant": grant}
    async with app.state.sessionmaker() as db:
        extra = [Task(user_id=world.guest["id"], project_id=world.project["id"], kind="outline.continue",
                      status="paused", input=dict(payload), result={"paused": True}) for _ in range(4)]
        # 拆书仍不在可收尾白名单（agent.turn 自 E4i-e 起纳入，见 test_agent_revocation_e4ie.py）。
        foreign = Task(user_id=world.guest["id"], project_id=world.project["id"], kind="book.import",
                       status="paused", input=dict(payload))
        db.add_all([*extra, foreign])
        await db.commit()
        extra_ids, foreign_id = [task.id for task in extra], foreign.id
    attempted: list[int] = []
    original = engine._fail_collaboration_paused

    async def spy(task_id, *args, **kwargs):
        attempted.append(task_id)
        return await original(task_id, *args, **kwargs)

    monkeypatch.setattr(engine, "_fail_collaboration_paused", spy)
    await _change_without_notification(app, world, "viewer")
    assert await engine.scan_collaboration_paused() == 5
    assert sorted(attempted) == sorted([row.id, *extra_ids])
    for task_id in [row.id, *extra_ids]:
        assert (await _row(app, task_id)).status == "failed"
    assert (await _row(app, foreign_id)).status == "paused"
    attempted.clear()
    assert await engine.scan_collaboration_paused() == 0
    assert attempted == []  # 已收尾与不可收尾类型都不再取写锁
