"""受理预检绑定原请求和重试来源，不能伪造或跨事务复用。"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.core.errors import AppError, ConflictError
from app.models import AIConfig, Task, User
from app.services import ai_config_service, authorization_service, project_ai_config, task_service
from app.services.task_engine.admission import (
    capture_retry_source,
    consume_task_admission,
    prepare_task_admission,
    require_consumed_retry_admission,
)
from app.services.task_engine.registry import TaskSpec, get_handler
from tests.helpers import create_ai_config, create_project, register


async def _world(app, client):
    owner = await register(client, "admission-owner")
    config = await create_ai_config(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        user = await db.get(User, owner["id"])
        actor = authorization_service.AuthenticatedActor(user.id, user.token_version)
    preflight = await project_ai_config.preflight_project_model(
        app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        allow_private=app.state.settings.allow_private_ai_endpoints,
    )
    return actor, project, config, preflight


async def _source(app, actor, project, binding):
    async with app.state.sessionmaker() as db:
        task = Task(user_id=actor.id, project_id=project["id"], kind="outline.continue", status="failed",
                    input={"request": {"project_id": project["id"]}, "_task_actor_token_version": actor.token_version,
                           "_project_model": project_ai_config.serialize_project_model(binding)})
        db.add(task)
        await db.commit()
        return capture_retry_source(task, expected_task_key=task.statistics_key)


async def test_fresh_admission_binds_original_actor_and_request_once(app, client, monkeypatch):
    actor, project, _config, preflight = await _world(app, client)
    calls = []

    async def build(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(ai_config_service, "build_provider", build)
    params = {"project_id": project["id"], "body": {"chapter": 1}}
    proof = await prepare_task_admission(app.state.task_engine.ctx, actor, "outline.continue", params,
                                         model_preflight=preflight)
    assert calls == [True] and "chapter" not in repr(proof)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        await authorization_service.require_authenticated(db, actor, write=True)
        accepted = await consume_task_admission(db, proof, actor=actor, kind="outline.continue", params=params,
                                                retry_of=None)
        assert accepted is preflight
        with pytest.raises(ConflictError):
            await consume_task_admission(db, proof, actor=actor, kind="outline.continue", params=params,
                                         retry_of=None)
        await db.rollback()


async def test_retry_preflight_cannot_be_reused_for_another_source_or_forged_before_dns(app, client, monkeypatch):
    actor, project, _config, preflight = await _world(app, client)
    first = await _source(app, actor, project, preflight.binding)
    second = await _source(app, actor, project, preflight.binding)
    original = await project_ai_config.preflight_original_project_model(
        app.state.sessionmaker, actor, first, cipher=app.state.cipher,
        allow_private=app.state.settings.allow_private_ai_endpoints,
    )
    calls = []

    async def build(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(ai_config_service, "build_provider", build)
    params = {"project_id": project["id"]}
    for source in (second, replace(first)):
        with pytest.raises(AppError):
            await prepare_task_admission(app.state.task_engine.ctx, actor, "outline.continue", params,
                                         model_preflight=original, retry_source=source)
    assert not calls


async def test_original_retry_preflight_keeps_original_model_and_consumed_proof_is_once(app, client, monkeypatch):
    actor, project, config, preflight = await _world(app, client)
    source = await _source(app, actor, project, preflight.binding)
    async with app.state.sessionmaker() as db:
        old = await db.get(AIConfig, config["id"])
        old.is_default = False
        db.add(AIConfig(user_id=actor.id, name="后来默认", provider=old.provider, model="different",
                        model_type="text", is_default=True, api_key_enc=old.api_key_enc))
        await db.commit()
    original = await project_ai_config.preflight_original_project_model(
        app.state.sessionmaker, actor, source, cipher=app.state.cipher,
        allow_private=app.state.settings.allow_private_ai_endpoints,
    )
    assert original.binding == preflight.binding

    async def build(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ai_config_service, "build_provider", build)
    params = {"project_id": project["id"]}
    proof = await prepare_task_admission(app.state.task_engine.ctx, actor, "outline.continue", params,
                                         model_preflight=original, retry_source=source)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        await authorization_service.require_authenticated(db, actor, write=True)
        row = await db.scalar(select(Task).where(Task.id == source.task_id).with_for_update())
        await consume_task_admission(db, proof, actor=actor, kind="outline.continue", params=params, retry_of=row)
        accepted = await project_ai_config.accept_original_preflight_project_model(
            db, original, admission=proof, cipher=app.state.cipher,
            allow_private=app.state.settings.allow_private_ai_endpoints,
        )
        assert accepted == preflight.binding
        with pytest.raises(AppError):
            await require_consumed_retry_admission(db, proof, original)
        await db.rollback()


async def test_fresh_and_original_retry_proofs_cannot_be_mixed_before_dns(app, client, monkeypatch):
    actor, project, _config, fresh = await _world(app, client)
    source = await _source(app, actor, project, fresh.binding)
    original = await project_ai_config.preflight_original_project_model(
        app.state.sessionmaker, actor, source, cipher=app.state.cipher,
        allow_private=app.state.settings.allow_private_ai_endpoints,
    )
    calls = []

    async def build(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(ai_config_service, "build_provider", build)
    for preflight, retry_source in ((fresh, source), (original, None)):
        with pytest.raises(AppError):
            await prepare_task_admission(app.state.task_engine.ctx, actor, "outline.continue",
                                         {"project_id": project["id"]}, model_preflight=preflight,
                                         retry_source=retry_source)
    assert not calls


@pytest.mark.parametrize("change", ["dirty_source", "transaction_ended"])
async def test_consumed_retry_proof_does_not_discard_changes_or_cross_transaction(app, client, monkeypatch, change):
    actor, project, _config, fresh = await _world(app, client)
    source = await _source(app, actor, project, fresh.binding)
    original = await project_ai_config.preflight_original_project_model(
        app.state.sessionmaker, actor, source, cipher=app.state.cipher,
        allow_private=app.state.settings.allow_private_ai_endpoints,
    )

    async def build(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ai_config_service, "build_provider", build)
    params = {"project_id": project["id"]}
    proof = await prepare_task_admission(app.state.task_engine.ctx, actor, "outline.continue", params,
                                         model_preflight=original, retry_source=source)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        await authorization_service.require_authenticated(db, actor, write=True)
        row = await db.scalar(select(Task).where(Task.id == source.task_id).with_for_update())
        await consume_task_admission(db, proof, actor=actor, kind="outline.continue", params=params, retry_of=row)
        if change == "dirty_source":
            row.input = {**row.input, "changed": True}
        else:
            scalar = db.scalar

            async def end_transaction(*args, **kwargs):
                found = await scalar(*args, **kwargs)
                await db.commit()
                return found

            monkeypatch.setattr(db, "scalar", end_transaction)
        with pytest.raises(AppError):
            await require_consumed_retry_admission(db, proof, original)
        if change == "dirty_source":
            assert row.input["changed"] is True
        await db.rollback()


async def test_aware_handler_cannot_commit_the_admission_transaction(app, client, monkeypatch):
    actor, project, _config, fresh = await _world(app, client)

    async def build(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ai_config_service, "build_provider", build)
    params = {"project_id": project["id"]}
    proof = await prepare_task_admission(app.state.task_engine.ctx, actor, "outline.continue", params,
                                         model_preflight=fresh)
    handler = get_handler("outline.continue")

    async def premature_commit(db, _ctx, *, user_id, kind, params, admission, accepted_model):
        await db.commit()
        return TaskSpec(project_id=project["id"], chapter_id=None, resource_key=None, title="不应创建", input={})

    monkeypatch.setattr(handler, "validate_and_claim", premature_commit)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(AppError):
            await task_service.create_task(db, app.state.task_engine, actor.id, "outline.continue", params,
                                           admission=proof)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert not list(await db.scalars(select(Task).where(Task.project_id == project["id"])))
