"""AC4：单章与向导在接管中再次失联，资源释放、终态和统计必须一起落库。"""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Character, Task, TaskStatistic
from app.services import chapter_claim, wizard_service
from tests.helpers import wait_until
from tests.test_tasks import _generate, _setup
from tests.worker_helpers import expire_worker


class ProcessExit(BaseException):
    """模拟进程丢失控制权，包括提交已成功但执行者未收到任何确认。"""


async def _leave_running(app, client, monkeypatch, kind):
    project, base, (chapter,) = await _setup(client)
    created = await client.post(f"{base}/characters", json={"name": "原角色", "relations_text": "原始关系"})
    assert created.status_code == 201
    engine = app.state.task_engine
    prepared = asyncio.Event()

    async def stop_before_generation(task_id, hub, execution, **kwargs):
        if kind == "wizard":
            await wizard_service.begin_wizard_pending(engine.ctx.sessionmaker, execution)
        prepared.set()

    with monkeypatch.context() as patch:
        patch.setattr(engine, "_run", stop_before_generation)
        if kind == "chapter":
            response = await _generate(client, chapter)
        else:
            response = await client.post(f"{base}/wizard/characters", json={})
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        await asyncio.wait_for(prepared.wait(), timeout=10)
        await wait_until(lambda: task_id not in engine._running)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        assert (task.status, task.worker_key) == ("running", engine.lease.worker_key)
        assert task.execution_key and task.recovered_at is None
    # 旧执行者退出，任务留在上面真实领取时的执行轮次。
    await engine.stop()
    return task, project["id"], chapter["id"]


def _stop_recovery(patch, engine, kind, point, task_id):
    if point == "adopted":
        async def exit_after_adoption(*args, **kwargs):
            raise ProcessExit

        patch.setattr(engine, "_recover_adopted", exit_after_adoption)
    elif point == "released":
        service, name = ((chapter_claim, "release_chapter_claim") if kind == "chapter"
                         else (wizard_service, "release_uncommitted_pending"))
        release = getattr(service, name)

        async def exit_before_terminal(*args, **kwargs):
            assert await release(*args, **kwargs)
            raise ProcessExit

        patch.setattr(service, name, exit_before_terminal)
    elif point in ("committed", "lost_reply"):
        commit = AsyncSession.commit
        fired = []

        async def lose_terminal_confirmation(db):
            status = await db.scalar(select(Task.status).where(Task.id == task_id))
            await commit(db)
            if not fired and status == "interrupted":
                fired.append(True)
                if point == "lost_reply":
                    raise ConnectionError("终态已提交，连接在返回确认前中断")
                raise ProcessExit

        patch.setattr(AsyncSession, "commit", lose_terminal_confirmation)
        return fired


async def _assert_committed(maker, original, chapter_id, project_id):
    async with maker() as db:
        task = await db.get(Task, original.id)
        assert (task.status, task.execution_key, task.recovered_at) == ("interrupted", None, None)
        assert "_chapter_claim" not in task.input and "_wizard_pending" not in (task.checkpoint or {})
        chapter = await db.get(Chapter, chapter_id)
        assert (chapter.content, chapter.status, chapter.generation_claim_key,
                chapter.generation_claim_task_key, chapter.generation_claim_prev_status) == (
            "", "draft", None, None, None,
        )
        assert (await db.execute(select(Character.name, Character.relations_text).where(
            Character.project_id == project_id,
        ))).all() == [("原角色", "原始关系")]
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.key == original.statistics_key,
        )) == 1
        record = await db.get(TaskStatistic, original.statistics_key)
        assert record.status == "interrupted" and task.finished_at is not None
        assert record.finished_event_at == task.finished_at
        return record.finished_event_at


@pytest.mark.parametrize("kind", ["chapter", "wizard"])
@pytest.mark.parametrize("point", ["healthy", "adopted", "released", "committed", "lost_reply"])
async def test_recovery_is_atomic_after_adopter_exits(
    app, client, second_app, peer_app_factory, monkeypatch, kind, point,
):
    original, project_id, chapter_id = await _leave_running(app, client, monkeypatch, kind)
    second = second_app.state.task_engine
    third = (await peer_app_factory()).state.task_engine
    # 在此精确驱动两个独立引擎每次恢复，避免后台调度抢先消费故障现场。
    await second.lease.register()
    await third.lease.register()
    assert len({original.worker_key, second.lease.worker_key, third.lease.worker_key}) == 3
    if point == "healthy":
        await second.recover()
    else:
        with monkeypatch.context() as patch:
            fired = _stop_recovery(patch, second, kind, point, original.id)
            if point == "lost_reply":
                await second.recover()
                assert fired and not second._adopted
            else:
                with pytest.raises(ProcessExit):
                    await second.recover()

    maker = third.ctx.sessionmaker
    if point in ("adopted", "released"):
        # 认领已提交。资源清理事务中断后，数据库仍留着可用于下一次接管的原始证据。
        async with maker() as db:
            task = await db.get(Task, original.id)
            assert (task.status, task.worker_key, task.execution_key) == (
                "running", second.lease.worker_key, original.execution_key,
            )
            assert (await db.get(TaskStatistic, original.statistics_key)).finished_event_at is None
            if kind == "chapter":
                assert task.input["_chapter_claim"] == original.input["_chapter_claim"]
                chapter = await db.get(Chapter, chapter_id)
                assert chapter.status == "generating"
                assert chapter.generation_claim_key == task.input["_chapter_claim"]["claim_key"]
            else:
                assert task.checkpoint["_wizard_pending"] == original.checkpoint["_wizard_pending"]
    if point != "healthy":
        await expire_worker(second)
    if point in ("adopted", "released"):
        await third.recover()

    finished = await _assert_committed(maker, original, chapter_id, project_id)
    await third.recover()
    assert await _assert_committed(maker, original, chapter_id, project_id) == finished
    assert not second.has_cleanup_failure(project_id) and not third.has_cleanup_failure(project_id)
    retry = await client.post(f"/api/v1/tasks/{original.id}/retry", params={"task_key": original.statistics_key})
    assert retry.status_code == 202, retry.text
