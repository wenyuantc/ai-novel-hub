"""Agent 出站必须验证入队时的模型实例，不能把复用的配置 ID 当作原模型。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.ai.providers import create_provider
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AgentConversation, AgentMessage, AIConfig, Project, Task
from app.services import ai_dispatch, project_ai_config
from app.services.agent import context
from app.services.ai_dispatch import VisibilityPolicyError, guard_provider, read_visibility_stamp
from app.services.task_engine.execution_identity import from_queued_task
from tests.test_agent_runtime import setup_agent
from tests.test_ai_visibility_dispatch import _server, _sse


async def _original_turn(app, client, url):
    project, _, _ = await setup_agent(client)
    actor = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
    assert actor is not None
    async with app.state.sessionmaker() as db:
        conversation = await db.scalar(select(AgentConversation))
        config = await db.scalar(select(AIConfig))
        config.base_url = f"{url}/v1/"
        await db.commit()
        binding = await project_ai_config.resolve_project_model(
            db, actor[0], project["id"], "text", cipher=app.state.cipher, allow_private=True,
        )
        message = AgentMessage(conversation_id=conversation.id, role="user", content="原始轮次")
        db.add(message)
        await db.flush()
        task = Task(
            user_id=actor[0], project_id=project["id"], kind="agent.turn", status="queued",
            queued_at=datetime.now(UTC), step_cursor=0,
            input={"request": {"conversation_id": conversation.id, "project_id": project["id"]},
                   "message_id": message.id, "ai_config_id": config.id, "history_epoch": conversation.ai_history_epoch,
                   "_agent_actor_token_version": actor[1], "_task_actor_token_version": actor[1],
                   "_project_model": project_ai_config.serialize_project_model(binding)},
        )
        db.add(task)
        await db.flush()
        message.turn_task_id, message.turn_task_key = task.id, task.statistics_key
        execution_key = str(uuid4())
        original = from_queued_task(task, execution_key)
        task.status, task.execution_key = "running", execution_key
        execution = SimpleNamespace(
            task_id=task.id, user_id=actor[0], project_id=project["id"], conversation_id=conversation.id,
            input_message_id=message.id, task_key=task.statistics_key, queued_at=task.queued_at,
            execution_key=execution_key, actor_token_version=actor[1], task_execution_identity=original,
            model_binding=binding, cipher=app.state.cipher, allow_private=True,
        )
        await db.commit()
        return execution, config.id


async def _guarded(app, execution, config_id, *, wrong_key=False):
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, execution.conversation_id)
        project = await db.get(Project, execution.project_id)
        config = await db.get(AIConfig, config_id)
        key = "different-key" if wrong_key else app.state.cipher.decrypt(config.api_key_enc)
        provider = ai_dispatch.chapter_provider_factory(create_provider)(
            config.provider, key, config.base_url, 3,
        )
        provider.follow_redirects = True
        guarded = await context.guard_agent_provider(db, conversation, project, config, provider, execution=execution)
        return guarded, config.model


async def test_reused_config_integer_id_does_not_authorize_old_turn(app, client):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        execution, config_id = await _original_turn(app, client, url)
        guarded, model = await _guarded(app, execution, config_id)
        async with app.state.sessionmaker() as db:
            config = await db.get(AIConfig, config_id)
            fields = ("user_id", "name", "model_type", "provider", "base_url", "api_key_enc", "model",
                      "thinking_level", "context_window_k", "max_output_k", "is_default")
            values = {name: getattr(config, name) for name in fields}
            old_key = config.instance_key
            await db.delete(config)
            await db.flush()
            replacement = AIConfig(id=config_id, **values)
            db.add(replacement)
            await db.commit()
            assert replacement.instance_key != old_key
        with pytest.raises(VisibilityPolicyError):
            _ = [part async for part in guarded.generate_stream([{"role": "user", "content": "private"}], model)]
        assert requests == []


async def test_original_agent_model_binding_sends_with_real_sdk(app, client):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        execution, config_id = await _original_turn(app, client, url)
        guarded, model = await _guarded(app, execution, config_id)
        parts = [part async for part in guarded.generate_stream([{"role": "user", "content": "写作"}], model)]
        assert parts == ["ok"]
        assert len(requests) == 1


async def test_provider_with_different_credentials_cannot_claim_original_binding(app, client):
    async with _server(payload=_sse("openai")) as (url, _received, _headers, _body, requests):
        execution, config_id = await _original_turn(app, client, url)
        with pytest.raises(VisibilityPolicyError):
            await _guarded(app, execution, config_id, wrong_key=True)
        assert requests == []


async def test_preexisting_preflight_callback_cannot_be_dropped(app, client):
    async with _server(payload=_sse("openai")) as (url, _received, _headers, _body, requests):
        execution, config_id = await _original_turn(app, client, url)

        async def preflight(_db):
            raise VisibilityPolicyError("必须保留的验证")

        async with app.state.sessionmaker() as db:
            conversation = await db.get(AgentConversation, execution.conversation_id)
            project = await db.get(Project, execution.project_id)
            config = await db.get(AIConfig, config_id)
            provider = ai_dispatch.chapter_provider_factory(create_provider)(
                config.provider, app.state.cipher.decrypt(config.api_key_enc), config.base_url, 3,
            )
            provider.follow_redirects = True
            stamp = await read_visibility_stamp(db, project.id)
            protected = guard_provider(provider, stamp, app.state.sessionmaker, verify_preflight=preflight)
            with pytest.raises(context.SummaryExecutionChanged):
                await context.guard_agent_provider(db, conversation, project, config, protected, execution=execution)
        assert requests == []


async def test_model_binding_database_failure_does_not_expose_query_or_parameters(app, client, monkeypatch):
    secret = "private-model-key-and-prompt"
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        execution, config_id = await _original_turn(app, client, url)
        guarded, model = await _guarded(app, execution, config_id)

        async def failure(*_args, **_kwargs):
            raise DBAPIError(f"SELECT {secret}", {"credential": secret}, RuntimeError(secret))

        monkeypatch.setattr(project_ai_config, "revalidate_project_model", failure)
        with pytest.raises(context.SummaryExecutionChanged) as caught:
            _ = [part async for part in guarded.generate_stream([{"role": "user", "content": "private"}], model)]
        assert caught.value.__context__ is None and secret not in str(caught.value)
        assert requests == []
