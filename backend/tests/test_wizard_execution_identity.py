"""真实向导任务在模型返回后不能为被复用的执行体写入结果。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Character, Task, User
from app.services import wizard_service
from app.services.ai_dispatch import lock_visibility_write
from app.services.setting_ai_visibility import bump_visibility_revision
from tests.helpers import Gate, create_ai_config, create_project, register, wait_task, wait_until


@pytest.mark.parametrize("replace", ["task", "execution", "token"])
async def test_changed_running_execution_cannot_replace_characters(app, client, fake_llm, replace):
    actor = await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post(f"{base}/characters", json={"name": "原角色"})
    assert created.status_code == 201, created.text
    gate = Gate()
    fake_llm.push([gate, '[{"name":"迟到的新角色"}]'])
    response = await client.post(f"{base}/wizard/characters", json={})
    assert response.status_code == 202, response.text
    task = response.json()
    try:
        await gate.wait_reached(within=10)
        running = app.state.task_engine._running[task["id"]]
        async with app.state.sessionmaker() as db:
            current = await db.get(Task, task["id"])
            assert current.statistics_key == task["task_key"] and current.execution_key is not None
            if replace == "task":
                await db.execute(delete(Task).where(Task.id == task["id"]))
                db.add(Task(id=task["id"], user_id=actor["id"], project_id=project["id"],
                            kind="wizard.characters", status="running", execution_key=str(uuid4()),
                            input={"request": {"project_id": project["id"]}}))
            elif replace == "execution":
                await db.execute(update(Task).where(Task.id == task["id"]).values(execution_key=str(uuid4())))
            else:
                await db.execute(update(User).where(User.id == actor["id"]).values(
                    token_version=User.token_version + 1,
                ))
            await db.commit()
        gate.release()
        await asyncio.wait_for(asyncio.shield(running), 10)
        async with app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(Character).where(Character.project_id == project["id"])))
            assert [row.name for row in rows] == ["原角色"]
    finally:
        gate.release()


@pytest.mark.parametrize("evidence", ["missing", "malformed", "rotated"])
async def test_resume_without_original_actor_token_evidence_is_rejected(app, client, fake_llm, evidence):
    actor = await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        task_input = {"request": {"project_id": project["id"]}, "ai_history_epoch": 0}
        if evidence != "missing":
            task_input["_wizard_actor_token_version"] = True if evidence == "malformed" else 0
            task_input["_task_actor_token_version"] = True if evidence == "malformed" else 0
        source = Task(user_id=actor["id"], project_id=project["id"], kind="wizard.outline", status="failed",
                      input=task_input,
                      output_text='{"volumes":[{"title":"旧输出"}')
        db.add(source)
        if evidence == "rotated":
            (await db.get(User, actor["id"])).token_version += 1
        await db.commit()
        old_id, old_key = source.id, source.statistics_key
    if evidence == "rotated":
        assert (await client.post("/api/v1/auth/login", json={
            "username": "alice", "password": "secret123",
        })).status_code == 200
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": old_id, "continue_from_task_key": old_key,
    })
    assert response.status_code in (400, 409), response.text
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(Task.id))) == [old_id]
    assert not fake_llm.calls


async def test_queued_wizard_keeps_accepted_model_if_default_changes(app, client, fake_llm):
    await register(client)
    await create_ai_config(client, name="原模型", model="model-original")
    project = await create_project(client)
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/world", json={})
    assert response.status_code == 202, response.text
    await create_ai_config(client, name="新默认模型", model="model-new", is_default=True)
    fake_llm.push('[{"title":"山川","content":"旧绑定"}]')
    app.state.task_engine.runtime.settings_confirmed = True
    app.state.task_engine.notify()
    finished = await wait_task(client, response.json(), within=15)
    assert finished["status"] == "succeeded", finished
    assert fake_llm.calls[-1]["model"] == "model-original"


@pytest.mark.parametrize("change", ["deleted", "modified"])
async def test_queued_wizard_rejects_changed_accepted_model(app, client, fake_llm, change):
    await register(client)
    config = await create_ai_config(client, name="原模型", model="model-original")
    project = await create_project(client)
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/world", json={})
    assert response.status_code == 202, response.text
    if change == "deleted":
        assert (await client.delete(f"/api/v1/ai-configs/{config['id']}")).status_code == 204
        await create_ai_config(client, name="新模型", model="model-new")
    else:
        updated = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "model-modified"})
        assert updated.status_code == 200, updated.text
    fake_llm.push('[{"title":"山川","content":"不应写入"}]')
    app.state.task_engine.runtime.settings_confirmed = True
    app.state.task_engine.notify()
    finished = await wait_task(client, response.json(), within=15)
    assert finished["status"] == "failed", finished
    assert not fake_llm.calls


async def test_wizard_does_not_save_after_bound_model_changes_during_response(app, client, fake_llm):
    await register(client)
    config = await create_ai_config(client, name="原模型", model="model-original")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    assert (await client.post(f"{base}/characters", json={"name": "原角色"})).status_code == 201
    gate = Gate()
    fake_llm.push([gate, '[{"name":"迟到的新角色"}]'])
    response = await client.post(f"{base}/wizard/characters", json={})
    assert response.status_code == 202, response.text
    try:
        await gate.wait_reached(within=10)
        updated = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "model-modified"})
        assert updated.status_code == 200, updated.text
        gate.release()
        finished = await wait_task(client, response.json(), within=15)
        assert finished["status"] == "failed", finished
        async with app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(Character).where(Character.project_id == project["id"])))
            assert [row.name for row in rows] == ["原角色"]
    finally:
        gate.release()


async def test_wizard_does_not_save_after_visibility_tightens_during_response(app, client, fake_llm):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    assert (await client.post(f"{base}/characters", json={"name": "原角色"})).status_code == 201
    gate = Gate()
    fake_llm.push([gate, '[{"name":"迟到的新角色"}]'])
    response = await client.post(f"{base}/wizard/characters", json={})
    assert response.status_code == 202, response.text
    try:
        await gate.wait_reached(within=10)
        async with app.state.sessionmaker() as db:
            await lock_visibility_write(db, project["id"])
            await bump_visibility_revision(db, project["id"], tighten=True)
            await db.commit()
        gate.release()
        finished = await wait_task(client, response.json(), within=15)
        assert finished["status"] == "failed", finished
        async with app.state.sessionmaker() as db:
            rows = list(await db.scalars(select(Character).where(Character.project_id == project["id"])))
            assert [row.name for row in rows] == ["原角色"]
    finally:
        gate.release()


@pytest.mark.parametrize("change", ["task", "execution", "token", "model"])
async def test_changed_identity_before_dispatch_never_sends(app, client, fake_llm, monkeypatch, change):
    actor = await register(client)
    config = await create_ai_config(client)
    project = await create_project(client)
    ready, release = asyncio.Event(), asyncio.Event()
    original = wizard_service.build_wizard_job

    async def pause_after_build(*args, **kwargs):
        job = await original(*args, **kwargs)
        ready.set()
        await release.wait()
        return job

    monkeypatch.setattr(wizard_service, "build_wizard_job", pause_after_build)
    fake_llm.push('[{"title":"不能发送","content":"不能写入"}]')
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/world", json={})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    try:
        await asyncio.wait_for(ready.wait(), 10)
        running = app.state.task_engine._running[task_id]
        if change == "model":
            updated = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "changed"})
            assert updated.status_code == 200
        else:
            async with app.state.sessionmaker() as db:
                if change == "task":
                    await db.execute(delete(Task).where(Task.id == task_id))
                    db.add(Task(id=task_id, user_id=actor["id"], project_id=project["id"], kind="wizard.world",
                                status="running", execution_key=str(uuid4()), input={}))
                elif change == "execution":
                    await db.execute(update(Task).where(Task.id == task_id).values(execution_key=str(uuid4())))
                else:
                    await db.execute(update(User).where(User.id == actor["id"]).values(
                        token_version=User.token_version + 1,
                    ))
                await db.commit()
        release.set()
        await asyncio.wait_for(asyncio.shield(running), 10)
        assert not fake_llm.calls
    finally:
        release.set()


@pytest.mark.parametrize("outcome", ["committed", "rolled_back"])
async def test_wizard_unknown_save_ack_cannot_blindly_retry(app, client, fake_llm, monkeypatch, outcome):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    assert (await client.post(f"{base}/characters", json={"name": "原角色"})).status_code == 201
    original_commit = AsyncSession.commit
    lost = False

    async def lose_ack(db):
        nonlocal lost
        if not lost and any(isinstance(row, Character) and row.name == "新角色" for row in db.identity_map.values()):
            lost = True
            if outcome == "committed":
                await original_commit(db)
            else:
                await db.rollback()
            raise RuntimeError("模拟保存确认丢失")
        await original_commit(db)

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    fake_llm.push('[{"name":"新角色"}]')
    response = await client.post(f"{base}/wizard/characters", json={})
    assert response.status_code == 202, response.text
    task_ref = response.json()
    await wait_until(lambda: lost and task_ref["id"] not in app.state.task_engine._running, within=15)
    detail = await client.get(f"/api/v1/tasks/{task_ref['id']}", params={"task_key": task_ref["task_key"]})
    assert detail.status_code == 200
    finished = detail.json()
    assert lost and finished["status"] in ("failed", "succeeded", "interrupted", "running")
    assert finished["checkpoint"] is None
    retry = await client.post(f"/api/v1/tasks/{finished['id']}/retry", params={"task_key": finished["task_key"]})
    assert retry.status_code == 409, retry.text
    async with app.state.sessionmaker() as db:
        names = list(await db.scalars(select(Character.name).where(Character.project_id == project["id"])))
    assert names == (["新角色"] if outcome == "committed" else ["原角色"])
    assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("corrupt", [False, True])
async def test_only_proven_finished_wizard_receipt_can_be_deleted(app, client, fake_llm, corrupt):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    fake_llm.push('[{"title":"世界","content":"原始结果"}]')
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/world", json={})
    assert response.status_code == 202, response.text
    task_ref = response.json()
    finished = await wait_task(client, task_ref, within=15)
    assert finished["status"] == "succeeded" and finished["checkpoint"] is None
    await wait_until(lambda: task_ref["id"] not in app.state.task_engine._running, within=15)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_ref["id"])
        assert wizard_service.verified_wizard_terminal_receipt(row)
        if corrupt:
            checkpoint = dict(row.checkpoint)
            checkpoint["_wizard_completion"] = {**checkpoint["_wizard_completion"], "result_hash": "0" * 64}
            row.checkpoint = checkpoint
            await db.commit()
    result = await client.delete(f"/api/v1/tasks/{task_ref['id']}", params={"task_key": task_ref["task_key"]})
    assert result.status_code == (409 if corrupt else 204), result.text


async def test_owner_can_cancel_before_save_and_pending_is_released(app, client, fake_llm):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    assert (await client.post(f"{base}/characters", json={"name": "原角色"})).status_code == 201
    gate = Gate()
    fake_llm.push([gate, '[{"name":"不应保存"}]'])
    response = await client.post(f"{base}/wizard/characters", json={})
    assert response.status_code == 202, response.text
    ref = response.json()
    try:
        await gate.wait_reached(within=10)
        cancelled = await client.post(f"/api/v1/tasks/{ref['id']}/cancel", params={"task_key": ref["task_key"]})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, ref["id"])
            assert not task.checkpoint or "_wizard_pending" not in task.checkpoint
            assert list(await db.scalars(select(Character.name).where(Character.project_id == project["id"]))) == [
                "原角色"
            ]
    finally:
        gate.release()
