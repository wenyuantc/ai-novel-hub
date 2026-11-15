"""E4-f PostgreSQL：协作者章节任务的最终保存与撤权两种提交顺序（双实例，无本进程通知）。

执行引擎只在第二个实例运行，撤权经第一个实例提交，只能依赖数据库锁与锁内重验。
"""

import asyncio

import pytest
from sqlalchemy import func, select, update

from app.models import Chapter, GenerationLog, Task, User
from app.services import generation_service, version_service
from app.services.task_engine import engine as engine_module
from tests.conftest import _new_client
from tests.helpers import wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_chapter_entry_e4f import _assert_untouched, _change, _terminal, _world
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

RECEIPT_NOTE = engine_module.RECEIPT_FINALIZED_NOTE

postgres_e4f_apps = _postgres_fixture


def _allow_private(*apps):
    for app in apps:
        app.state.settings.allow_private_ai_endpoints = True


async def test_pg_save_commits_first_then_revoke_keeps_result(postgres_e4f_apps, fake_llm, monkeypatch):
    first_app, second_app = postgres_e4f_apps
    _allow_private(first_app, second_app)
    holder = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    original = version_service.ensure_baseline

    async def hold_inside_save(db, chapter):
        if not holder.done():
            holder.set_result(await db.scalar(select(func.pg_backend_pid())))
            await release.wait()
        return await original(db, chapter)

    monkeypatch.setattr(version_service, "ensure_baseline", hold_inside_save)
    revoke = None
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            fake_llm.push("保存先提交的正文")
            created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            pid = await asyncio.wait_for(holder, 15)
            # 保存事务已持有 User/门禁/作品/Task 锁：撤权必须等待它提交。
            revoke = asyncio.create_task(_change(first_app, owner, world, "revoked"))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, pid), 15)
            assert not revoke.done()
            release.set()
            await asyncio.wait_for(revoke, 15)
            task_id = created.json()["id"]
            engine = second_app.state.task_engine
            await wait_until(lambda: task_id not in engine._running, within=15)
            async with first_app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
                # 保存先提交：正文、版本、本人成功日志保留，不因随后撤权回滚。
                chapter = await db.get(Chapter, world.chapter["id"])
                assert chapter.content == "保存先提交的正文" and chapter.status == "done"
                assert chapter.generation_claim_key is None
                log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task_id))
                assert (log.status, log.actor_user_id) == ("success", world.guest["id"])
                assert row.user_id == world.guest["id"]
                # 撤权与 done 核验的先后不定：done 先核验 → 普通成功；撤权先提交 → 凭完成收据受限收尾。
                # 两种都必须是成功终态并释放执行屏障，绝不能停在 running 永久占用章节。
                assert row.status == "succeeded" and row.execution_key is None, row.error
                assert row.error in ("", RECEIPT_NOTE), row.error
                assert row.checkpoint["_chapter_claim"]["phase"] == "completed"
            # 撤权之后不能再发起新的章节任务。
            again = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/polish", json={})
            assert again.status_code == 404, again.text
    finally:
        release.set()
        if revoke is not None and not revoke.done():
            revoke.cancel()
        await second_app.state.task_engine.stop()


@pytest.mark.parametrize("change", ["revoked", "viewer"])
async def test_pg_revoke_commits_first_then_save_fails_closed(postgres_e4f_apps, fake_llm, monkeypatch, change):
    first_app, second_app = postgres_e4f_apps
    _allow_private(first_app, second_app)
    reached, release = asyncio.Event(), asyncio.Event()
    original = generation_service._save_chapter_result

    async def wait_before_save(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(generation_service, "_save_chapter_result", wait_before_save)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            fake_llm.push("撤权后不能保存的正文")
            created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            await asyncio.wait_for(reached.wait(), 15)
            # 模型输出已产出、保存尚未取锁：撤权在另一实例完整提交。
            await asyncio.wait_for(_change(first_app, owner, world, change), 15)
            release.set()
            row = await _terminal(second_app, created.json()["id"], within=15)
            assert row.status == "failed" and row.result is None, row.error
            assert len(fake_llm.calls) == 1
            await _assert_untouched(first_app, world)
            async with first_app.state.sessionmaker() as db:
                assert await db.scalar(select(GenerationLog.id).where(GenerationLog.task_id == row.id)) is None
    finally:
        release.set()
        await second_app.state.task_engine.stop()


async def _bump_token(app, user_id):
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1))
        await db.commit()


