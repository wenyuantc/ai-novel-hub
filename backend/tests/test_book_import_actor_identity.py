"""拆书深度分析必须继承原 HTTP actor 与不可变领取轮次。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.ai.providers import create_provider
from app.models import Character, Project, Task, User
from app.services.book_import import reverse
from app.services.task_engine.hub import TaskHub
from tests.helpers import Gate, analysis_json, wait_task
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_book_import import DIGESTS, SYNTHESIZE, _setup, _start


def _script(fake_llm):
    for item in DIGESTS:
        fake_llm.push(item)
    fake_llm.push(SYNTHESIZE)
    for _ in range(3):
        fake_llm.push(analysis_json())


async def _parent_phase(app, task_id, phase, *, deadline=30):
    async with asyncio.timeout(deadline):
        while True:
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, task_id)
                if task is not None and isinstance(task.checkpoint, dict) and task.checkpoint.get("phase") == phase:
                    return task
                if task is None or task.status in ("failed", "cancelled", "interrupted"):
                    raise AssertionError(f"父任务未达到 {phase}：{task.status if task else None}")
            await asyncio.sleep(0.01)


async def test_deep_analysis_inherits_actual_actor_and_original_token(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    changed = await client.post("/api/v1/auth/change-password", json={
        "old_password": "secret123", "new_password": "new-secret123",
    })
    assert changed.status_code == 204
    _script(fake_llm)
    response = await _start(client, rec, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    await _parent_phase(app, parent["id"], "done")
    async with app.state.sessionmaker() as db:
        saved = await db.get(Task, parent["id"])
        user = await db.get(User, saved.user_id)
        assert user.token_version > 0
        assert saved.input["_task_actor_token_version"] == user.token_version
        children = list(await db.scalars(select(Task).where(
            Task.project_id == saved.project_id, Task.kind == "chapter.analyze",
        ).order_by(Task.id)))
        assert len(children) == 3
        assert all(task.user_id == saved.user_id and task.project_id == saved.project_id for task in children)
        assert all(task.input.get("_task_actor_token_version") == user.token_version for task in children)
        assert all("_collaboration_grant" not in task.input for task in children)
    done = await wait_task(client, parent, within=30)
    assert done["status"] == "succeeded", done


async def test_running_import_cancellation_closes_model_without_late_writes(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    gate = Gate()
    fake_llm.push([gate, DIGESTS[0]])
    response = await _start(client, rec, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    await gate.wait_reached(within=20)
    try:
        async with asyncio.timeout(10):
            cancelled = await client.post(
                f"/api/v1/tasks/{parent['id']}/cancel", params={"task_key": parent["task_key"]},
            )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
    finally:
        gate.release()
    assert fake_llm.aborted == 1 and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, parent["id"])
        project = await db.get(Project, task.project_id)
        assert task.status == "cancelled" and task.checkpoint is None
        assert project.synopsis == ""
        assert (await db.scalars(select(Task).where(
            Task.project_id == task.project_id, Task.kind == "chapter.analyze",
        ))).all() == []


async def test_ready_model_fragments_are_delivered_before_next_fragment_arrives(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    gate = Gate()
    delivered = asyncio.Event()
    original_append = TaskHub.append

    def observed(hub, text):
        original_append(hub, text)
        if "先到紧随" in hub.text:
            delivered.set()

    monkeypatch.setattr(TaskHub, "append", observed)
    fake_llm.push(["先到", "紧随", gate, DIGESTS[0]])
    response = await _start(client, rec, deep_analyze=False, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    try:
        await gate.wait_reached(within=20)
        await asyncio.wait_for(delivered.wait(), 3)
        cancelled = await client.post(
            f"/api/v1/tasks/{parent['id']}/cancel", params={"task_key": parent["task_key"]},
        )
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert fake_llm.aborted == 1
    finally:
        gate.release()


async def test_lost_checkpoint_commit_acknowledgement_never_replays(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    finished = asyncio.Event()
    engine = app.state.task_engine
    original_run = engine._run

    async def observed(*args, **kwargs):
        try:
            return await original_run(*args, **kwargs)
        finally:
            finished.set()

    async def lost_ack(db, job):
        await db.commit()
        raise RuntimeError("simulated lost commit acknowledgement")

    monkeypatch.setattr(engine, "_run", observed)
    monkeypatch.setattr(reverse, "_commit_checkpoint", lost_ack)
    _script(fake_llm)
    response = await _start(client, rec, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    await asyncio.wait_for(finished.wait(), 30)
    parent = response.json()["task"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, parent["id"])
        assert task.status == "running" and task.execution_key is not None
        assert task.step_cursor == 1 and task.checkpoint["batches"][0]["state"] == "done"
        assert (await db.scalars(select(Task).where(
            Task.project_id == task.project_id, Task.kind == "chapter.analyze",
        ))).all() == []
    assert len(fake_llm.calls) == 1


async def test_revoked_actor_cannot_publish_late_model_output(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    marker = "PRIVATE_IMPORT_MODEL_FRAGMENT"
    fake_llm.push([marker, DIGESTS[0]])
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    engine = app.state.task_engine
    original_event, original_run = engine._event_task, engine._run

    async def gated(db, execution, current, event):
        if event == "delta" and not entered.is_set():
            entered.set()
            await release.wait()
        return await original_event(db, execution, current, event)

    async def observed(*args, **kwargs):
        try:
            return await original_run(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(engine, "_event_task", gated)
    monkeypatch.setattr(engine, "_run", observed)
    response = await _start(client, rec, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    stream = asyncio.create_task(client.get(
        f"/api/v1/tasks/{parent['id']}/stream", params={"task_key": parent["task_key"]},
    ))
    try:
        await asyncio.wait_for(entered.wait(), 30)
        changed = await client.post("/api/v1/auth/change-password", json={
            "old_password": "secret123", "new_password": "new-secret123",
        })
        assert changed.status_code == 204
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 30)
    delivered = await asyncio.wait_for(stream, 30)
    assert marker not in delivered.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, parent["id"])
        assert marker not in (task.output_text or "")
        # design §5：失败关闭允许按原执行事实做无正文系统收尾，不借当前用户补签。
        assert task.status == "failed" and task.execution_key is None
        assert (await db.scalars(select(Task).where(
            Task.project_id == task.project_id, Task.kind == "chapter.analyze",
        ))).all() == []


async def test_legacy_import_without_original_token_never_sends_or_creates_children(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    engine = app.state.task_engine
    await engine.stop()
    _script(fake_llm)
    response = await _start(client, rec, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    task_id, project_id = response.json()["task"]["id"], response.json()["project_id"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        payload = dict(task.input)
        payload.pop("_task_actor_token_version")
        task.input = payload
        await db.commit()
    finished = asyncio.Event()
    original = engine._claim_and_run

    async def observed(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(engine, "_claim_and_run", observed)
    await engine.start()
    await asyncio.wait_for(finished.wait(), 30)
    assert fake_llm.calls == []
    async with app.state.sessionmaker() as db:
        persisted = await db.get(Task, task_id)
        assert persisted.status == "queued" and persisted.execution_key is None
        assert (await db.scalars(select(Task).where(
            Task.project_id == project_id, Task.kind == "chapter.analyze",
        ))).all() == []


async def test_invalidated_original_token_cannot_enqueue_analysis(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = reverse._analyze
    engine = app.state.task_engine
    original_run = engine._run

    async def observed(*args, **kwargs):
        try:
            return await original_run(*args, **kwargs)
        finally:
            finished.set()

    async def gated(ctx, job):
        entered.set()
        await release.wait()
        return await original(ctx, job)

    monkeypatch.setattr(reverse, "_analyze", gated)
    monkeypatch.setattr(engine, "_run", observed)
    _script(fake_llm)
    response = await _start(client, rec, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    try:
        await asyncio.wait_for(entered.wait(), 30)
        changed = await client.post("/api/v1/auth/change-password", json={
            "old_password": "secret123", "new_password": "new-secret123",
        })
        assert changed.status_code == 204
    finally:
        release.set()
    await asyncio.wait_for(finished.wait(), 30)
    async with app.state.sessionmaker() as db:
        saved = await db.get(Task, parent["id"])
        # 原令牌已失效：只允许按原执行事实做无正文系统收尾（design §5），不能借当前用户补签。
        assert saved.status == "failed" and saved.execution_key is None
        children = list(await db.scalars(select(Task).where(
            Task.project_id == saved.project_id, Task.kind == "chapter.analyze",
        )))
        assert children == []
        assert saved.checkpoint["phase"] == "analyze"
    # 深度分析的外呼也不能发生；之前完成的拆书调用可以保留。
    assert len(fake_llm.calls) == 5


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_password_change_stops_next_real_model_request(app, client, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    monkeypatch.setattr(reverse, "RETRY_DELAYS", (0.0, 0.0))
    rec = await _setup(client)
    app.state.task_engine.ctx.factory = create_provider
    completed = asyncio.Event()
    original = reverse._call

    async def observed(*args, **kwargs):
        try:
            async for event in original(*args, **kwargs):
                yield event
        finally:
            completed.set()

    monkeypatch.setattr(reverse, "_call", observed)
    async with _server(payload=_sse("openai")) as (url, received, headers, body, calls):
        config = (await client.get("/api/v1/ai-configs")).json()[0]
        updated = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"base_url": f"{url}/v1/"})
        assert updated.status_code == 200, updated.text
        response = await _start(client, rec, deep_analyze=True, extract_style=False)
        assert response.status_code == 202, response.text
        try:
            await asyncio.wait_for(received.wait(), 30)
            headers.set()
            changed = await client.post("/api/v1/auth/change-password", json={
                "old_password": "secret123", "new_password": "new-secret123",
            })
            assert changed.status_code == 204
            body.set()
            await asyncio.wait_for(completed.wait(), 30)
            assert len(calls) == 1
            async with app.state.sessionmaker() as db:
                assert (await db.scalars(select(Task).where(
                    Task.project_id == response.json()["project_id"], Task.kind == "chapter.analyze",
                ))).all() == []
        finally:
            headers.set()
            body.set()


async def test_replaced_execution_cannot_enqueue_analysis(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = reverse._analyze

    async def gated(ctx, job):
        entered.set()
        await release.wait()
        try:
            return await original(ctx, job)
        finally:
            finished.set()

    monkeypatch.setattr(reverse, "_analyze", gated)
    _script(fake_llm)
    response = await _start(client, rec, deep_analyze=True, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    try:
        await asyncio.wait_for(entered.wait(), 30)
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, parent["id"])
            assert row.status == "running" and row.execution_key is not None
            row.execution_key = str(uuid4())
            await db.commit()
        release.set()
        await asyncio.wait_for(finished.wait(), 30)
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, parent["id"])
            assert row is not None and row.checkpoint["phase"] == "analyze"
            assert (await db.scalars(select(Task).where(
                Task.project_id == row.project_id, Task.kind == "chapter.analyze",
            ))).all() == []
        assert len(fake_llm.calls) == 5
    finally:
        release.set()


async def test_replaced_execution_cannot_apply_book_results(app, client, fake_llm, monkeypatch):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    rec = await _setup(client)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = reverse._apply

    async def gated(ctx, job):
        entered.set()
        await release.wait()
        try:
            return await original(ctx, job)
        finally:
            finished.set()

    monkeypatch.setattr(reverse, "_apply", gated)
    _script(fake_llm)
    response = await _start(client, rec, deep_analyze=False, extract_style=False)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    project_id = response.json()["project_id"]
    try:
        await asyncio.wait_for(entered.wait(), 30)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, parent["id"])
            assert task.status == "running" and task.checkpoint["phase"] == "apply"
            task.execution_key = str(uuid4())
            await db.commit()
        release.set()
        await asyncio.wait_for(finished.wait(), 30)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, parent["id"])
            project = await db.get(Project, project_id)
            assert task.checkpoint["phase"] == "apply" and project.synopsis == ""
            assert await db.scalar(select(func.count(Character.id)).where(Character.project_id == project_id)) == 0
        assert len(fake_llm.calls) == 5
    finally:
        release.set()


@pytest.mark.parametrize(("phase", "expected_calls"), [("digest", 0), ("synthesize", 4), ("style", 5)])
@pytest.mark.parametrize("replacement", ["execution", "checkpoint"])
async def test_replaced_execution_cannot_send_or_advance_in_other_phases(
    app, client, fake_llm, monkeypatch, phase, expected_calls, replacement,
):
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    monkeypatch.setattr(reverse, "RETRY_DELAYS", (0.0, 0.0))
    rec = await _setup(client)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    name = {"digest": "_digest_batch", "synthesize": "_synthesize", "style": "_style"}[phase]
    original = getattr(reverse, name)

    async def gated(*args, **kwargs):
        entered.set()
        await release.wait()
        try:
            async for event in original(*args, **kwargs):
                yield event
        finally:
            finished.set()

    monkeypatch.setattr(reverse, name, gated)
    _script(fake_llm)
    response = await _start(client, rec, deep_analyze=False, extract_style=True)
    assert response.status_code == 202, response.text
    parent = response.json()["task"]
    project_id = response.json()["project_id"]
    try:
        await asyncio.wait_for(entered.wait(), 30)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, parent["id"])
            assert task.status == "running"
            if replacement == "execution":
                expected_checkpoint = task.checkpoint
                task.execution_key = str(uuid4())
            else:
                expected_checkpoint = {"phase": "替换后的断点"}
                task.checkpoint = expected_checkpoint
            await db.commit()
        release.set()
        await asyncio.wait_for(finished.wait(), 30)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, parent["id"])
            assert task.checkpoint == expected_checkpoint and task.project_id == project_id
        assert len(fake_llm.calls) == expected_calls
    finally:
        release.set()


