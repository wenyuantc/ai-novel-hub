"""修改影响评估 PostgreSQL：协作者评估的落库事务与撤权的提交顺序（双实例）。

执行引擎只在第二个实例运行，撤权经第一个实例提交，只能依赖数据库锁与锁内重验（无本进程通知）：
撤权先提交 → 台账落库事务锁内重验失败，不保存台账与影响记录、不再发送判定请求，任务失败关闭并释放资源键。

需要 AINOVEL_COLLAB_POSTGRES_TEST_URL（经 postgres_collaboration_apps 夹具）；未配置时由夹具跳过。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import ConsistencyImpact, ConsistencyLedger, Task
from app.services.consistency import check, impact_task
from tests.conftest import _new_client
from tests.test_collaboration_outline_entry_e4g3 import EDITOR_KEY, STOPPED, _change, _terminal, _world
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_consistency_impact_collaboration import VERDICT, _assess, _prepare, _sword

postgres_impact_apps = _postgres_fixture


def _allow_private(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True
        app.state.settings.auto_analyze_enabled = False


async def _counts(app, project_id, chapter_id):
    async with app.state.sessionmaker() as db:
        impacts = await db.scalar(select(func.count(ConsistencyImpact.id)).where(
            ConsistencyImpact.project_id == project_id))
        ledger = await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == chapter_id))
        active = await db.scalar(select(func.count(Task.id)).where(
            Task.resource_key == check.resource_key(project_id), Task.status.in_(("queued", "running"))))
        return impacts, ledger, active


async def test_pg_editor_assessment_commits_ledger_and_impact(postgres_impact_apps, fake_llm):
    first_app, second_app = postgres_impact_apps
    _allow_private(first_app, second_app)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            chapters = await _prepare(first_app, owner, world)
            fake_llm.push(_sword())
            fake_llm.push(VERDICT)
            created = await _assess(guest, world, chapters[1])
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert row.status == "succeeded", row.error
            assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY] * 2
            impacts, ledger, active = await _counts(first_app, world.project["id"], chapters[1]["id"])
            assert (impacts, active) == (1, 0)
            assert ledger.prev_facts[0]["statement"] == "林远的佩剑完好"
            detail = (await owner.get(f"{world.base}/consistency/impacts/{chapters[1]['id']}")).json()
            assert [item["chapter_id"] for item in detail["assessment"]["items"]] == [chapters[2]["id"]]
    finally:
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_pg_revoke_commits_first_then_saves_fail_closed(postgres_impact_apps, fake_llm, monkeypatch, change):
    first_app, second_app = postgres_impact_apps
    _allow_private(first_app, second_app)
    original_complete = impact_task.complete
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

    monkeypatch.setattr(impact_task, "complete", complete_then_revoke)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
            boxes.update(owner=owner, world=world)
            chapters = await _prepare(first_app, owner, world)
            fake_llm.push(_sword())
            created = await _assess(guest, world, chapters[1])
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert fired == [change]
            assert row.status == "failed" and row.result is None, (row.status, row.error)
            assert row.error in STOPPED, row.error
            assert len(fake_llm.calls) == 1
            impacts, ledger, active = await _counts(first_app, world.project["id"], chapters[1]["id"])
            assert (impacts, active) == (0, 0)
            assert ledger.prev_facts is None and ledger.facts[0]["statement"] == "林远的佩剑完好"
    finally:
        await second_app.state.task_engine.stop()
