"""旧客户端保留原任务身份，数字 ID 复用时不能读取或更改新任务。"""

from uuid import UUID, uuid4

import pytest

from app.models import Task
from app.schemas.task import TaskOut
from tests.helpers import create_project, parse_sse, register


@pytest.mark.parametrize(
    ("method", "suffix", "replacement_status", "kind"),
    [
        ("GET", "", "succeeded", "chapter.generate"),
        ("GET", "/output", "succeeded", "chapter.generate"),
        ("GET", "/stream", "succeeded", "chapter.generate"),
        ("POST", "/cancel", "paused", "chapter.generate"),
        ("POST", "/retry", "failed", "chapter.generate"),
        ("POST", "/resume", "paused", "chapter.generate"),
        ("POST", "/finish", "paused", "chapter.generate"),
        ("DELETE", "", "succeeded", "chapter.generate"),
        ("POST", "/skip", "paused", "book.import"),
    ],
)
async def test_stale_client_key_cannot_access_reused_id(app, client, method, suffix, replacement_status, kind):
    owner = await register(client, "stale-task-client")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        old = Task(user_id=owner["id"], project_id=project["id"], kind=kind, status="succeeded")
        db.add(old)
        await db.commit()
        task_id, stored_key = old.id, old.statistics_key

    # 模拟客户端从本人任务列表保存的身份，不在请求时按整数 ID 查找新身份。
    initial = await client.get("/api/v1/tasks")
    assert initial.status_code == 200, initial.text
    old_view = next(item for item in initial.json() if item["id"] == task_id)
    old_key = old_view["task_key"]
    assert old_key == stored_key
    assert str(UUID(old_key)) == old_key

    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(Task, task_id))
        await db.flush()
        replacement = Task(
            id=task_id, user_id=owner["id"], project_id=project["id"], kind=kind,
            status=replacement_status, output_text="不属于旧任务的新正文",
        )
        db.add(replacement)
        await db.commit()
        replacement_key = replacement.statistics_key
    assert replacement_key != old_key

    prefix = "/api/v1/book-imports/tasks" if suffix == "/skip" else "/api/v1/tasks"
    response = await client.request(method, f"{prefix}/{task_id}{suffix}", params={"task_key": old_key})
    assert response.status_code == 404, response.text
    assert "不属于旧任务的新正文" not in response.text
    async with app.state.sessionmaker() as db:
        replacement = await db.get(Task, task_id)
        assert replacement is not None
        assert replacement.statistics_key == replacement_key
        assert replacement.status == replacement_status
        assert replacement.output_text == "不属于旧任务的新正文"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/tasks/1"),
        ("GET", "/api/v1/tasks/1/output"),
        ("GET", "/api/v1/tasks/1/stream"),
        ("POST", "/api/v1/tasks/1/cancel"),
        ("POST", "/api/v1/tasks/1/retry"),
        ("POST", "/api/v1/tasks/1/resume"),
        ("POST", "/api/v1/tasks/1/finish"),
        ("DELETE", "/api/v1/tasks/1"),
        ("POST", "/api/v1/book-imports/tasks/1/skip"),
    ],
)
async def test_task_identity_is_required(app, client, method, path):
    await register(client, "missing-task-key")
    response = await client.request(method, path)
    assert response.status_code == 422, response.text


