"""一致性检查 PostgreSQL：协作者检查的落库事务与撤权两种提交顺序（双实例）。

执行引擎只在第二个实例运行，撤权经第一个实例提交，只能依赖数据库锁与锁内重验（无本进程通知）：
- 合并事务先持锁（锁内重验已通过）：撤权被阻塞至提交，问题与台账保留；
- 撤权先提交：台账落库事务锁内重验失败，本章结果不保存、不再发送后续请求，任务失败关闭并释放资源键。

需要 AINOVEL_COLLAB_POSTGRES_TEST_URL（经 postgres_collaboration_apps 夹具）；未配置时由夹具跳过。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import ConsistencyIssue, ConsistencyLedger, ConsistencyRun, Task
from app.services.consistency import check
from tests.conftest import _new_client
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_outline_entry_e4g3 import EDITOR_KEY, STOPPED, _change, _terminal, _world
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_consistency_collaboration import BROKEN, USED, _chapters, _finding, _start, _sword

postgres_consistency_apps = _postgres_fixture


def _allow_private(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True
        app.state.settings.auto_analyze_enabled = False


async def _counts(app, project_id):
    async with app.state.sessionmaker() as db:
        ledgers = set(await db.scalars(select(ConsistencyLedger.chapter_id).where(
            ConsistencyLedger.project_id == project_id)))
        issues = await db.scalar(select(func.count(ConsistencyIssue.id)).where(
            ConsistencyIssue.project_id == project_id))
        active = await db.scalar(select(func.count(Task.id)).where(
            Task.resource_key == check.resource_key(project_id), Task.status.in_(("queued", "running"))))
        runs = list(await db.scalars(select(ConsistencyRun.status).where(ConsistencyRun.project_id == project_id)))
        return ledgers, issues, active, runs


async def test_pg_merge_commits_first_then_revoke_keeps_issues(postgres_consistency_apps, fake_llm, monkeypatch):
    first_app, second_app = postgres_consistency_apps
    _allow_private(first_app, second_app)
    original = check.issues.merge_findings
    holder = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def hold_inside_merge(db, **kwargs):
        stats = await original(db, **kwargs)
        if not holder.done():
            # 锁内重验已通过，合并事务仍持有 认证 / 门禁 / 作品 / Task 锁：撤权必须等待其提交。
            holder.set_result(await db.scalar(select(func.pg_backend_pid())))
            await release.wait()
        return stats

    monkeypatch.setattr(check.issues, "merge_findings", hold_inside_merge)
    revoke = None
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            chapters = await _chapters(owner, world, {1: BROKEN, 2: USED})
            fake_llm.push(_sword("佩剑在山门前断成两截"))
            fake_llm.push(_sword("拔出佩剑"))
            fake_llm.push(_finding(chapters))
            created = await _start(guest, world)
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            pid = await asyncio.wait_for(holder, 15)
            revoke = asyncio.create_task(_change(first_app, owner, world, "revoked"))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, pid), 15)
            assert not revoke.done()
            release.set()
            await asyncio.wait_for(revoke, 15)
            row = await _terminal(first_app, created.json()["id"])
            # 合并已提交；之后的完成事件 / 终态核验因撤权失败时任务改记无结果失败，数据保留。
            assert row.status in ("succeeded", "failed"), row.error
            assert row.user_id == world.guest["id"]
            assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY] * 3
            ledgers, issues, active, runs = await _counts(first_app, world.project["id"])
            assert ledgers == {chapters[1]["id"], chapters[2]["id"]} and issues == 1
            assert active == 0 and runs == ["succeeded"]
    finally:
        release.set()
        if revoke is not None and not revoke.done():
            revoke.cancel()
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_pg_revoke_commits_first_then_ledger_save_fails_closed(postgres_consistency_apps, fake_llm,
                                                                     monkeypatch, change):
    first_app, second_app = postgres_consistency_apps
    _allow_private(first_app, second_app)
    original_complete = check.complete
    fired, boxes = [], {}

    async def complete_then_revoke(target, messages):
        # 模型已返回、台账落库事务开始前，撤权经另一实例完整提交（本实例收不到进程内通知）。
        text = await original_complete(target, messages)
        if not fired:
            fired.append(change)
            await asyncio.shield(asyncio.ensure_future(
                _change(first_app, boxes["owner"], boxes["world"], change),
            ))
        return text

    monkeypatch.setattr(check, "complete", complete_then_revoke)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
            boxes.update(owner=owner, world=world)
            await _chapters(owner, world, {1: BROKEN, 2: USED})
            fake_llm.push(_sword("佩剑在山门前断成两截"))
            created = await _start(guest, world)
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert fired == [change]
            assert row.status == "failed" and row.result is None, (row.status, row.error)
            assert row.error in STOPPED, row.error
            assert len(fake_llm.calls) == 1
            ledgers, issues, active, runs = await _counts(first_app, world.project["id"])
            assert (ledgers, issues, active) == (set(), 0, 0)
            assert all(status != "running" for status in runs)
    finally:
        await second_app.state.task_engine.stop()
