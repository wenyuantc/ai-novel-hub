"""模型预检必须接纳同源事务包装，同时拒绝其他引擎或授权运行时。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.authorization_runtime import AuthorizationRuntime
from app.core.errors import BadRequestError, ConflictError, UnauthorizedError
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AIConfig, Task
from app.services import project_access, project_ai_config
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.admission import consume_task_admission, prepare_task_admission
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_rewrite_identity import _http_clients
from tests.test_project_model_preflight import _retry_world


async def _owner_preflight(app, client):
    await register(client, "engine-identity-owner")
    project = await create_project(client)
    await create_ai_config(client)
    token = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
    assert token is not None
    actor = AuthenticatedActor(*token)
    preflight = await project_ai_config.preflight_project_model(
        app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
    )
    return actor, preflight


def _option_engine(engine, depth=1):
    wrapped = engine
    for _ in range(depth):
        wrapped = wrapped.execution_options(isolation_level="SERIALIZABLE")
    assert wrapped is not engine and wrapped.sync_engine.pool is engine.sync_engine.pool
    return wrapped


@pytest.mark.parametrize("depth", [1, 2])
async def test_same_engine_transaction_option_accepts_original_actor_and_model(app, client, depth):
    actor, preflight = await _owner_preflight(app, client)
    async with app.state.sessionmaker(bind=_option_engine(app.state.engine, depth)) as db:
        db.info["authenticated_actor"] = actor
        result = await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
        assert result == preflight.binding
        await db.rollback()


@pytest.mark.parametrize("same_pool", [False, True])
async def test_other_engine_same_url_and_forged_info_cannot_use_preflight(app, client, same_pool):
    actor, preflight = await _owner_preflight(app, client)
    kw = {"pool": app.state.engine.sync_engine.pool} if same_pool else {}
    other = create_async_engine(app.state.engine.url, **kw)
    assert other.sync_engine is not app.state.engine.sync_engine
    assert (other.sync_engine.pool is app.state.engine.sync_engine.pool) is same_pool
    maker = async_sessionmaker(other, info=dict(app.state.sessionmaker.kw["info"]))
    try:
        async with maker() as db:
            # 即使伪造原 runtime 和 actor，也不能跨不同根引擎消费预检。
            db.info["authenticated_actor"] = actor
            with pytest.raises(BadRequestError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            assert await project_ai_config.accept_preflight_project_model(
                db, preflight, cipher=app.state.cipher,
            ) == preflight.binding
    finally:
        if not same_pool:
            await other.dispose()


async def test_different_application_runtime_or_actor_cannot_use_option_engine(app, client):
    actor, preflight = await _owner_preflight(app, client)
    wrapped = _option_engine(app.state.engine)
    other_app = async_sessionmaker(wrapped, info={"authorization_runtime": AuthorizationRuntime()})
    async with other_app() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(BadRequestError):
            await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
    async with app.state.sessionmaker(bind=wrapped) as db:
        db.info["authenticated_actor"] = AuthenticatedActor(actor.id, actor.token_version + 1)
        with pytest.raises(UnauthorizedError):
            await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_same_primary_bind_cannot_hide_a_different_config_engine(app, client):
    actor, preflight = await _owner_preflight(app, client)
    other = create_async_engine(app.state.engine.url)
    maker = async_sessionmaker(app.state.engine, binds={AIConfig: other},
                               info=dict(app.state.sessionmaker.kw["info"]))
    try:
        async with maker() as db:
            db.info["authenticated_actor"] = actor
            assert db.bind is app.state.engine
            with pytest.raises(BadRequestError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
    finally:
        await other.dispose()


async def test_original_connection_as_session_bind_cannot_replace_engine_proof(app, client):
    actor, preflight = await _owner_preflight(app, client)
    async with app.state.engine.connect() as connection:
        maker = async_sessionmaker(connection, info=dict(app.state.sessionmaker.kw["info"]))
        async with maker() as db:
            db.info["authenticated_actor"] = actor
            with pytest.raises(BadRequestError):
                await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


@pytest.mark.parametrize("option", [
    {"schema_translate_map": {None: "another_schema"}},
    {"isolation_level": "AUTOCOMMIT"},
])
async def test_option_engine_cannot_change_schema_or_remove_transaction(app, client, option):
    actor, preflight = await _owner_preflight(app, client)
    wrapped = app.state.engine.execution_options(**option)
    async with app.state.sessionmaker(bind=wrapped) as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(BadRequestError):
            await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_existing_connection_cannot_switch_schema_before_admission(app, client):
    actor, preflight = await _owner_preflight(app, client)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        await db.connection(execution_options={"schema_translate_map": {None: "another_schema"}})
        with pytest.raises((BadRequestError, ConflictError)):
            await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_same_option_engine_accepts_inside_existing_savepoint(app, client):
    actor, preflight = await _owner_preflight(app, client)
    async with app.state.sessionmaker(bind=_option_engine(app.state.engine)) as db:
        db.info["authenticated_actor"] = actor
        async with db.begin():
            async with db.begin_nested():
                assert await project_ai_config.accept_preflight_project_model(
                    db, preflight, cipher=app.state.cipher,
                ) == preflight.binding


async def test_preflight_rejects_transaction_switch_during_model_revalidation(app, client, monkeypatch):
    actor, preflight = await _owner_preflight(app, client)
    original = project_ai_config.resolve_project_model

    async def wrong_transaction(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        await db.commit()
        return result

    monkeypatch.setattr(project_ai_config, "resolve_project_model", wrong_transaction)
    async with app.state.sessionmaker(bind=_option_engine(app.state.engine)) as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(ConflictError):
            await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_replaced_pool_or_tampered_preflight_is_not_resurrected(app, client):
    actor, preflight = await _owner_preflight(app, client)
    with pytest.raises(BadRequestError):
        project_ai_config.require_original_preflight(replace(preflight))
    await app.state.engine.dispose()
    with pytest.raises(BadRequestError):
        project_ai_config.require_original_preflight(preflight)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        with pytest.raises(BadRequestError):
            await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)


async def test_task_admission_remains_single_use_with_option_engine(app, client, fake_llm):
    actor, preflight = await _owner_preflight(app, client)
    ctx = SimpleNamespace(settings=SimpleNamespace(allow_private_ai_endpoints=False, ai_request_timeout=5),
                          cipher=app.state.cipher, factory=fake_llm.factory)
    params = {"project_id": preflight.binding.project_id}
    proof = await prepare_task_admission(ctx, actor, "outline.continue", params, model_preflight=preflight)
    async with app.state.sessionmaker(bind=_option_engine(app.state.engine)) as db:
        db.info["authenticated_actor"] = actor
        chosen = await consume_task_admission(
            db, proof, actor=actor, kind="outline.continue", params=params, retry_of=None,
        )
        assert await project_ai_config.accept_preflight_project_model(db, chosen, cipher=app.state.cipher) == (
            preflight.binding
        )
        with pytest.raises(ConflictError):
            await consume_task_admission(db, proof, actor=actor, kind="outline.continue", params=params, retry_of=None)
        await db.rollback()


async def test_original_retry_proof_accepts_only_once_with_option_engine(app, fake_llm):
    async with _http_clients(app) as (owner, editor):
        project, actor, binding, source = await _retry_world(app, owner, editor)
        preflight = await project_ai_config.preflight_original_project_model(
            app.state.sessionmaker, actor, source, cipher=app.state.cipher,
        )
        ctx = SimpleNamespace(settings=SimpleNamespace(allow_private_ai_endpoints=False, ai_request_timeout=5),
                              cipher=app.state.cipher, factory=fake_llm.factory)
        proof = await prepare_task_admission(
            ctx, actor, source.kind, {}, model_preflight=preflight, retry_source=source,
        )
        async with app.state.sessionmaker(bind=_option_engine(app.state.engine)) as db:
            db.info["authenticated_actor"] = actor
            await project_access.require_project(db, actor.id, project["id"], "ai.use", for_update=True,
                                                 visibility_write=True, grant_identity=binding.grant_identity)
            source_task = await db.get(Task, source.task_id)
            chosen = await consume_task_admission(db, proof, actor=actor, kind=source.kind, params={},
                                                  retry_of=source_task)
            assert await project_ai_config.accept_original_preflight_project_model(
                db, chosen, admission=proof, cipher=app.state.cipher,
            ) == binding
            with pytest.raises(ConflictError):
                await project_ai_config.accept_original_preflight_project_model(
                    db, chosen, admission=proof, cipher=app.state.cipher,
                )
            await db.rollback()