async def test_owner_reads_and_streams_with_original_task_key(app, client):
    owner = await register(client, "task-key-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        task = Task(
            user_id=owner["id"], project_id=project["id"], kind="chapter.generate",
            status="succeeded", output_text="本人任务正文",
        )
        db.add(task)
        await db.commit()
        task_id, stored_key = task.id, task.statistics_key

    initial = await client.get("/api/v1/tasks")
    assert initial.status_code == 200, initial.text
    item = next(item for item in initial.json() if item["id"] == task_id)
    key = item["task_key"]
    assert key == stored_key
    params = {"task_key": key}
    detail = await client.get(f"/api/v1/tasks/{task_id}", params=params)
    output = await client.get(f"/api/v1/tasks/{task_id}/output", params=params)
    stream = await client.get(f"/api/v1/tasks/{task_id}/stream", params=params)
    assert detail.status_code == 200, detail.text
    assert detail.json()["task_key"] == key
    assert output.status_code == 200, output.text
    assert output.json()["text"] == "本人任务正文"
    assert stream.status_code == 200, stream.text
    task_frames = [data["task"] for _, data in parse_sse(stream.text) if isinstance(data.get("task"), dict)]
    assert task_frames
    assert all(frame["id"] == task_id and frame["task_key"] == key for frame in task_frames)
    assert "本人任务正文" in stream.text


async def test_another_user_cannot_read_with_valid_task_key(app, client, make_client):
    owner = await register(client, "task-key-private-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.generate", status="succeeded")
        db.add(task)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key

    stranger = make_client()
    await register(stranger, "task-key-stranger")
    response = await stranger.get(f"/api/v1/tasks/{task_id}", params={"task_key": task_key})
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("action", ["cancel", "finish"])
async def test_review_action_rejects_conflicting_task_keys(app, client, action):
    owner = await register(client, f"mismatched-review-{action}")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.generate", status="paused")
        db.add(task)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key

    body = {"task_key": str(uuid4()), "run_key": str(uuid4()), "token": str(uuid4())}
    response = await client.post(f"/api/v1/tasks/{task_id}/{action}", params={"task_key": task_key}, json=body)
    assert response.status_code == 404, response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).status == "paused"


async def test_delete_basic_key_and_optional_review_identity(app, client):
    owner = await register(client, "delete-task-identity")
    project = await create_project(client)
    run_key, token = str(uuid4()), str(uuid4())
    async with app.state.sessionmaker() as db:
        ordinary = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.generate", status="succeeded")
        reviewed = Task(
            user_id=owner["id"], project_id=project["id"], kind="chapter.generate", status="succeeded",
            checkpoint={"rich_review": {"phase": "applied", "run_key": run_key, "token": token, "chapter_id": 1}},
        )
        db.add_all([ordinary, reviewed])
        await db.commit()
        ordinary_id, reviewed_id = ordinary.id, reviewed.id

    initial = await client.get("/api/v1/tasks")
    assert initial.status_code == 200, initial.text
    tasks = {item["id"]: item for item in initial.json()}
    ordinary_key = tasks[ordinary_id]["task_key"]
    reviewed_key = tasks[reviewed_id]["task_key"]
    assert tasks[reviewed_id]["rich_review"]["task_key"] == reviewed_key
    url = f"/api/v1/tasks/{reviewed_id}"
    missing_review = await client.delete(url, params={"task_key": reviewed_key})
    assert missing_review.status_code == 409, missing_review.text
    incomplete_review = await client.delete(url, params={"task_key": reviewed_key, "run_key": run_key})
    assert incomplete_review.status_code == 400, incomplete_review.text
    malformed_key = await client.delete(url, params={"task_key": "invalid", "run_key": run_key})
    assert malformed_key.status_code == 404, malformed_key.text
    reviewed_delete = await client.delete(url, params={"task_key": reviewed_key, "run_key": run_key, "token": token})
    assert reviewed_delete.status_code == 204, reviewed_delete.text
    ordinary_delete = await client.delete(f"/api/v1/tasks/{ordinary_id}", params={"task_key": ordinary_key})
    assert ordinary_delete.status_code == 204, ordinary_delete.text


async def test_taskout_preserves_unproven_historical_null(app, client):
    owner = await register(client, "historical-null-key")
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], kind="chapter.generate", status="succeeded")
        db.add(task)
        await db.commit()
        task.statistics_key = None
        await db.commit()
        output = TaskOut.model_validate(task).model_dump(mode="json")
    assert "task_key" in output
    assert output["task_key"] is None
    assert "statistics_key" not in output
