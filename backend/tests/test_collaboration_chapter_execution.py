"""单章和场景任务的原模型、操作者及邀请轮次。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.providers import AIProviderError, create_provider
from app.models import AIConfig, Chapter, ChapterVersion, GenerationLog, ProjectCollaborator, Task, User, WritingStyle
from app.services import chapter_claim, generation_service, project_ai_config
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.handlers.chapter import ChapterHandler, bind_queued_claim, capture_queued_claim
from tests.helpers import create_ai_config, create_project, register, stream_task
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_analysis import analysis_json
from tests.test_collaboration_rewrite_identity import _http_clients, _world


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True}


async def test_author_queued_task_keeps_initial_model_after_default_changes(app, fake_llm):
    await app.state.task_engine.stop()
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-model-owner")
        project = await create_project(owner)
        first = await create_ai_config(owner, name="初始模型", model="initial-model", is_default=True)
        second = await create_ai_config(owner, name="后续模型", model="changed-model", is_default=False)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        assert chapter.status_code == 201, chapter.text
        queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert queued.status_code == 202, queued.text
        changed = await owner.patch(f"/api/v1/ai-configs/{second['id']}", json={"is_default": True})
        assert changed.status_code == 200, changed.text
        fake_llm.push("生成正文")
        await app.state.task_engine.start()
        events = await stream_task(owner, queued.json())
        assert events[-1][1]["task"]["status"] == "succeeded", events
        assert len(fake_llm.calls) == 1 and fake_llm.calls[0]["model"] == "initial-model"
        async with app.state.sessionmaker() as db:
            log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == queued.json()["id"]))
            assert log.ai_config_id == first["id"]


async def test_editor_chapter_task_is_attributed_to_real_actor(app):
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            app.state.task_engine.ctx.factory = create_provider
            project, chapter, editor_user = await _world(owner, editor, url)
            response = await editor.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
            assert response.status_code == 202, response.text
            task = response.json()
            # 协作者 SSE 订阅自 E5-f 起经受控管线开放（见 test_task_sse_scope / test_collaboration_stream_*）；
            # 此处仍经本人 + 当前轮次的任务详情观察终态，并在数据库核对保存结果与归属，不放宽任何结果断言。
            async with asyncio.timeout(10):
                while True:
                    detail = await editor.get(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
                    assert detail.status_code == 200, detail.text
                    if detail.json()["status"] not in ("queued", "running"):
                        break
                    await asyncio.sleep(0.02)
            assert detail.json()["status"] == "succeeded", detail.text
            assert len(requests) == 1
            async with asyncio.timeout(10):
                while True:
                    async with app.state.sessionmaker() as db:
                        row = await db.get(Task, task["id"])
                        if row.status == "succeeded" and row.execution_key is None:
                            log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == row.id))
                            assert row.user_id == editor_user["id"] and row.project_id == project["id"]
                            assert log.actor_user_id == editor_user["id"] and log.status == "success"
                            assert log.model_source == "actor" and log.model_config_owner_id == editor_user["id"]
                            saved = await db.get(Chapter, chapter["id"])
                            assert saved.status == "done" and saved.content != "旧正文示例"
                            assert saved.generation_claim_key is None
                            break
                    await asyncio.sleep(0.02)


async def test_editor_queued_claim_keeps_original_invitation_identity(app):
    await app.state.task_engine.stop()
    async with (
        _server(payload=_sse("openai")) as (url, _received, _headers, _body, _requests),
        _http_clients(app) as (owner, editor),
    ):
        project, chapter, actor = await _world(owner, editor, url)
        async with app.state.sessionmaker() as db:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == actor["id"],
            ))
            grant = (member.invitation_id, member.revision)
            user = await db.get(User, actor["id"])
            task = Task(
                user_id=actor["id"], project_id=project["id"], chapter_id=chapter["id"], kind="chapter.generate",
                resource_key=f"chapter:{chapter['id']}", status="queued", input={
                    "chapter_id": chapter["id"], "prev_status": chapter["status"],
                    "request": {"chapter_id": chapter["id"]},
                    "_collaboration_grant": {"invitation_id": grant[0], "revision": grant[1]},
                    "_task_actor_token_version": user.token_version,
                },
            )
            db.add(task)
            await db.flush()
            claim = await bind_queued_claim(db, task)
            assert claim.grant_identity == grant and capture_queued_claim(task) == claim
            await db.commit()
        async with app.state.sessionmaker() as db:
            assert await chapter_claim.release_chapter_claim(db, claim)
            await db.commit()


async def test_editor_ai_chapter_projection_hides_owner_style_even_for_admin(app):
    async with (
        _server(payload=_sse("openai")) as (url, _received, _headers, _body, _requests),
        _http_clients(app) as (owner, editor),
    ):
        project, chapter, actor = await _world(owner, editor, url)
        owner_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        async with app.state.sessionmaker() as db:
            (await db.get(User, actor["id"])).is_admin = True
            style = WritingStyle(user_id=owner_id, name="作者私有风格")
            db.add(style)
            await db.flush()
            row = await db.get(Chapter, chapter["id"])
            row.style_id = style.id
            await db.commit()
            style_id = style.id
        async with app.state.sessionmaker() as db:
            row = await db.get(Chapter, chapter["id"])
            row.title = "调用方尚未提交"
            output = await generation_service.chapter_ai_view(db, actor["id"], project["id"], row)
            assert output.style_id is None and output.title == "调用方尚未提交"
            assert row.style_id == style_id and db.is_modified(row)
        async with app.state.sessionmaker() as db:
            row = await db.get(Chapter, chapter["id"])
            output = await generation_service.chapter_ai_view(db, owner_id, project["id"], row)
            assert output.style_id == style_id


@pytest.mark.parametrize("change", ["token", "role", "owner_ai", "revoked", "replaced_task", "missing_identity"])
async def test_editor_identity_loss_never_writes_prompt_or_owner_model(app, fake_llm, change):
    await app.state.task_engine.stop()
    async with (
        _server(payload=_sse("openai")) as (url, _received, _headers, _body, _requests),
        _http_clients(app) as (owner, editor),
    ):
        project, chapter, actor = await _world(owner, editor, url, source="owner")
        scenes = await owner.get(f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}/scenes")
        scene_id = scenes.json()["items"][0]["id"]
        async with app.state.sessionmaker() as db:
            user = await db.get(User, actor["id"])
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == actor["id"],
            ))
            grant = (member.invitation_id, member.revision)
            binding = await project_ai_config.resolve_project_model(
                db, actor["id"], project["id"], "text", cipher=app.state.cipher, allow_private=True, for_update=True,
            )
            embedding = await project_ai_config.resolve_shared_embedding(
                db, actor["id"], project["id"], cipher=app.state.cipher, allow_private=True, for_update=True,
            )
            task = Task(
                user_id=actor["id"], project_id=project["id"], chapter_id=chapter["id"], kind="chapter.generate",
                title="编辑者任务", resource_key=f"chapter:{chapter['id']}", status="queued", input={
                    "chapter_id": chapter["id"], "prev_status": chapter["status"],
                    "request": {"chapter_id": chapter["id"]},
                    "_collaboration_grant": {"invitation_id": grant[0], "revision": grant[1]},
                    "_task_actor_token_version": user.token_version,
                    "_project_model": project_ai_config.serialize_project_model(binding),
                    "_shared_embedding": project_ai_config.serialize_shared_embedding(embedding),
                },
            )
            db.add(task)
            await db.flush()
            queued_claim = await chapter_claim.claim_queued_chapter(
                db, task_id=task.id, task_key=task.statistics_key, actor_id=actor["id"], project_id=project["id"],
                chapter_id=chapter["id"], queued_at=task.queued_at, grant_identity=grant,
            )
            execution_key = str(uuid4())
            execution = from_queued_task(task, execution_key)
            task.status, task.execution_key = "running", execution_key
            await db.flush()
            claim = await chapter_claim.activate_queued_chapter(db, queued_claim, execution_key=execution_key)
            await db.commit()
        if change == "token":
            changed = await editor.post("/api/v1/auth/change-password", json={
                "old_password": "secret123", "new_password": "new-secret123",
            })
            assert changed.status_code == 204, changed.text
        elif change == "replaced_task":
            async with app.state.sessionmaker() as db:
                await db.execute(update(Task).where(Task.id == execution.task_id).values(statistics_key=str(uuid4())))
                await db.commit()
        elif change in ("revoked", "missing_identity"):
            changed = await owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}",
                params={"expected_revision": grant[1]},
            )
            assert changed.status_code == 204, changed.text
        else:
            body = {"expected_revision": grant[1], **({"role": "viewer"} if change == "role"
                                                       else {"allow_owner_ai": False})}
            changed = await owner.patch(f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}", json=body)
            assert changed.status_code == 200, changed.text
        secret = "private-owner-prompt-and-provider-error"
        fake_llm.push(AIProviderError(secret))
        provider = fake_llm.factory("openai_compat", "private-owner-key", "https://example.test/v1/", 2)
        scene = {"id": scene_id, "sort": 0, "content": chapter["content"], "separator_before": ""}
        job = generation_service.ChapterJob(
            chapter_id=chapter["id"], prev_status=claim.prev_status, provider=provider, model="owner-private-model",
            messages=[{"role": "user", "content": secret}], project_id=project["id"], user_id=actor["id"],
            task_id=execution.task_id, task_key=execution.task_key, chapter_claim=claim, execution=execution,
            model_binding=binding, embedding_selection=embedding, ai_config_id=binding.config_id,
            provider_name="openai_compat", params={"instruction": secret},
            scenes=[scene], steps=[scene],
        )
        if change == "missing_identity":
            job.execution = job.chapter_claim = job.model_binding = job.embedding_selection = None
        events = [event async for event in generation_service.run_chapter_job(job, app.state.sessionmaker)]
        assert len(fake_llm.calls) == 1 and events[-1][0] == "error" and events[-1][1]["detail"] == secret
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(GenerationLog.id).where(GenerationLog.task_id == execution.task_id)) is None
            saved = await db.get(Chapter, chapter["id"])
            if change in ("replaced_task", "missing_identity"):
                assert saved.generation_claim_key == claim.claim_key and saved.status == "generating"
            else:
                assert saved.generation_claim_key is None and saved.status == claim.prev_status


@pytest.mark.parametrize("change", ["password", "model"])
async def test_author_change_after_headers_prevents_old_task_save(app, change):
    async with (
        _server(payload=_sse("openai")) as (url, received, headers, body, requests),
        _http_clients(app) as (owner, _unused),
    ):
        app.state.task_engine.ctx.factory = create_provider
        await register(owner, "chapter-token-owner")
        project = await create_project(owner)
        config = await create_ai_config(owner, base_url=f"{url}/v1/", model="initial-model")
        chapter = await owner.post(
            f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节", "content": "原始正文"},
        )
        queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert queued.status_code == 202, queued.text
        task = queued.json()
        try:
            await asyncio.wait_for(received.wait(), 10)
            running = app.state.task_engine._running.get(task["id"])
            assert running is not None
            headers.set()
            if change == "password":
                changed = await owner.post("/api/v1/auth/change-password", json={
                    "old_password": "secret123", "new_password": "new-secret123",
                })
                assert changed.status_code == 204, changed.text
            else:
                changed = await owner.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "changed-model"})
                assert changed.status_code == 200, changed.text
            body.set()
            await asyncio.wait_for(asyncio.shield(running), 10)
            async with app.state.sessionmaker() as db:
                saved = await db.get(Chapter, chapter.json()["id"])
                original = await db.get(Task, task["id"])
                success = await db.scalar(select(GenerationLog).where(
                    GenerationLog.task_id == task["id"], GenerationLog.status == "success",
                ))
                assert saved.content == "原始正文" and original.status != "succeeded"
                assert success is None
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()


@pytest.mark.parametrize("damage", ["none", "wrong_task", "boolean_actor", "input", "result", "version", "claim"])
async def test_success_receipt_binds_original_execution_before_auto_analysis(app, fake_llm, monkeypatch, damage):
    reached = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def wait_before_analysis(_self, _ctx, _task, *, execution):
        reached.set_result(execution)
        await release.wait()

    monkeypatch.setattr(ChapterHandler, "on_success", wait_before_analysis)
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-auto-proof-owner")
        project = await create_project(owner)
        await create_ai_config(owner)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        fake_llm.push("已完成正文")
        queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert queued.status_code == 202, queued.text
        try:
            execution = await asyncio.wait_for(reached, 10)
            running = app.state.task_engine._running.get(queued.json()["id"])
            assert running is not None
            if damage == "wrong_task":
                execution = replace(execution, task_id=execution.task_id + 1)
            elif damage == "boolean_actor":
                execution = replace(execution, actor_id=True)
            elif damage != "none":
                async with app.state.sessionmaker() as db:
                    task = await db.get(Task, execution.task_id)
                    if damage == "input":
                        task.input = {**task.input, "chapter_id": task.chapter_id + 1}
                    elif damage == "result":
                        task.result = {**task.result, "warning": "伪造"}
                    elif damage == "claim":
                        checkpoint = deepcopy(task.checkpoint)
                        checkpoint["_chapter_claim"]["claim_key"] = "00000000-0000-0000-0000-000000000000"
                        task.checkpoint = checkpoint
                    else:
                        version = await db.get(ChapterVersion, task.checkpoint["_chapter_claim"]["version_id"])
                        version.content = "伪造版本"
                    await db.commit()
            async with app.state.sessionmaker() as db:
                receipt = await generation_service.completed_success_chapter_receipt(db, execution)
                assert (receipt is not None) == (damage == "none")
        finally:
            release.set()
            if queued.json()["id"] in app.state.task_engine._running:
                await asyncio.wait_for(asyncio.shield(app.state.task_engine._running[queued.json()["id"]]), 10)


async def test_running_completion_event_requires_original_receipt_and_result(app, fake_llm, monkeypatch):
    reached = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    save = generation_service._save_chapter_result

    async def paused(job, *args):
        result = await save(job, *args)
        reached.set_result((job.execution, result))
        await release.wait()
        return result

    monkeypatch.setattr(generation_service, "_save_chapter_result", paused)
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-running-proof-owner")
        project = await create_project(owner)
        await create_ai_config(owner)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        fake_llm.push("已提交正文")
        queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert queued.status_code == 202, queued.text
        try:
            execution, result = await asyncio.wait_for(reached, 10)
            async with app.state.sessionmaker() as db:
                assert await generation_service.completed_running_chapter_receipt(db, execution, result) is not None
            async with app.state.sessionmaker() as db:
                assert await generation_service.completed_running_chapter_receipt(
                    db, execution, {**result, "warning": "伪造"},
                ) is None
            operation = str(uuid4())
            try:
                async with app.state.sessionmaker() as db:
                    await db.execute(update(Task).where(Task.id == execution.task_id).values(
                        admin_cancel_operation_id=operation, admin_cancel_requested_at=datetime.now(UTC),
                    ))
                    await db.commit()
                async with app.state.sessionmaker() as db:
                    assert await generation_service.completed_running_chapter_receipt(db, execution, result) is None
                    assert await generation_service.completed_admin_race_chapter_receipt(
                        db, execution, result, operation_id=operation,
                    ) is not None
                    assert await generation_service.completed_admin_race_chapter_receipt(
                        db, execution, result, operation_id=str(uuid4()),
                    ) is None
                    assert await generation_service.completed_admin_race_chapter_receipt(
                        db, execution, result, operation_id=True,
                    ) is None
                async with app.state.sessionmaker() as db:
                    task = await db.get(Task, execution.task_id)
                    task.input = {**task.input, "_admin_chapter_cancel": {"operation_id": operation}}
                    await db.commit()
                async with app.state.sessionmaker() as db:
                    assert await generation_service.completed_admin_race_chapter_receipt(
                        db, execution, result, operation_id=operation,
                    ) is None
            finally:
                async with app.state.sessionmaker() as db:
                    task = await db.get(Task, execution.task_id)
                    task.input = {key: value for key, value in task.input.items() if key != "_admin_chapter_cancel"}
                    task.admin_cancel_operation_id = None
                    task.admin_cancel_requested_at = None
                    await db.commit()
        finally:
            release.set()
            running = app.state.task_engine._running.get(queued.json()["id"])
            if running is not None:
                await asyncio.wait_for(asyncio.shield(running), 10)


async def test_mutated_transport_before_first_guard_never_sends(app, monkeypatch):
    reached = asyncio.get_running_loop().create_future()
    release = asyncio.Event()
    original = generation_service._prepare_scene_step

    async def paused(db, job, *args, **kwargs):
        reached.set_result(job.raw_provider)
        await release.wait()
        return await original(db, job, *args, **kwargs)

    monkeypatch.setattr(generation_service, "_prepare_scene_step", paused)
    async with (
        _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
        _http_clients(app) as (owner, _unused),
    ):
        app.state.task_engine.ctx.factory = create_provider
        headers.set()
        body.set()
        await register(owner, "chapter-transport-before-guard")
        project = await create_project(owner)
        await create_ai_config(owner, base_url=f"{url}/v1/")
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert queued.status_code == 202, queued.text
        try:
            provider = await asyncio.wait_for(reached, 10)
            provider.api_key = "changed-key"
            provider._provider.api_key = "changed-key"
            release.set()
            events = await stream_task(owner, queued.json())
            assert events[-1][1]["task"]["status"] == "failed", events
            assert not requests
        finally:
            release.set()


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True, "auto_analyze_enabled": True}])
async def test_author_polish_automatic_analysis_inherits_original_identity(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        actor = await register(owner, "chapter-polish-auto-owner")
        project = await create_project(owner)
        await create_ai_config(owner)
        chapter = await owner.post(
            f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节", "content": "原文"},
        )
        fake_llm.push("润色后的正文")
        # 本例验证派生身份；分析分片门禁由独立专项覆盖，避免把完整 JSON 拆成数百次无关检查。
        fake_llm.push([analysis_json()])
        created = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/polish", json={})
        assert created.status_code == 202, created.text
        events = await stream_task(owner, created.json())
        assert events[-1][1]["task"]["status"] == "succeeded", events
        async with app.state.sessionmaker() as db:
            source = await db.get(Task, created.json()["id"])
            derived = await db.scalar(select(Task).where(
                Task.project_id == project["id"], Task.chapter_id == chapter.json()["id"],
                Task.kind == "chapter.analyze",
            ))
            assert derived is not None and derived.user_id == actor["id"]
            assert derived.input["_task_actor_token_version"] == source.input["_task_actor_token_version"]
            assert derived.input["_project_model"] == source.input["_project_model"]
            assert derived.input["_shared_embedding"] == source.input["_shared_embedding"]
            reference = (derived.id, derived.statistics_key)
        assert (await stream_task(owner, reference))[-1][1]["task"]["status"] == "succeeded"


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True, "auto_analyze_enabled": True}])
@pytest.mark.parametrize("change", ["token", "model"])
async def test_auto_analysis_is_not_derived_after_source_identity_changes(app, fake_llm, monkeypatch, change):
    reached, release = asyncio.Event(), asyncio.Event()
    original = ChapterHandler.on_success

    async def paused(_self, *args, **kwargs):
        reached.set()
        await release.wait()
        return await original(_self, *args, **kwargs)

    monkeypatch.setattr(ChapterHandler, "on_success", paused)
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-auto-token-owner")
        project = await create_project(owner)
        config = await create_ai_config(owner)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        fake_llm.push("已经完成的正文")
        created = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert created.status_code == 202, created.text
        try:
            await asyncio.wait_for(reached.wait(), 10)
            running = app.state.task_engine._running.get(created.json()["id"])
            assert running is not None
            if change == "token":
                changed = await owner.post("/api/v1/auth/change-password", json={
                    "old_password": "secret123", "new_password": "new-secret123",
                })
                assert changed.status_code == 204, changed.text
            else:
                changed = await owner.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "changed-model"})
                assert changed.status_code == 200, changed.text
            release.set()
            await asyncio.wait_for(asyncio.shield(running), 10)
            async with app.state.sessionmaker() as db:
                source = await db.get(Task, created.json()["id"])
                assert source.status == "succeeded"
                derived = await db.scalar(select(Task.id).where(
                    Task.project_id == project["id"], Task.chapter_id == chapter.json()["id"],
                    Task.kind == "chapter.analyze",
                ))
                assert derived is None
        finally:
            release.set()


async def test_author_retry_keeps_original_model_after_default_changes(app, fake_llm):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-retry-owner")
        project = await create_project(owner)
        await create_ai_config(owner, name="初始模型", model="initial-model", is_default=True)
        second = await create_ai_config(owner, name="后续模型", model="changed-model", is_default=False)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        fake_llm.push(AIProviderError("第一次失败"))
        created = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert created.status_code == 202, created.text
        original = created.json()
        events = await stream_task(owner, original)
        assert events[-1][1]["task"]["status"] == "failed", events
        changed = await owner.patch(f"/api/v1/ai-configs/{second['id']}", json={"is_default": True})
        assert changed.status_code == 200, changed.text
        fake_llm.push("重试正文")
        retry = await owner.post(f"/api/v1/tasks/{original['id']}/retry", params={"task_key": original["task_key"]})
        assert retry.status_code == 202, retry.text
        resumed = await stream_task(owner, retry.json())
        assert resumed[-1][1]["task"]["status"] == "succeeded", resumed
        assert [call["model"] for call in fake_llm.calls] == ["initial-model", "initial-model"]


@pytest.mark.parametrize("change", ["modified", "deleted", "missing_binding"])
async def test_author_retry_rejects_lost_or_changed_original_binding(app, fake_llm, change):
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-retry-invalid")
        project = await create_project(owner)
        first = await create_ai_config(owner, name="初始模型", model="initial-model", is_default=True)
        await create_ai_config(owner, name="其他模型", model="other-model", is_default=False)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        fake_llm.push(AIProviderError("第一次失败"))
        created = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert created.status_code == 202, created.text
        task = created.json()
        assert (await stream_task(owner, task))[-1][1]["task"]["status"] == "failed"
        if change == "modified":
            changed = await owner.patch(f"/api/v1/ai-configs/{first['id']}", json={"model": "modified"})
            assert changed.status_code == 200, changed.text
        elif change == "deleted":
            assert (await owner.delete(f"/api/v1/ai-configs/{first['id']}")).status_code == 204
        else:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task["id"])
                payload = dict(row.input)
                payload.pop("_project_model")
                row.input = payload
                await db.commit()
        retry = await owner.post(f"/api/v1/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
        assert retry.status_code in (400, 404, 409), retry.text
        assert len(fake_llm.calls) == 1
        async with app.state.sessionmaker() as db:
            saved = await db.get(Chapter, chapter.json()["id"])
            assert saved.status == "draft" and saved.generation_claim_key is None


@pytest.mark.parametrize("model_type", ["text", "embedding"])
async def test_reused_config_id_with_identical_fields_cannot_replace_original_instance(app, fake_llm, model_type):
    await app.state.task_engine.stop()
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-model-instance-owner")
        project = await create_project(owner)
        text_config = await create_ai_config(owner, name="文字模型", model="text-model")
        target = text_config if model_type == "text" else await create_ai_config(
            owner, name="向量模型", model="vector-model", model_type="embedding", is_default=True,
        )
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert queued.status_code == 202, queued.text
        async with app.state.sessionmaker() as db:
            old = await db.get(AIConfig, target["id"])
            fields = {name: getattr(old, name) for name in (
                "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type", "is_default",
                "thinking_level", "context_window_k", "max_output_k",
            )}
            old_key = old.instance_key
            await db.delete(old)
            await db.commit()
        async with app.state.sessionmaker() as db:
            replacement = AIConfig(id=target["id"], **fields)
            db.add(replacement)
            await db.commit()
            assert replacement.instance_key != old_key
        fake_llm.push("不应发送")
        await app.state.task_engine.start()
        events = await stream_task(owner, queued.json())
        assert events[-1][1]["task"]["status"] == "failed", events
        assert not fake_llm.calls and not fake_llm.embed_calls


@pytest.mark.parametrize(
    "change", ["config_changed", "config_deleted", "binding_missing", "binding_forged", "request_mismatch"],
)
async def test_queued_author_task_never_reselects_if_original_binding_fails(app, fake_llm, change):
    await app.state.task_engine.stop()
    async with _http_clients(app) as (owner, _unused):
        app.state.provider_factory = fake_llm.factory
        await register(owner, "chapter-binding-owner")
        project = await create_project(owner)
        first = await create_ai_config(owner, name="初始模型", model="initial-model", is_default=True)
        other = await create_ai_config(owner, name="后续模型", model="other-model", is_default=False)
        chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
        queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
        assert queued.status_code == 202, queued.text
        if change == "config_changed":
            changed = await owner.patch(f"/api/v1/ai-configs/{first['id']}", json={"model": "modified"})
            assert changed.status_code == 200, changed.text
        elif change == "config_deleted":
            assert (await owner.delete(f"/api/v1/ai-configs/{first['id']}")).status_code == 204
        else:
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, queued.json()["id"])
                payload = dict(task.input)
                if change == "binding_missing":
                    payload.pop("_project_model")
                elif change == "binding_forged":
                    payload["_project_model"] = {**payload["_project_model"], "config_fingerprint": "0" * 64}
                else:
                    payload["request"] = {**payload["request"], "ai_config_id": other["id"]}
                task.input = payload
                await db.commit()
        await app.state.task_engine.start()
        events = await stream_task(owner, queued.json())
        assert events[-1][1]["task"]["status"] == "failed", events
        assert not fake_llm.calls
        async with app.state.sessionmaker() as db:
            saved = await db.get(Chapter, chapter.json()["id"])
            assert saved.status == "draft" and saved.generation_claim_key is None