@pytest.mark.parametrize("change", ["revoked", "viewer", "author_token", "recover"])
async def test_pg_change_between_commit_and_done_completes_from_receipt(postgres_e4f_apps, fake_llm, monkeypatch,
                                                                       change):
    """确定性覆盖：保存已提交、done 核验取锁之前，授权变化在另一实例完整提交。

    原执行（第二实例，无本进程通知）done 核验失败，必须凭持久完成收据受限收尾为成功并释放活动资源；
    recover 变体模拟进程内收尾不可用，由重启恢复按同一收据收尾。
    """
    first_app, second_app = postgres_e4f_apps
    _allow_private(first_app, second_app)
    engine_cls = type(second_app.state.task_engine)
    original = engine_cls._event_task
    real_completion = engine_cls._complete_from_chapter_receipt
    fired = []
    owner_client = []
    world_box = []

    async def change_now():
        owner, world = owner_client[0], world_box[0]
        if change == "author_token":
            await _bump_token(first_app, world.owner["id"])
        else:
            await _change(first_app, owner, world, "viewer" if change == "viewer" else "revoked")

    async def wrapped(self, db, execution, current, event):
        if event == "done" and not fired:
            fired.append(event)
            work = asyncio.ensure_future(change_now())
            await asyncio.shield(work)
        return await original(self, db, execution, current, event)

    monkeypatch.setattr(engine_cls, "_event_task", wrapped)
    if change == "recover":
        unavailable = []

        async def unavailable_completion(self, task, **kwargs):
            unavailable.append(task.id)
            return None

        monkeypatch.setattr(engine_cls, "_complete_from_chapter_receipt", unavailable_completion)
    try:
        async with _new_client(first_app) as owner, _new_client(first_app) as guest:
            world = await _world(owner, guest)
            owner_client.append(owner)
            world_box.append(world)
            fake_llm.push("提交后才变化的正文")
            starter = owner if change == "author_token" else guest
            created = await starter.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
            assert created.status_code == 202, created.text
            task_id = created.json()["id"]
            engine = second_app.state.task_engine
            await engine.start()
            if change == "recover":
                await wait_until(lambda: bool(unavailable) and task_id not in engine._running, within=15)
                async with first_app.state.sessionmaker() as db:
                    assert (await db.get(Task, task_id)).status == "running"
                monkeypatch.setattr(engine_cls, "_complete_from_chapter_receipt", real_completion)
                await engine.stop()
                await first_app.state.task_engine.recover()
            row = await _terminal(first_app, task_id, within=15)
            assert fired == ["done"]
            assert row.status == "succeeded" and row.error == RECEIPT_NOTE, row.error
            assert row.checkpoint["_chapter_claim"]["phase"] == "completed"
            if change == "recover":
                assert row.result is None
            else:
                assert row.result["chapter"]["content"] == "提交后才变化的正文"
            assert len(fake_llm.calls) == 1
            async with first_app.state.sessionmaker() as db:
                chapter = await db.get(Chapter, world.chapter["id"])
                assert chapter.content == "提交后才变化的正文" and chapter.status == "done"
                assert chapter.generation_claim_key is None
                assert await db.scalar(select(func.count(GenerationLog.id)).where(
                    GenerationLog.task_id == task_id, GenerationLog.status == "success")) == 1
                assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 0
            # 章节活动资源已释放：作者（若改密则需新会话，这里只验证资源键不再被占用）可再发起。
            async with first_app.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(Task.id)).where(
                    Task.resource_key == f"chapter:{world.chapter['id']}",
                    Task.status.in_(("queued", "running")))) == 0
    finally:
        await second_app.state.task_engine.stop()
