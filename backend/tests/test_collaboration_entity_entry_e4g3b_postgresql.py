"""E4-g3b PostgreSQL：协作者实体草稿的终态保存与撤权两种提交顺序（双实例）。

实体任务唯一的持久化是 Task 终态与草稿 result，在同一终态事务内按原 grant 锁内重验后写入（含实体来源快照的
行锁）。执行引擎只在第二个实例运行，撤权经第一个实例提交，只能依赖数据库锁与锁内重验（无本进程通知）：
- 终态事务先持锁：撤权被阻塞至提交，草稿结果保留（succeeded）；
- 撤权先提交：终态锁内重验失败，改记无结果失败，活动资源键（relparse:{cid}）释放，不停在 running。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import Task
from app.services.task_engine.registry import get_handler
from tests.conftest import _new_client
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_entity_entry_e4g3b import (
    EDITOR_KEY,
    OWNER_KEY,
    STOPPED,
    _answer,
    _change,
    _start,
    _terminal,
    _world,
)
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_e4g3b_apps = _postgres_fixture
KIND = "relations.parse"


def _allow_private(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True
        app.state.settings.auto_analyze_enabled = False


async def _active_parse_tasks(app, character_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count(Task.id)).where(
            Task.resource_key == f"relparse:{character_id}", Task.status.in_(("queued", "running")),
        ))


async def test_pg_terminal_save_commits_first_then_revoke_keeps_drafts(postgres_e4g3b_apps, fake_llm, monkeypatch):
    first_app, second_app = postgres_e4g3b_apps
    _allow_private(first_app, second_app)
    handler = get_handler(KIND)
    original = type(handler).verify_finalization
    holder = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def hold_inside_terminal(self, db, task, **kwargs):
        await original(self, db, task, **kwargs)
        if not holder.done():
            # 锁内重验已通过，终态事务仍持有 认证/门禁/作品/Task/来源 锁：撤权必须等待其提交。
            holder.set_result(await db.scalar(select(func.pg_backend_pid())))
            await release.wait()

    monkeypatch.setattr(type(handler), "verify_finalization", hold_inside_terminal)
    revoke = None
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            fake_llm.push(_answer(KIND))
            created = await _start(guest, world, KIND)
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            pid = await asyncio.wait_for(holder, 15)
            revoke = asyncio.create_task(_change(first_app, owner, world, "revoked"))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, pid), 15)
            assert not revoke.done()
            release.set()
            await asyncio.wait_for(revoke, 15)
            row = await _terminal(first_app, created.json()["id"])
            assert row.status == "succeeded", row.error
            assert row.result["character_id"] == world.chars[0]
            assert [s["target_name"] for s in row.result["suggestions"]] == ["乙"]
            assert row.user_id == world.guest["id"]
            assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY]
            assert await _active_parse_tasks(first_app, world.chars[0]) == 0
    finally:
        release.set()
        if revoke is not None and not revoke.done():
            revoke.cancel()
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_pg_revoke_commits_first_then_terminal_save_fails_closed(postgres_e4g3b_apps, fake_llm, monkeypatch,
                                                                       change):
    first_app, second_app = postgres_e4g3b_apps
    _allow_private(first_app, second_app)
    engine = second_app.state.task_engine
    engine_cls = type(engine)
    original = engine_cls.complete_state
    fired, boxes = [], {}

    async def revoke_before_terminal(self, task, **kwargs):
        # 完成事件已核验发布、终态事务开始前，撤权经另一实例完整提交。
        if (not fired and task is not None and task.kind == KIND
                and kwargs.get("status") == "succeeded"):
            fired.append(change)
            await asyncio.shield(asyncio.ensure_future(
                _change(first_app, boxes["owner"], boxes["world"], change),
            ))
        return await original(self, task, **kwargs)

    monkeypatch.setattr(engine_cls, "complete_state", revoke_before_terminal)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
            boxes.update(owner=owner, world=world)
            fake_llm.push(_answer(KIND))
            created = await _start(guest, world, KIND)
            assert created.status_code == 202, created.text
            await engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert fired == [change]
            assert row.status == "failed" and row.result is None and row.error in STOPPED, (row.status, row.error)
            assert [call["api_key"] for call in fake_llm.calls] == [
                OWNER_KEY if change == "owner_ai_direct" else EDITOR_KEY,
            ]
            assert await _active_parse_tasks(first_app, world.chars[0]) == 0
            assert not engine.has_cleanup_failure(world.project["id"])
            # 同一角色的解析资源键已释放：作者可立即发起并成功。
            fake_llm.push(_answer(KIND))
            author = await _start(owner, world, KIND)
            assert author.status_code == 202, author.text
            assert (await _terminal(first_app, author.json()["id"])).status == "succeeded"
    finally:
        await engine.stop()
