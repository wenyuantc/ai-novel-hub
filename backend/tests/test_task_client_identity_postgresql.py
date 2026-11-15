"""跨进程原任务身份：PostgreSQL 仅在专用隔离测试库中运行。"""

import httpx
import pytest

from app.models import Task
from tests.helpers import create_project, register
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as postgres_collaboration_apps


@pytest.mark.parametrize(
    ("method", "suffix", "status", "kind"),
    [
        ("GET", "/output", "succeeded", "chapter.generate"),
        ("GET", "/stream", "succeeded", "chapter.generate"),
        ("DELETE", "", "succeeded", "chapter.generate"),
        ("POST", "/skip", "paused", "book.import"),
    ],
)
async def test_postgres_reused_id_rejects_client_original_key(
    postgres_collaboration_apps, method, suffix, status, kind,
):
    reader_app, writer_app = postgres_collaboration_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reader_app), base_url="http://testserver") as client:
        owner = await register(client, "pg-original-task-client")
        project = await create_project(client)
        async with reader_app.state.sessionmaker() as db:
            old = Task(user_id=owner["id"], project_id=project["id"], kind=kind, status="succeeded")
            db.add(old)
            await db.commit()
            task_id = old.id

        response = await client.get("/api/v1/tasks")
        assert response.status_code == 200, response.text
        original = next(item for item in response.json() if item["id"] == task_id)
        original_key = original["task_key"]
        assert original_key

        # 在另一应用会话提交替代行，旧客户端继续持有首次列表响应里的 key。
        async with writer_app.state.sessionmaker() as db:
            await db.delete(await db.get(Task, task_id))
            await db.flush()
            new = Task(
                id=task_id, user_id=owner["id"], project_id=project["id"], kind=kind,
                status=status, output_text="新任务的私人正文",
            )
            db.add(new)
            await db.commit()
            new_key = new.statistics_key
        assert new_key != original_key

        prefix = "/api/v1/book-imports/tasks" if suffix == "/skip" else "/api/v1/tasks"
        response = await client.request(method, f"{prefix}/{task_id}{suffix}", params={"task_key": original_key})
        assert response.status_code == 404, response.text
        assert "新任务的私人正文" not in response.text
        async with writer_app.state.sessionmaker() as db:
            new = await db.get(Task, task_id)
            assert new is not None
            assert (new.statistics_key, new.status, new.output_text) == (new_key, status, "新任务的私人正文")
