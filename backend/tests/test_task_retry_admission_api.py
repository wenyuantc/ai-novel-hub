"""通用任务重试使用用户最初看到的任务与服务端原模型绑定。"""

from sqlalchemy import select

from app.models import Task
from app.services import ai_config_service, project_ai_config, task_service
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import bind_task_grant
from app.services.task_engine.registry import get_handler
from tests.helpers import create_ai_config, create_project, register


async def _failed_outline(app, client, monkeypatch):
    await register(client, "retry-admission-owner")
    project = await create_project(client)
    original = await create_ai_config(client, model="original-model")
    replacement = await create_ai_config(client, model="replacement-model")
    handler = get_handler("outline.continue")
    monkeypatch.setattr(handler, "admission_preflight_aware", True, raising=False)
    await app.state.task_engine.stop()
    response = await client.post(f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1})
    assert response.status_code == 202, response.text
    task = response.json()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        old_binding = dict(row.input["_project_model"])
        row.status = "failed"
        await db.commit()
    return task, original, replacement, old_binding


async def test_retry_uses_original_model_when_default_changes(app, client, monkeypatch):
    task, original, replacement, binding = await _failed_outline(app, client, monkeypatch)
    assert (await client.post(f"/api/v1/ai-configs/{replacement['id']}/default")).status_code == 200
    response = await client.post(f"/api/v1/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
    assert response.status_code == 202, response.text
    async with app.state.sessionmaker() as db:
        retried = await db.get(Task, response.json()["id"])
        assert retried.input["_project_model"] == binding
        assert retried.input["ai_config_id"] == original["id"]
        assert retried.statistics_key != task["task_key"]


async def test_retry_rejects_changed_original_model_without_disclosing_it(app, client, monkeypatch):
    task, original, _replacement, _binding = await _failed_outline(app, client, monkeypatch)
    changed = await client.patch(f"/api/v1/ai-configs/{original['id']}", json={"model": "private-changed"})
    assert changed.status_code == 200
    response = await client.post(f"/api/v1/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
    assert response.status_code in (400, 409) and "private-changed" not in response.text


async def test_retry_missing_key_or_stale_actor_never_preflights(app, client, monkeypatch):
    task, _original, _replacement, _binding = await _failed_outline(app, client, monkeypatch)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        row.input = {**row.input, "_task_actor_token_version": 999}
        await db.commit()
    called = []
    original = project_ai_config.preflight_original_project_model

    async def track(*args, **kwargs):
        called.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(project_ai_config, "preflight_original_project_model", track)
    wrong = await client.post(f"/api/v1/tasks/{task['id']}/retry", params={
        "task_key": "00000000-0000-4000-8000-000000000000",
    })
    assert wrong.status_code == 404
    stale = await client.post(f"/api/v1/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
    assert stale.status_code == 409
    assert not called


async def test_retry_detects_source_change_during_lock_free_provider_preflight(app, client, monkeypatch):
    task, _original, _replacement, _binding = await _failed_outline(app, client, monkeypatch)
    original = ai_config_service.build_provider
    touched = []

    async def change_source(*args, **kwargs):
        async with app.state.sessionmaker() as db:
            row = await db.scalar(select(Task).where(Task.id == task["id"], Task.statistics_key == task["task_key"]))
            row.checkpoint = {"changed": True}
            await db.commit()
        touched.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(ai_config_service, "build_provider", change_source)
    response = await client.post(f"/api/v1/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
    assert touched and response.status_code == 409, response.text
    async with app.state.sessionmaker() as db:
        assert (await db.scalar(select(Task).where(Task.id == task["id"]))).checkpoint == {"changed": True}


async def test_collaborator_owner_model_retry_uses_original_source_only_when_handler_ready(
    app, client, make_client, monkeypatch,
):
    await app.state.task_engine.stop()
    await register(client, "retry-source-owner")
    editor_client = make_client()
    editor = await register(editor_client, "retry-source-editor")
    project = await create_project(client)
    await create_ai_config(client, model="private-owner-model")
    base = f"/api/v1/projects/{project['id']}"
    invitation = await client.post(f"{base}/collaborators", json={
        "username": "retry-source-editor", "role": "editor", "allow_owner_ai": True,
    })
    assert invitation.status_code == 201
    accepted = await editor_client.post(
        f"/api/v1/collaboration/invitations/project/{invitation.json()['invitation_id']}/accept",
        json={"expected_revision": invitation.json()["revision"]},
    )
    assert accepted.status_code == 200
    actor = AuthenticatedActor(editor["id"], 0)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        model = await project_ai_config.resolve_project_model(
            db, actor.id, project["id"], "text", cipher=app.state.cipher, for_update=True,
        )
        assert model.source == "owner"
        await db.rollback()
        payload = bind_task_grant({
            "request": {"project_id": project["id"], "count": 1},
            "_project_model": project_ai_config.serialize_project_model(model), "_task_actor_token_version": 0,
        }, model.grant_identity)
        row = Task(user_id=actor.id, project_id=project["id"], kind="outline.continue", status="failed", input=payload)
        db.add(row)
        await db.commit()
        task_id, key = row.id, row.statistics_key
        result = task_service.to_out(row).model_copy(update={"task_key": key})

    handler = get_handler("outline.continue")
    monkeypatch.setattr(handler, "admission_preflight_aware", True, raising=False)
    monkeypatch.setattr(handler, "collaboration_execution_ready", False, raising=False)
    path = f"/api/v1/tasks/{task_id}/retry"
    blocked = await editor_client.post(path, params={"task_key": key})
    assert blocked.status_code == 409 and "private-owner-model" not in blocked.text

    monkeypatch.setattr(handler, "collaboration_execution_ready", True, raising=False)
    calls = []

    async def record(_db, _engine, _actor_id, _task_id, *, expected_task_key, admission):
        assert expected_task_key == key and admission.retry_source.task_key == key
        assert project_ai_config.is_original_model_preflight(admission.model_preflight)
        assert admission.model_preflight.binding == model
        calls.append(True)
        return result

    monkeypatch.setattr(task_service, "retry_task", record)
    response = await editor_client.post(path, params={"task_key": key})
    assert response.status_code == 202 and calls == [True]
    assert "private-owner-model" not in response.text
