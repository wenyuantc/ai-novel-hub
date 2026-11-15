"""执行体的原令牌、任务输入和批量 attempt 必须贯穿真实的模型发送。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.ai.providers import create_provider
from app.ai.providers.dispatch_hooks import DispatchHooks
from app.models import AIConfig, Chapter, Project, ProjectCollaborator, Task, User
from app.services import chapter_claim, project_ai_config
from app.services import collaboration_ai_guard as guard
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp
from app.services.collaboration_task_access import bind_task_grant
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.stream_evidence import TerminalEvidence, capture_task_view
from app.services.wizard_service import ResumeBinding, WizardResumeChanged, bind_resume_source
from tests.test_ai_visibility_dispatch import _server, _sse


@pytest.fixture
async def world(app):
    async with app.state.sessionmaker() as db:
        owner = User(username="task-guard-owner", password_hash="test")
        actor = User(username="task-guard-editor", password_hash="test")
        db.add_all([owner, actor])
        await db.flush()
        project = Project(user_id=owner.id, title="测试作品")
        db.add(project)
        await db.flush()
        member = ProjectCollaborator(project_id=project.id, user_id=actor.id, status="accepted", role="editor",
                                     allow_owner_ai=True)
        config = AIConfig(user_id=owner.id, name="测试", provider="openai_compat", model="test-model",
                          model_type="text", is_default=True, api_key_enc=app.state.cipher.encrypt("test-key"))
        db.add_all([member, config])
        await db.commit()
        return {"owner": owner.id, "actor": actor.id, "project": project.id,
                "config": config.id, "token": actor.token_version}


async def _binding(app, world, url):
    async with app.state.sessionmaker() as db:
        cfg = await db.get(AIConfig, world["config"])
        cfg.base_url = f"{url}/v1/"
        await db.commit()
        binding = await project_ai_config.resolve_project_model(
            db, world["actor"], world["project"], "text", cipher=app.state.cipher, allow_private=True,
        )
        stamp = await read_visibility_stamp(db, world["project"])
        return binding, stamp


async def _queued_execution(app, world, binding, *, batch=False, embedding=None):
    async with app.state.sessionmaker() as db:
        chapter = None
        if batch:
            chapter = Chapter(project_id=world["project"], title="第一章", content="", status="draft")
            db.add(chapter)
            await db.flush()
        payload = bind_task_grant({"request": {"project_id": world["project"]}}, binding.grant_identity)
        payload["_project_model"] = project_ai_config.serialize_project_model(binding)
        payload["_task_actor_token_version"] = world["token"]
        if embedding is not None:
            payload["_shared_embedding"] = project_ai_config.serialize_shared_embedding(embedding)
        if batch:
            payload["chapter_ids"] = [chapter.id]
        initial = {"phase": "generate", "current": None, "items": [{"chapter_id": chapter.id}]} if batch else None
        task = Task(user_id=world["actor"], project_id=world["project"],
                    kind="chapter.batch" if batch else "outline.continue",
                    resource_key=f"batch:{world['project']}" if batch else None, status="queued",
                    statistics_key=str(uuid4()), queued_at=datetime.now(UTC), step_cursor=0,
                    input=payload, checkpoint=initial)
        db.add(task)
        await db.flush()
        execution_key = str(uuid4())
        identity = from_queued_task(task, execution_key)
        task.status, task.execution_key = "running", execution_key
        await db.flush()
        attempt = None
        if batch:
            key = str(uuid4())
            checkpoint = deepcopy(task.checkpoint)
            checkpoint["_batch_attempt"] = key
            task.checkpoint = checkpoint
            await db.flush()
            claim = await chapter_claim.claim_batch_chapter(
                db, task_id=task.id, task_key=task.statistics_key, actor_id=task.user_id,
                project_id=task.project_id, chapter_id=chapter.id, queued_at=task.queued_at,
                grant_identity=binding.grant_identity, execution_key=execution_key,
            )
            attempt = guard.BatchAttemptIdentity(claim, 0, key)
        await db.commit()
        return identity, attempt


async def _send(bound):
    return [part async for part in bound.provider.generate_stream(
        [{"role": "user", "content": "private task content"}], bound.model,
    )]


async def test_running_orm_task_is_not_a_proof_of_original_execution(app, world):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        binding, stamp = await _binding(app, world, url)
        identity, _ = await _queued_execution(app, world, binding)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, identity.task_id)
            with pytest.raises(guard.CollaborationSendBlocked):
                guard.for_task(task, binding, stamp, cipher=app.state.cipher, allow_private=True)
        assert requests == []


@pytest.mark.parametrize("change", ["task_key", "execution", "input", "cursor", "checkpoint", "token",
                                     "reinvite", "model", "disabled"])
async def test_original_execution_is_revalidated_before_every_real_send(app, world, change):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        binding, stamp = await _binding(app, world, url)
        identity, _ = await _queued_execution(app, world, binding)
        checks = guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        assert await _send(bound)
        assert len(requests) == 1
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, identity.task_id)
            if change == "task_key":
                task.statistics_key = str(uuid4())
            elif change == "execution":
                task.execution_key = str(uuid4())
            elif change == "input":
                task.input = {**task.input, "request": {"project_id": world["project"], "changed": True}}
            elif change == "cursor":
                task.step_cursor = 1
            elif change == "checkpoint":
                task.checkpoint = {"replaced": True}
            elif change == "token":
                (await db.get(User, world["actor"])).token_version += 1
            elif change == "disabled":
                (await db.get(User, world["actor"])).is_active = False
            elif change == "model":
                (await db.get(AIConfig, world["config"])).model = "different-model"
            else:
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.user_id == world["actor"], ProjectCollaborator.project_id == world["project"],
                ))
                member.invitation_id, member.revision = str(uuid4()), member.revision + 1
            await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await _send(bound)
        assert len(requests) == 1


async def test_redirect_rechecks_original_task_before_second_physical_request(app, world, monkeypatch):
    original = DispatchHooks._after_headers
    current = None

    async def replace_after_headers(hooks, response):
        await original(hooks, response)
        async with app.state.sessionmaker() as db:
            (await db.get(Task, current.task_id)).execution_key = str(uuid4())
            await db.commit()

    monkeypatch.setattr(DispatchHooks, "_after_headers", replace_after_headers)
    async with _server(status="307 Temporary Redirect", payload=b"", headers={"Location": "/fallback"}) as (
        url, _received, headers, body, requests,
    ):
        headers.set()
        body.set()
        binding, stamp = await _binding(app, world, url)
        current, _ = await _queued_execution(app, world, binding)
        checks = guard.for_task(current, binding, stamp, cipher=app.state.cipher, allow_private=True)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        with pytest.raises(guard.CollaborationSendBlocked):
            await _send(bound)
        assert len(requests) == 1


@pytest.mark.parametrize("mode", ["keyword", "vector"])
async def test_text_and_original_embedding_selection_share_send_and_write_fence(app, world, mode):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        binding, stamp = await _binding(app, world, url)
        async with app.state.sessionmaker() as db:
            if mode == "vector":
                db.add(AIConfig(user_id=world["owner"], name="向量", provider="openai_compat",
                                model="embedding-v1", model_type="embedding", is_default=True,
                                base_url=f"{url}/v1/", api_key_enc=app.state.cipher.encrypt("embedding-key")))
                await db.commit()
            embedding = await project_ai_config.resolve_shared_embedding(
                db, world["actor"], world["project"], cipher=app.state.cipher, allow_private=True,
            )
            assert embedding.mode == mode
        identity, _ = await _queued_execution(app, world, binding, embedding=embedding)
        with pytest.raises(guard.CollaborationSendBlocked):
            guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True)
        checks = guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True,
                                extra_bindings=(embedding,))
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        await bound.provider.preflight()
        assert await _send(bound)
        async with app.state.sessionmaker() as db:
            await checks.require_task_write(db)
            (await db.get(Task, identity.task_id)).output_text = "已通过原身份验证"
            await db.commit()
        async with app.state.sessionmaker() as db:
            if mode == "vector":
                cfg = await db.get(AIConfig, embedding.model.config_id)
                cfg.model = "embedding-v2"
            else:
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.user_id == world["actor"], ProjectCollaborator.project_id == world["project"],
                ))
                member.invitation_id, member.revision = str(uuid4()), member.revision + 1
            await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await bound.provider.preflight()
        with pytest.raises(VisibilityPolicyError):
            await _send(bound)
        async with app.state.sessionmaker() as db:
            with pytest.raises(guard.CollaborationSendBlocked):
                await checks.require_task_write(db)
            task = await db.get(Task, identity.task_id)
            assert task.output_text == "已通过原身份验证"
        assert len(requests) == 1


async def test_batch_write_barrier_checks_attempt_before_result_can_commit(app, world):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        binding, stamp = await _binding(app, world, url)
        identity, attempt = await _queued_execution(app, world, binding, batch=True)
        checks = guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True,
                                chapter=attempt.claim, batch_attempt=attempt)
        async with app.state.sessionmaker() as db:
            await checks.require_task_write(db)
            await db.rollback()
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, identity.task_id)
            task.checkpoint = {**task.checkpoint, "_batch_attempt": str(uuid4())}
            await db.commit()
        async with app.state.sessionmaker() as db:
            with pytest.raises(guard.CollaborationSendBlocked):
                await checks.require_task_write(db)
            assert (await db.get(Task, identity.task_id)).output_text == ""
        assert requests == []


async def test_embedding_preflight_rechecks_original_text_binding(app, world):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        binding, stamp = await _binding(app, world, url)
        async with app.state.sessionmaker() as db:
            db.add(AIConfig(user_id=world["owner"], name="向量", provider="openai_compat",
                            model="embedding-v1", model_type="embedding", is_default=True,
                            base_url=f"{url}/v1/", api_key_enc=app.state.cipher.encrypt("embedding-key")))
            await db.commit()
            selection = await project_ai_config.resolve_shared_embedding(
                db, world["actor"], world["project"], cipher=app.state.cipher, allow_private=True,
            )
        identity, _ = await _queued_execution(app, world, binding, embedding=selection)
        with pytest.raises(guard.CollaborationSendBlocked):
            guard.for_task(identity, selection, stamp, cipher=app.state.cipher, allow_private=True)
        checks = guard.for_task(identity, selection, stamp, cipher=app.state.cipher, allow_private=True,
                                extra_bindings=(binding,))
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        await bound.provider.preflight()
        async with app.state.sessionmaker() as db:
            (await db.get(AIConfig, world["config"])).model = "replaced-text-model"
            await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await bound.provider.preflight()
        async with app.state.sessionmaker() as db:
            with pytest.raises(guard.CollaborationSendBlocked):
                await checks.require_task_write(db)
        assert requests == []


async def test_task_output_read_guard_requires_real_frame_lease(app, world):
    async with _server(payload=_sse("openai")) as (url, _received, _headers, _body, requests):
        binding, stamp = await _binding(app, world, url)
        identity, _ = await _queued_execution(app, world, binding)
        checks = guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True)
        async with app.state.sessionmaker() as db:
            with pytest.raises(guard.CollaborationSendBlocked):
                await checks.require_task_read(db)
            forged = TerminalEvidence(identity, capture_task_view(await db.get(Task, identity.task_id)))
            with pytest.raises(guard.CollaborationSendBlocked):
                await checks.require_terminal_read(db, forged)
        assert requests == []


async def test_resume_source_is_rechecked_in_send_and_write_transaction(app, world):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        await _binding(app, world, url)
        async with app.state.sessionmaker() as db:
            actor = await db.get(User, world["owner"])
            binding = await project_ai_config.resolve_project_model(
                db, actor.id, world["project"], "text", cipher=app.state.cipher, allow_private=True,
            )
            stamp = await read_visibility_stamp(db, world["project"])
            source = Task(
                user_id=actor.id, project_id=world["project"], kind="wizard.outline", status="failed",
                statistics_key=str(uuid4()), queued_at=datetime.now(UTC), step_cursor=0,
                input={"request": {"project_id": world["project"]}, "ai_history_epoch": stamp.epoch,
                       "_task_actor_token_version": actor.token_version,
                       "_wizard_actor_token_version": actor.token_version},
                output_text='{"id": 1}',
            )
            db.add(source)
            await db.flush()
            await db.refresh(source)
            raw_resume = bind_resume_source(source)
            resume = ResumeBinding(actor.id, world["project"], "outline", source.id, source.statistics_key,
                                   raw_resume["fingerprint"], actor.token_version)
            payload = {
                "request": {"project_id": world["project"], "continue_from_task_id": source.id,
                            "continue_from_task_key": source.statistics_key},
                "_wizard_resume": raw_resume, "_task_actor_token_version": actor.token_version,
                "_wizard_actor_token_version": actor.token_version,
                "_project_model": project_ai_config.serialize_project_model(binding),
            }
            task = Task(user_id=actor.id, project_id=world["project"], kind="wizard.outline", status="queued",
                        statistics_key=str(uuid4()), queued_at=datetime.now(UTC), step_cursor=0, input=payload)
            db.add(task)
            await db.flush()
            execution_key = str(uuid4())
            identity = from_queued_task(task, execution_key)
            task.status, task.execution_key = "running", execution_key
            await db.commit()
        with pytest.raises(guard.CollaborationSendBlocked):
            guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True)
        checks = guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True, resume=resume)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        assert await _send(bound)
        async with app.state.sessionmaker() as db:
            (await db.get(Task, source.id)).output_text = '{"id": 2}'
            await db.commit()
        with pytest.raises(WizardResumeChanged):
            await bound.provider.preflight()
        with pytest.raises(WizardResumeChanged):
            await _send(bound)
        async with app.state.sessionmaker() as db:
            with pytest.raises(WizardResumeChanged):
                await checks.require_task_write(db)
            assert (await db.get(Task, identity.task_id)).output_text == ""
        with pytest.raises(WizardResumeChanged):
            await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        assert len(requests) == 1


async def test_missing_original_token_cannot_be_reissued_from_current_user(app, world):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        binding, stamp = await _binding(app, world, url)
        identity, _ = await _queued_execution(app, world, binding)
        with pytest.raises(guard.CollaborationSendBlocked):
            guard.for_task(replace(identity, actor_token_version=None), binding, stamp,
                           cipher=app.state.cipher, allow_private=True)
        assert requests == []


@pytest.mark.parametrize("change", ["attempt", "cursor", "claim", "input", "uncertain"])
async def test_batch_attempt_cannot_be_reissued_from_new_checkpoint(app, world, change):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        binding, stamp = await _binding(app, world, url)
        identity, attempt = await _queued_execution(app, world, binding, batch=True)
        checks = guard.for_task(identity, binding, stamp, cipher=app.state.cipher, allow_private=True,
                                chapter=attempt.claim, batch_attempt=attempt)
        bound = await checks.prepare_provider(app.state.sessionmaker, create_provider, request_timeout=2)
        assert await _send(bound)
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, identity.task_id)
            if change == "input":
                task.input = {**task.input, "chapter_ids": [attempt.claim.chapter_id + 1]}
            elif change == "cursor":
                task.step_cursor = 1
            elif change == "attempt":
                task.checkpoint = {**task.checkpoint, "_batch_attempt": str(uuid4())}
            elif change == "uncertain":
                task.checkpoint = {**task.checkpoint, "_batch_outcome_unknown": {}}
            else:
                chapter = await db.get(Chapter, attempt.claim.chapter_id)
                chapter.generation_claim_key = str(uuid4())
            await db.commit()
        with pytest.raises(VisibilityPolicyError):
            await _send(bound)
        assert len(requests) == 1
