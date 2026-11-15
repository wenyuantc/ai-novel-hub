"""真实 PostgreSQL 两连接验证已完成章节收据与整数 ID 复用。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update

from app.models import ChapterVersion, Task
from app.services import generation_service, task_service
from tests.test_chapter_terminal_receipt_identity import _completed
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_rewrite_identity import _http_clients
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_terminal_apps = _postgres_fixture


@pytest.mark.parametrize("phase", ["before_lock", "after_proof"])
async def test_terminal_delete_cannot_touch_reused_task(postgres_terminal_apps, fake_llm, monkeypatch, phase):
    first_app, second_app = postgres_terminal_apps
    await first_app.state.task_engine.start()
    reached, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original_get = task_service.get_owned_task
    original_proof = generation_service.completed_terminal_chapter_receipt
    blocked = False

    async def gated_get(*args, **kwargs):
        nonlocal blocked
        task = await original_get(*args, **kwargs)
        if not blocked:
            blocked = True
            reached.set()
            await release.wait()
        return task

    async def gated_proof(db, task):
        receipt = await original_proof(db, task)
        assert receipt is not None
        pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        reached.set()
        await release.wait()
        return receipt

    try:
        async with _http_clients(first_app) as (owner, _unused):
            first_app.state.provider_factory = fake_llm.factory
            user, _project, _chapter, task = await _completed(first_app, owner, fake_llm)
            if phase == "before_lock":
                monkeypatch.setattr(task_service, "get_owned_task", gated_get)
            else:
                monkeypatch.setattr(generation_service, "completed_terminal_chapter_receipt", gated_proof)
            replacement_key = str(uuid4())
            pending = asyncio.create_task(owner.delete(
                f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]},
            ))

            async def replace():
                async with second_app.state.sessionmaker() as db:
                    await db.execute(delete(Task).where(Task.id == task["id"]))
                    db.add(Task(id=task["id"], statistics_key=replacement_key, user_id=user["id"],
                                kind="book.import", status="succeeded", title="同 ID 新任务"))
                    await db.commit()

            replacement = None
            try:
                await asyncio.wait_for(reached.wait(), 15)
                replacement = asyncio.create_task(replace())
                if phase == "after_proof":
                    await asyncio.wait_for(_wait_for_blocker(
                        second_app.state.sessionmaker, await asyncio.wait_for(pid, 10),
                    ), 10)
                    assert not replacement.done()
                    release.set()
                    deleted = await asyncio.wait_for(pending, 15)
                    assert deleted.status_code == 204, deleted.text
                    await asyncio.wait_for(replacement, 15)
                else:
                    await asyncio.wait_for(replacement, 15)
                    release.set()
                    deleted = await asyncio.wait_for(pending, 15)
                    assert deleted.status_code == 404, deleted.text
                async with second_app.state.sessionmaker() as db:
                    current = await db.get(Task, task["id"])
                    assert current.statistics_key == replacement_key and current.title == "同 ID 新任务"
            finally:
                release.set()
                for running in (pending, replacement):
                    if running is not None and not running.done():
                        running.cancel()
                for running in (pending, replacement):
                    if running is not None:
                        with suppress(asyncio.CancelledError):
                            await running
    finally:
        await first_app.state.task_engine.stop()


@pytest.mark.parametrize("phase", ["before_lock", "after_proof"])
async def test_terminal_delete_serializes_with_version_change(postgres_terminal_apps, fake_llm, monkeypatch, phase):
    first_app, second_app = postgres_terminal_apps
    await first_app.state.task_engine.start()
    reached, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original_get = task_service.get_owned_task
    original_proof = generation_service.completed_terminal_chapter_receipt
    blocked = False

    async def gated_get(*args, **kwargs):
        nonlocal blocked
        task = await original_get(*args, **kwargs)
        if not blocked:
            blocked = True
            reached.set()
            await release.wait()
        return task

    async def gated_proof(db, task):
        receipt = await original_proof(db, task)
        assert receipt is not None
        pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        reached.set()
        await release.wait()
        return receipt

    try:
        async with _http_clients(first_app) as (owner, _unused):
            first_app.state.provider_factory = fake_llm.factory
            _user, _project, _chapter, task = await _completed(first_app, owner, fake_llm)
            async with first_app.state.sessionmaker() as db:
                version_id = (await db.get(Task, task["id"])).checkpoint["_chapter_claim"]["version_id"]
            if phase == "before_lock":
                monkeypatch.setattr(task_service, "get_owned_task", gated_get)
            else:
                monkeypatch.setattr(generation_service, "completed_terminal_chapter_receipt", gated_proof)
            pending = asyncio.create_task(owner.delete(
                f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]},
            ))

            async def change_version():
                async with second_app.state.sessionmaker() as db:
                    await db.execute(update(ChapterVersion).where(ChapterVersion.id == version_id)
                                     .values(content="竞争的不同版本正文"))
                    await db.commit()

            changing = None
            try:
                await asyncio.wait_for(reached.wait(), 15)
                changing = asyncio.create_task(change_version())
                if phase == "after_proof":
                    await asyncio.wait_for(_wait_for_blocker(
                        second_app.state.sessionmaker, await asyncio.wait_for(pid, 10),
                    ), 10)
                    assert not changing.done()
                    release.set()
                    deleted = await asyncio.wait_for(pending, 15)
                    assert deleted.status_code == 204, deleted.text
                    await asyncio.wait_for(changing, 15)
                else:
                    await asyncio.wait_for(changing, 15)
                    release.set()
                    deleted = await asyncio.wait_for(pending, 15)
                    assert deleted.status_code == 409, deleted.text
                async with second_app.state.sessionmaker() as db:
                    current = await db.get(Task, task["id"])
                    assert (current is None) == (phase == "after_proof")
                    assert (await db.get(ChapterVersion, version_id)).content == "竞争的不同版本正文"
            finally:
                release.set()
                for running in (pending, changing):
                    if running is not None and not running.done():
                        running.cancel()
                for running in (pending, changing):
                    if running is not None:
                        with suppress(asyncio.CancelledError):
                            await running
    finally:
        await first_app.state.task_engine.stop()
