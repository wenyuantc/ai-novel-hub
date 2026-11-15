"""E4-g3c PostgreSQL：协作者开书向导的内容保存与撤权两种提交顺序（双实例）。

向导直接写库：内容替换与完成收据在同一保存事务内、按原 grant 锁内重验（require_write_execution）后提交。执行引擎
只在第二个实例运行，撤权经第一个实例提交，只能依赖数据库锁与锁内重验（无本进程通知）：
- 保存事务先持锁：撤权被阻塞至提交，协作者内容保留；之后的完成事件核验若已看到撤权，按本轮收据受限收尾为成功；
- 撤权先提交：保存锁内重验失败，内容与收据都不写入，未决凭据经系统清理，任务失败且向导资源键释放；
- 内容提交后、完成事件核验前撤权：按收据收尾为 succeeded（附固定说明），不停在 running。
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import Task
from app.services import wizard_service
from app.services.task_engine import engine as engine_module
from tests.conftest import _new_client
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_entity_entry_e4g3b import EDITOR_KEY, OWNER_KEY, STOPPED, _change, _terminal, _world
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_wizard_entry_e4g3c import ANSWERS, NEW_NAMES, _contents, _names, _start

postgres_e4g3c_apps = _postgres_fixture
KIND = "characters"
NOTE = engine_module.WIZARD_RECEIPT_FINALIZED_NOTE


def _allow_private(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True
        app.state.settings.auto_analyze_enabled = False


async def _active_wizard_tasks(app, project_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count(Task.id)).where(
            Task.resource_key == f"wizard:{project_id}:{KIND}", Task.status.in_(("queued", "running")),
        ))


async def test_pg_save_commits_first_then_revoke_keeps_content(postgres_e4g3c_apps, fake_llm, monkeypatch):
    first_app, second_app = postgres_e4g3c_apps
    _allow_private(first_app, second_app)
    original = wizard_service._require_safe_replacement
    holder = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    armed = []

    async def hold_inside_save(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        if armed and not holder.done():
            # 保存事务已按原 grant 锁内重验并持有 认证/门禁/作品/Task 锁：撤权必须等待其提交。
            holder.set_result(await db.scalar(select(func.pg_backend_pid())))
            await release.wait()
        return result

    monkeypatch.setattr(wizard_service, "_require_safe_replacement", hold_inside_save)
    revoke = None
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            fake_llm.push(ANSWERS[KIND])
            created = await _start(guest, world, KIND)
            assert created.status_code == 202, created.text
            armed.append(True)  # 只拦截执行体的保存事务，不拦截受理时的覆盖预检
            await second_app.state.task_engine.start()
            pid = await asyncio.wait_for(holder, 15)
            revoke = asyncio.create_task(_change(first_app, owner, world, "revoked"))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, pid), 15)
            assert not revoke.done()
            release.set()
            await asyncio.wait_for(revoke, 15)
            row = await _terminal(first_app, created.json()["id"])
            # 完成事件核验可能先于或晚于撤权提交：前者普通成功，后者按收据收尾；两者都不发布失权结果之外的副作用。
            assert row.status == "succeeded" and row.error in ("", NOTE), (row.status, row.error)
            assert row.result["kind"] == KIND and row.user_id == world.guest["id"]
            assert await _names(first_app, world, KIND) == sorted(NEW_NAMES[KIND])
            assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY]
            assert await _active_wizard_tasks(first_app, world.project["id"]) == 0
    finally:
        release.set()
        if revoke is not None and not revoke.done():
            revoke.cancel()
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_pg_revoke_commits_first_then_save_fails_closed(postgres_e4g3c_apps, fake_llm, monkeypatch, change):
    first_app, second_app = postgres_e4g3c_apps
    _allow_private(first_app, second_app)
    engine = second_app.state.task_engine
    original = wizard_service.save_wizard_result
    fired, boxes = [], {}

    async def revoke_before_save(*args, **kwargs):
        # 模型输出已完整发布、内容替换事务开始前，撤权经另一实例完整提交。
        if not fired:
            fired.append(change)
            await asyncio.shield(asyncio.ensure_future(_change(first_app, boxes["owner"], boxes["world"], change)))
        return await original(*args, **kwargs)

    monkeypatch.setattr(wizard_service, "save_wizard_result", revoke_before_save)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
            boxes.update(owner=owner, world=world)
            before = await _contents(first_app, world)
            fake_llm.push(ANSWERS[KIND])
            created = await _start(guest, world, KIND)
            assert created.status_code == 202, created.text
            await engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert fired == [change]
            assert row.status == "failed" and row.result is None and row.error in STOPPED, (row.status, row.error)
            assert not isinstance(row.checkpoint, dict) or not (
                {"_wizard_pending", "_wizard_completion"} & set(row.checkpoint)
            )
            assert [call["api_key"] for call in fake_llm.calls] == [
                OWNER_KEY if change == "owner_ai_direct" else EDITOR_KEY,
            ]
            assert await _contents(first_app, world) == before
            assert await _active_wizard_tasks(first_app, world.project["id"]) == 0
            assert not engine.has_cleanup_failure(world.project["id"])
            # 同一步骤的向导资源键已释放：作者可立即发起并成功。
            fake_llm.push(ANSWERS[KIND])
            author = await _start(owner, world, KIND)
            assert author.status_code == 202, author.text
            assert (await _terminal(first_app, author.json()["id"])).status == "succeeded"
    finally:
        await engine.stop()


async def test_pg_revoke_after_commit_before_done_finalizes_from_receipt(postgres_e4g3c_apps, fake_llm, monkeypatch):
    first_app, second_app = postgres_e4g3c_apps
    _allow_private(first_app, second_app)
    engine = second_app.state.task_engine
    engine_cls = type(engine)
    original = engine_cls._event_task
    fired, boxes = [], {}

    async def revoke_before_done(self, db, execution, current, event):
        if event == "done" and not fired:
            fired.append(event)
            await asyncio.shield(asyncio.ensure_future(
                _change(first_app, boxes["owner"], boxes["world"], "revoked"),
            ))
        return await original(self, db, execution, current, event)

    monkeypatch.setattr(engine_cls, "_event_task", revoke_before_done)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            boxes.update(owner=owner, world=world)
            fake_llm.push(ANSWERS[KIND])
            created = await _start(guest, world, KIND)
            assert created.status_code == 202, created.text
            await engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert fired == ["done"]
            assert (row.status, row.error, row.execution_key) == ("succeeded", NOTE, None)
            assert row.result == row.checkpoint["_wizard_completion"]["result"]
            assert await _names(first_app, world, KIND) == sorted(NEW_NAMES[KIND])
            assert await _active_wizard_tasks(first_app, world.project["id"]) == 0
            assert not engine.has_cleanup_failure(world.project["id"])
    finally:
        await engine.stop()
