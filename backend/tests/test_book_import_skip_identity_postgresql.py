"""PostgreSQL 独立连接验证拆书跳过的任务身份屏障。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest

from app.models import Task
from app.services import task_service
from app.services.book_import import reverse
from tests.helpers import register
from tests.test_book_import_skip_identity import _paused_task
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_skip_apps = _postgres_fixture


@pytest.mark.parametrize("phase", ["initial_read", "locked_before_cas"])
async def test_skip_keeps_original_uuid_across_postgresql_connections(postgres_skip_apps, monkeypatch, phase):
    first_app, second_app = postgres_skip_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=first_app), base_url="http://testserver") as client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver") as other,
    ):
        user, project, task_id, old_key, task_input, cp = await _paused_task(first_app, client)
        other_user = await register(other, "replacement_user")
        reached, release = asyncio.Event(), asyncio.Event()
        if phase == "initial_read":
            original = task_service.get_owned_task

            async def gated_get(*args, **kwargs):
                task = await original(*args, **kwargs)
                reached.set()
                await release.wait()
                return task

            monkeypatch.setattr(task_service, "get_owned_task", gated_get)
        else:
            original = reverse.revalidate

            async def gated_revalidate(*args, **kwargs):
                reached.set()
                await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(reverse, "revalidate", gated_revalidate)
        pending = asyncio.create_task(
            client.post(f"/api/v1/book-imports/tasks/{task_id}/skip", params={"task_key": old_key})
        )
        new_key = str(uuid4())

        async def replace():
            async with second_app.state.sessionmaker() as db:
                old = await db.get(Task, task_id)
                await db.delete(old)
                await db.flush()
                # 锁后阶段使用另一用户的个人任务，避免等待已持有的原用户/作品父锁。
                replacement = Task(
                    id=task_id, statistics_key=new_key,
                    user_id=user["id"] if phase == "initial_read" else other_user["id"],
                    project_id=project["id"] if phase == "initial_read" else None,
                    kind=reverse.KIND, status="paused", input=task_input, checkpoint=cp,
                )
                db.add(replacement)
                await db.commit()

        try:
            await asyncio.wait_for(reached.wait(), 10)
            await asyncio.wait_for(replace(), 10)
            release.set()
            response = await asyncio.wait_for(pending, 10)
            assert response.status_code == (404 if phase == "initial_read" else 409), response.text
            async with second_app.state.sessionmaker() as db:
                current = await db.get(Task, task_id)
                assert current.statistics_key == new_key
                assert current.status == "paused" and current.step_cursor == 0
                assert current.checkpoint == cp
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
