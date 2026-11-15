"""拆书跳过始终绑定客户端看到的任务身份。"""

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models import Chapter, SceneReviewProposal, Task
from app.services import task_service
from app.services.book_import import reverse
from tests.helpers import create_ai_config, register
from tests.test_book_import import _start, upload


async def _paused_task(app, client):
    user = await register(client)
    await create_ai_config(client)
    await app.state.task_engine.stop()
    uploaded = await upload(client)
    assert uploaded.status_code == 201, uploaded.text
    accepted = await _start(client, uploaded.json(), import_range=[1, 1], reverse_range=[1, 1],
                            extract_style=False, deep_analyze=False)
    assert accepted.status_code == 202, accepted.text
    result = accepted.json()
    task_ref = result["task"]
    project_response = await client.get(f"/api/v1/projects/{result['project_id']}")
    assert project_response.status_code == 200, project_response.text
    project = project_response.json()
    chapters_response = await client.get(f"/api/v1/projects/{project['id']}/chapters")
    assert chapters_response.status_code == 200, chapters_response.text
    chapter = chapters_response.json()[0]
    cp = reverse.new_checkpoint([[[chapter["id"], 1, 1, chapter["chapter_no"]]]])
    cp["pause_reason"] = {"reason": "failed", "error": "测试失败"}
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, task_ref["id"])
        assert original.statistics_key == task_ref["task_key"] and original.user_id == user["id"]
        assert original.kind == reverse.KIND and original.project_id == project["id"] and original.status == "queued"
        task_input = deepcopy(original.input)
        assert type(task_input["_task_actor_token_version"]) is int
        assert task_input["_project_model"]["version"] == task_input["_shared_embedding"]["version"] == 2
        original.status, original.checkpoint, original.step_cursor = "paused", cp, 0
        await db.commit()
    return user, project, task_ref["id"], task_ref["task_key"], task_input, cp


async def _replace_task(app, user, project, task_id, task_input, cp):
    new_key = str(uuid4())
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, task_id)
        await db.delete(original)
        await db.flush()
        replacement = Task(id=task_id, statistics_key=new_key, user_id=user["id"], project_id=project["id"],
                           kind=reverse.KIND, status="paused", input=task_input, checkpoint=cp)
        db.add(replacement)
        await db.commit()
    return new_key


async def test_skip_rejects_reused_integer_task_id(app, client):
    user, project, task_id, old_key, task_input, cp = await _paused_task(app, client)
    new_key = await _replace_task(app, user, project, task_id, task_input, cp)

    response = await client.post(f"/api/v1/book-imports/tasks/{task_id}/skip", params={"task_key": old_key})
    assert response.status_code == 404, response.text
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task_id)
        assert current.statistics_key == new_key
        assert current.status == "paused"
        assert current.step_cursor == 0
        assert current.checkpoint == cp


@pytest.mark.parametrize("transition", ["replacement", "round"])
async def test_skip_rechecks_identity_and_round_after_initial_read(app, client, monkeypatch, transition):
    user, project, task_id, old_key, task_input, cp = await _paused_task(app, client)
    entered, release = asyncio.Event(), asyncio.Event()
    original_get = task_service.get_owned_task
    blocked = False

    async def gated_get(*args, **kwargs):
        nonlocal blocked
        task = await original_get(*args, **kwargs)
        if not blocked:
            blocked = True
            entered.set()
            await release.wait()
        return task

    monkeypatch.setattr(task_service, "get_owned_task", gated_get)
    pending = asyncio.create_task(
        client.post(f"/api/v1/book-imports/tasks/{task_id}/skip", params={"task_key": old_key})
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if transition == "replacement":
            new_key = await _replace_task(app, user, project, task_id, task_input, cp)
        else:
            async with app.state.sessionmaker() as db:
                current = await db.get(Task, task_id)
                current.queued_at += timedelta(seconds=1)
                await db.commit()
            new_key = old_key
        release.set()
        response = await asyncio.wait_for(pending, 10)
        expected = 404 if transition == "replacement" else 409
        assert response.status_code == expected, response.text
        if transition == "round":
            assert response.json()["code"] == "task_round_changed"
        async with app.state.sessionmaker() as db:
            current = await db.get(Task, task_id)
            assert current.statistics_key == new_key
            assert current.status == "paused"
            assert current.step_cursor == 0
            assert current.checkpoint == cp
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending


async def test_skip_requires_original_key_and_preserves_normal_recovery(app, client):
    _, _, task_id, key, _, cp = await _paused_task(app, client)
    url = f"/api/v1/book-imports/tasks/{task_id}/skip"
    assert (await client.post(url)).status_code == 422
    assert (await client.post(url, params={"task_key": "invalid"})).status_code == 404
    assert (await client.post(url, params={"task_key": str(uuid4())})).status_code == 404
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(statistics_key=None))
        await db.commit()
    assert (await client.post(url, params={"task_key": key})).status_code == 404
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task_id)
        assert current.status == "paused" and current.checkpoint == cp
        current.statistics_key = key
        await db.commit()
    response = await client.post(url, params={"task_key": key})
    assert response.status_code == 202, response.text
    assert response.json()["task_key"] == key
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task_id)
        assert current.status == "queued"
        assert current.step_cursor == 1
        assert current.checkpoint["batches"][0]["state"] == "skipped"


async def test_skip_rolls_back_checkpoint_if_resume_is_rejected(app, client):
    _, project, task_id, key, _, cp = await _paused_task(app, client)
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=project["id"], title="章节")
        db.add(chapter)
        await db.flush()
        db.add(SceneReviewProposal(
            task_id=task_id, task_key=key, chapter_id=chapter.id, run_key=str(uuid4()), token=str(uuid4()),
            content_revision=0, payload={},
        ))
        await db.commit()
    response = await client.post(
        f"/api/v1/book-imports/tasks/{task_id}/skip", params={"task_key": key},
    )
    assert response.status_code == 409 and response.json()["code"] == "rich_review_pending", response.text
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task_id)
        assert current.status == "paused"
        assert current.step_cursor == 0
        assert current.checkpoint == cp
