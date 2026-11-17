"""分析绑定原任务执行身份：流中复用、续跑及权限失效不可落库或续发。"""

import asyncio
from contextlib import aclosing, suppress
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import AIProviderError, create_provider
from app.ai.providers.dispatch_hooks import DispatchHooks
from app.models import (
    AIConfig,
    AnalysisChange,
    Chapter,
    ChapterAnalysis,
    Project,
    ProjectCollaborator,
    SceneReviewAnalysisReceipt,
    StoryMemory,
    Task,
    User,
)
from app.services import analysis_service, memory_service, project_ai_config, rich_document_ai
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import Gate, analysis_json, create_ai_config, create_project, register
from tests.test_ai_visibility_dispatch import _server


async def _world(app, client, *, with_embedding=False, model_base_url=None):
    owner = await register(client, "analysis-execution-owner")
    config = await create_ai_config(client)
    if with_embedding:
        await create_ai_config(client, name="索引模型", model_type="embedding", model="embed")
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "分析正文", "content": "这是需要分析的章节正文。",
    })
    assert response.status_code == 201, response.text
    await app.state.task_engine.stop()
    chapter_id = response.json()["id"]
    async with app.state.sessionmaker() as db:
        token_version = (await db.get(User, owner["id"])).token_version
        if model_base_url is not None:
            (await db.get(AIConfig, config["id"])).base_url = model_base_url
            await db.flush()
        binding = await project_ai_config.resolve_project_model(
            db, owner["id"], project["id"], "text", cipher=app.state.cipher, config_id=config["id"],
            allow_private=model_base_url is not None,
        )
        selection = await project_ai_config.resolve_shared_embedding(
            db, owner["id"], project["id"], cipher=app.state.cipher,
            allow_private=model_base_url is not None,
        )
        task = Task(user_id=owner["id"], project_id=project["id"], chapter_id=chapter_id,
                    kind="chapter.analyze", status="queued",
                    input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id,
                           "_task_actor_token_version": token_version,
                           "_project_model": project_ai_config.serialize_project_model(binding),
                           "_shared_embedding": project_ai_config.serialize_shared_embedding(selection)})
        db.add(task)
        await db.flush()
        identity = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", identity.execution_key
        execution = analysis_service.analysis_execution_from_claim(identity)
        await db.commit()
    return owner, config, project, chapter_id, execution


async def _job(app, fake_llm, world, *, factory=None, allow_private=False):
    owner, _, _, chapter_id, execution = world
    pending = await analysis_service.begin_analysis_pending(app.state.sessionmaker, execution)
    async with app.state.sessionmaker() as db:
        return await analysis_service.build_analysis_job(
            db, user_id=owner["id"], chapter_id=chapter_id, ai_config_id=None, cipher=app.state.cipher,
            factory=factory or fake_llm.factory, ai_timeout=3, allow_private=allow_private, execution=execution,
            pending=pending,
        )


async def _assert_no_analysis_side_effects(app):
    async with app.state.sessionmaker() as db:
        for model in (ChapterAnalysis, AnalysisChange, StoryMemory):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def test_analysis_queue_dedup_is_personal_and_invitation_scoped(app, client, make_client):
    owner, _, project, chapter_id, _ = await _world(app, client)
    editor_client = make_client()
    editor = await register(editor_client, "analysis-dedup-editor")
    original_grant = (str(uuid4()), 2)
    async with app.state.sessionmaker() as db:
        other = Task(user_id=editor["id"], project_id=project["id"], chapter_id=chapter_id,
                     kind="chapter.analyze", status="queued", input={
                         "_collaboration_grant": {"invitation_id": original_grant[0], "revision": original_grant[1]},
                     })
        db.add(other)
        await db.flush()
        assert await analysis_service.queued_analysis(db, chapter_id, actor_id=owner["id"]) is None
        assert (await analysis_service.queued_analysis(
            db, chapter_id, actor_id=editor["id"], grant_identity=original_grant,
        )).id == other.id
        assert await analysis_service.queued_analysis(
            db, chapter_id, actor_id=editor["id"], grant_identity=(str(uuid4()), 2),
        ) is None
        # 原作者正在运行的任务也参与本人 pending 去重；不能被另一用户任务替代。
        original = await db.scalar(select(Task).where(Task.user_id == owner["id"], Task.kind == "chapter.analyze"))
        original.status = "failed"
        await db.flush()
        assert chapter_id in await analysis_service.pending_chapter_ids(db, project["id"], actor_id=owner["id"])
        own = Task(user_id=owner["id"], project_id=project["id"], chapter_id=chapter_id,
                   kind="chapter.analyze", status="queued", input={"request": {"chapter_id": chapter_id}})
        db.add(own)
        await db.flush()
        assert (await analysis_service.queued_analysis(db, chapter_id, actor_id=owner["id"])).id == own.id
        assert chapter_id not in await analysis_service.pending_chapter_ids(db, project["id"], actor_id=owner["id"])
        await db.rollback()


async def _replace_config_with_same_id(db, config_id):
    old = await db.get(AIConfig, config_id)
    old_key = old.instance_key
    values = {column.name: deepcopy(getattr(old, column.name)) for column in AIConfig.__table__.columns
              if column.name not in {"id", "instance_key"}}
    await db.delete(old)
    await db.flush()
    replacement = AIConfig(id=config_id, **values)
    db.add(replacement)
    await db.flush()
    assert replacement.instance_key != old_key


@pytest.mark.parametrize("change", ["none", "execution", "input", "cursor", "checkpoint", "token", "dirty"])
async def test_import_analysis_enqueue_requires_original_running_step(app, client, fake_llm, change):
    owner, _, project, chapter_id, previous = await _world(app, client)
    checkpoint = {"phase": "analyze", "stats": {"chapters": 1}}
    async with app.state.sessionmaker() as db:
        previous_task = await db.get(Task, previous.task_id)
        source_bindings = {key: deepcopy(previous_task.input[key]) for key in ("_project_model", "_shared_embedding")}
        await db.delete(previous_task)
        token = (await db.get(User, owner["id"])).token_version
        source = Task(user_id=owner["id"], project_id=project["id"], kind="book.import", status="queued",
                      step_cursor=0, checkpoint=deepcopy(checkpoint),
                      input={"request": {"project_id": project["id"], "ai_config_id": None},
                             "_task_actor_token_version": token, **source_bindings})
        db.add(source)
        await db.flush()
        identity = from_queued_task(source, str(uuid4()))
        source.status, source.execution_key = "running", identity.execution_key
        await db.commit()
    async with app.state.sessionmaker() as db:
        source = await db.get(Task, identity.task_id)
        if change == "execution":
            source.execution_key = str(uuid4())
        elif change == "input":
            source.input = {**source.input, "request": {**source.input["request"], "ai_config_id": 999}}
        elif change == "cursor":
            source.step_cursor = 1
        elif change == "checkpoint":
            source.checkpoint = {**source.checkpoint, "phase": "done"}
        elif change == "token":
            (await db.get(User, owner["id"])).token_version += 1
        await db.commit()
    async with app.state.sessionmaker() as db:
        locked_project = await db.get(Project, project["id"])
        if change == "dirty":
            locked_project.title = "不得自动 flush 的暂存标题"
        if change == "none":
            ids = await analysis_service.enqueue_pending(
                db, locked_project, ai_config_id=source_bindings["_project_model"]["config_id"], execution=identity,
                expected_cursor=0, expected_checkpoint=checkpoint, cipher=app.state.cipher,
            )
            assert len(ids) == 1
            await db.commit()
            task = await db.get(Task, ids[0])
            assert task.user_id == owner["id"] and task.chapter_id == chapter_id
            assert task.input["_task_actor_token_version"] == token
        else:
            with pytest.raises(VisibilityPolicyError):
                await analysis_service.enqueue_pending(
                    db, locked_project, ai_config_id=source_bindings["_project_model"]["config_id"], execution=identity,
                    expected_cursor=0, expected_checkpoint=checkpoint, cipher=app.state.cipher,
                )
            await db.rollback()
            assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 0
    assert not fake_llm.calls


@pytest.mark.parametrize("token", [None, True, -1, "0"])
async def test_analysis_cannot_capture_missing_or_invalid_original_token(app, client, fake_llm, token):
    execution = (await _world(app, client))[-1]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        data = dict(task.input)
        if token is None:
            data.pop("_task_actor_token_version")
        else:
            data["_task_actor_token_version"] = token
        task.input = data
        with pytest.raises(VisibilityPolicyError):
            analysis_service.capture_analysis_execution(task)
    assert fake_llm.calls == []


@pytest.mark.parametrize("damage", ["missing_model", "missing_embedding", "null_model", "old_model", "old_embedding"])
async def test_analysis_cannot_capture_missing_or_obsolete_model_selection(app, client, fake_llm, damage):
    execution = (await _world(app, client))[-1]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        data = deepcopy(task.input)
        if damage == "missing_model":
            data.pop("_project_model")
        elif damage == "missing_embedding":
            data.pop("_shared_embedding")
        elif damage == "null_model":
            data["_project_model"] = None
        elif damage == "old_model":
            data["_project_model"]["version"] = 1
        else:
            data["_shared_embedding"]["version"] = 1
        task.input = data
        with pytest.raises(VisibilityPolicyError):
            analysis_service.capture_analysis_execution(task)
    assert fake_llm.calls == []


async def test_analysis_pending_rejects_changed_initial_checkpoint(app, client, fake_llm):
    execution = (await _world(app, client))[-1]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.checkpoint = {"unexpected": "changed"}
        await db.commit()
    with pytest.raises(VisibilityPolicyError):
        await analysis_service.begin_analysis_pending(app.state.sessionmaker, execution)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.checkpoint == {"unexpected": "changed"}
    assert not fake_llm.calls


async def test_analysis_coalesces_only_immediately_ready_fragments(app, client, fake_llm):
    world = await _world(app, client)
    job = await _job(app, fake_llm, world)
    raw = analysis_json(summary="连续小片段保持原样")
    fake_llm.push(raw)
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        result = [event async for event in events]
    chunks = [data["text"] for kind, data in result if kind == "delta"]
    assert "".join(chunks) == raw and result[-1][0] == "done"
    assert len(chunks) < len(raw) // 14


async def test_analysis_ready_fragment_is_published_before_provider_error(app, client, fake_llm):
    job = await _job(app, fake_llm, await _world(app, client))
    fake_llm.push(["模型已返回的片段", AIProviderError("模拟模型中断")])
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        result = [event async for event in events]
    assert result == [("delta", {"text": "模型已返回的片段"}), ("error", {"detail": "模拟模型中断"})]


async def test_ready_text_does_not_wait_for_later_network_and_closes_pending_read():
    blocked, stopped = asyncio.Event(), asyncio.Event()

    async def provider():
        try:
            yield "现有片段"
            blocked.set()
            await asyncio.Event().wait()
            yield "不可到达"
        finally:
            stopped.set()

    async with aclosing(analysis_service.coalesce_ready_text(provider())) as stream:
        assert await asyncio.wait_for(anext(stream), 1) == "现有片段"
        await asyncio.wait_for(blocked.wait(), 1)
    assert stopped.is_set()


@pytest.mark.parametrize("change", [
    "replace", "resume", "execution", "terminal", "grant", "binding", "model", "visibility", "disabled", "input",
    "token", "embedding", "shared_binding", "cancel_requested",
])
async def test_analysis_does_not_commit_when_execution_changes_during_stream(app, client, fake_llm, change):
    world = await _world(app, client, with_embedding=change == "embedding")
    _, config, project, chapter_id, execution = world
    job = await _job(app, fake_llm, world)
    gate = Gate()
    fake_llm.push([gate, analysis_json(summary="不应保存的摘要")])

    async def consume():
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            return [event async for event in events]

    pending = asyncio.create_task(consume())
    try:
        await gate.wait_reached()
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, execution.task_id)
            if change == "replace":
                old_key = task.statistics_key
                await db.delete(task)
                await db.flush()
                task = Task(id=execution.task_id, user_id=execution.actor_id, project_id=project["id"],
                            chapter_id=chapter_id, kind="chapter.analyze", status="running",
                            queued_at=execution.queued_at, execution_key=str(uuid4()),
                            input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id})
                db.add(task)
                await db.flush()
                assert task.statistics_key != old_key
            elif change == "resume":
                task.queued_at += timedelta(seconds=1)
                task.execution_key = str(uuid4())
            elif change == "execution":
                task.execution_key = str(uuid4())
            elif change == "terminal":
                task.status = "succeeded"
            elif change == "grant":
                task.input = {**task.input, "_collaboration_grant": {
                    "invitation_id": str(uuid4()), "revision": 1,
                }}
            elif change == "binding":
                task.input = {**task.input, "_project_model": {"unexpected": "binding"}}
            elif change == "shared_binding":
                task.input = {**task.input, "_shared_embedding": {"unexpected": "selection"}}
            elif change == "model":
                (await db.get(AIConfig, config["id"])).model = "changed-model"
            elif change == "embedding":
                embedding = await db.scalar(select(AIConfig).where(
                    AIConfig.user_id == execution.actor_id, AIConfig.model_type == "embedding",
                ))
                embedding.model = "changed-embed"
            elif change == "visibility":
                row = await db.get(Project, project["id"])
                row.ai_visibility_revision += 1
            elif change == "input":
                task.input = {**task.input, "request": {**task.input["request"], "ai_config_id": 999}}
            elif change == "token":
                (await db.get(User, execution.actor_id)).token_version += 1
            elif change == "cancel_requested":
                task.admin_cancel_requested_at = datetime.now(UTC)
            else:
                await db.execute(update(User).where(User.id == execution.actor_id).values(is_active=False))
            await db.commit()
        gate.release()
        with pytest.raises(VisibilityPolicyError):
            await asyncio.wait_for(pending, 10)
        await _assert_no_analysis_side_effects(app)
    finally:
        gate.release()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


@pytest.mark.parametrize("change", [
    "replace", "resume", "execution", "terminal", "grant", "binding", "model", "disabled", "input", "token",
    "embedding", "shared_binding", "cancel_requested",
])
async def test_analysis_refuses_changed_execution_before_model_send(app, client, fake_llm, change):
    world = await _world(app, client, with_embedding=change == "embedding")
    _, config, project, chapter_id, execution = world
    job = await _job(app, fake_llm, world)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        if change == "replace":
            await db.delete(task)
            await db.flush()
            db.add(Task(id=execution.task_id, user_id=execution.actor_id, project_id=project["id"],
                        chapter_id=chapter_id, kind="chapter.analyze", status="running",
                        queued_at=execution.queued_at, execution_key=str(uuid4()),
                        input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id}))
        elif change == "resume":
            task.queued_at += timedelta(seconds=1)
            task.execution_key = str(uuid4())
        elif change == "execution":
            task.execution_key = str(uuid4())
        elif change == "terminal":
            task.status = "succeeded"
        elif change == "grant":
            task.input = {**task.input, "_collaboration_grant": {
                "invitation_id": str(uuid4()), "revision": 1,
            }}
        elif change == "binding":
            task.input = {**task.input, "_project_model": {"unexpected": "binding"}}
        elif change == "shared_binding":
            task.input = {**task.input, "_shared_embedding": {"unexpected": "selection"}}
        elif change == "model":
            (await db.get(AIConfig, config["id"])).model = "changed-model"
        elif change == "embedding":
            embedding = await db.scalar(select(AIConfig).where(
                AIConfig.user_id == execution.actor_id, AIConfig.model_type == "embedding",
            ))
            embedding.model = "changed-embed"
        elif change == "input":
            task.input = {**task.input, "request": {**task.input["request"], "ai_config_id": 999}}
        elif change == "token":
            (await db.get(User, execution.actor_id)).token_version += 1
        elif change == "cancel_requested":
            task.admin_cancel_requested_at = datetime.now(UTC)
        else:
            await db.execute(update(User).where(User.id == execution.actor_id).values(is_active=False))
        await db.commit()
    fake_llm.push(analysis_json())
    with pytest.raises(VisibilityPolicyError):
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            async for _ in events:
                pass
    assert fake_llm.calls == []
    await _assert_no_analysis_side_effects(app)


async def test_editor_manual_analysis_is_admitted_with_grant_and_malformed_rows_fail_closed(
    app, client, make_client, fake_llm,
):
    # E4-g1 起 accepted editor 的手动分析经受控受理入队（引擎已停止，此处只观察受理结果）；
    # 端到端执行、撤权各时点与费用来源见 test_collaboration_analysis_entry_e4g.py。
    owner, _, project, chapter_id, _ = await _world(app, client)
    editor_client = make_client()
    editor = await register(editor_client, "analysis-execution-editor")
    await create_ai_config(editor_client)
    base = f"/api/v1/projects/{project['id']}"
    invitation = await client.post(f"{base}/collaborators", json={
        "username": "analysis-execution-editor", "role": "editor",
    })
    assert invitation.status_code == 201, invitation.text
    invitation = invitation.json()
    accepted = await editor_client.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    response = await editor_client.post(f"/api/v1/chapters/{chapter_id}/analyze", json={})
    assert response.status_code == 202, response.text
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
        ))
        admitted = await db.get(Task, response.json()["id"])
        assert (admitted.user_id, admitted.status, admitted.kind) == (editor["id"], "queued", "chapter.analyze")
        assert admitted.input["_collaboration_grant"] == {
            "invitation_id": member.invitation_id, "revision": member.revision,
        }
        assert admitted.input["_project_model"]["source"] == "actor"
        assert "_analysis_proposal_source" not in admitted.input
        # 缺服务端冻结模型绑定的协作行仍不能被捕获为执行身份（不从当前配置补签）。
        task = Task(user_id=editor["id"], project_id=project["id"], chapter_id=chapter_id,
                    kind="chapter.analyze", status="running", execution_key=str(uuid4()),
                    input={"chapter_id": chapter_id, "_collaboration_grant": {
                        "invitation_id": member.invitation_id, "revision": member.revision,
                    }, "_task_actor_token_version": (await db.get(User, editor["id"])).token_version})
        db.add(task)
        await db.flush()
        with pytest.raises(VisibilityPolicyError):
            analysis_service.capture_analysis_execution(task)
        await db.rollback()
    assert owner["id"] != editor["id"] and not fake_llm.calls
    await _assert_no_analysis_side_effects(app)


async def _accepted_editor_analysis(app, owner, editor_client, fake_llm, *, owner_fallback=False):
    from tests.test_collaboration_editor_proposal import _editor_proposal

    project, chapter, editor, member, _url, _source, source_job, source_identity, body = await _editor_proposal(
        app, owner, editor_client, allow_owner_ai=owner_fallback, editor_config=not owner_fallback,
        owner_embedding=owner_fallback,
    )
    accepted = await editor_client.post(
        f"/api/v1/tasks/{source_job.task_id}/rich-proposals/{source_identity['token']}/accept", json=body,
    )
    assert accepted.status_code == 200, accepted.text
    async with app.state.sessionmaker() as db:
        task = await db.scalar(select(Task).where(Task.kind == "chapter.analyze", Task.user_id == editor["id"]))
        assert task is not None and task.status == "queued" and "_analysis_proposal_source" in task.input
        identity = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", identity.execution_key
        execution = analysis_service.analysis_execution_from_claim(identity)
        await db.commit()
    pending = await analysis_service.begin_analysis_pending(app.state.sessionmaker, execution)
    async with app.state.sessionmaker() as db:
        job = await analysis_service.build_analysis_job(
            db, user_id=editor["id"], chapter_id=chapter["id"], ai_config_id=execution.model_binding.config_id,
            cipher=app.state.cipher, factory=fake_llm.factory, ai_timeout=3, allow_private=False,
            execution=execution, pending=pending,
        )
    return project, chapter, editor, member, source_job, execution, job


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize(
    "change", ["none", "demote", "reinvite", "token", "source_delete", "source_replace", "chapter"],
)
async def test_editor_proposal_analysis_keeps_original_actor_grant_and_model(
    app, client, make_client, fake_llm, change,
):
    editor_client = make_client()
    project, chapter, editor, member, source_job, execution, job = await _accepted_editor_analysis(
        app, client, editor_client, fake_llm,
    )
    base = f"/api/v1/projects/{project['id']}"
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    if change == "demote":
        response = await client.patch(f"{base}/collaborators/{editor['id']}", json={
            "expected_revision": grant["revision"], "role": "viewer",
        })
        assert response.status_code == 200, response.text
    elif change == "reinvite":
        response = await client.delete(f"{base}/collaborators/{editor['id']}", params={
            "expected_revision": grant["revision"],
        })
        assert response.status_code == 204, response.text
        invite = await client.post(f"{base}/collaborators", json={"username": "proposal-editor", "role": "editor"})
        assert invite.status_code == 201, invite.text
        new = invite.json()
        accepted = await editor_client.post(
            f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept",
            json={"expected_revision": new["revision"]},
        )
        assert accepted.status_code == 200 and new["invitation_id"] != grant["invitation_id"]
    elif change == "token":
        async with app.state.sessionmaker() as db:
            (await db.get(User, editor["id"])).token_version += 1
            await db.commit()
    elif change in {"source_delete", "source_replace"}:
        async with app.state.sessionmaker() as db:
            source = await db.get(Task, source_job.task_id)
            await db.delete(source)
            await db.flush()
            if change == "source_replace":
                db.add(Task(id=source_job.task_id, user_id=editor["id"], project_id=project["id"],
                            chapter_id=chapter["id"], kind="chapter.generate", status="succeeded", input={}))
            await db.commit()
    elif change == "chapter":
        from tests.test_rich_document_api import save_payload

        document_url = f"{base}/chapters/{chapter['id']}/scenes/{source_job.scenes[0]['id']}/document"
        document = save_payload((await client.get(document_url)).json())
        document["document"]["content"] += "后续改稿"
        # 末尾追加不移动任何已有位置；按富文本保存协议显式声明映射（同服务端 write_mapped_document）。
        document["mapped_section_keys"] = [item["key"] for item in document["document"]["sections"]]
        document["mapped_marker_keys"] = [item["key"] for item in document["document"]["markers"]]
        response = await client.put(document_url, json=document)
        assert response.status_code == 200, response.text
    fake_llm.push(analysis_json(summary="编辑者分析"))
    if change == "none":
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            result = [event async for event in events]
        assert result[-1][0] == "done" and any("关键词检索" in note for note in result[-1][1]["warnings"])
        async with app.state.sessionmaker() as db:
            analysis = await db.get(ChapterAnalysis, result[-1][1]["analysis_id"])
            source = await db.get(Task, analysis.task_id)
            assert source.user_id == editor["id"] and source.input["_collaboration_grant"] == grant
    else:
        with pytest.raises(VisibilityPolicyError):
            async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
                async for _ in events:
                    pass
        assert not fake_llm.calls
        await _assert_no_analysis_side_effects(app)


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("change", ["none", "revoke_owner_ai"])
async def test_editor_analysis_owner_model_requires_original_explicit_authorization(
    app, client, make_client, fake_llm, change,
):
    editor_client = make_client()
    project, _chapter, editor, member, _source, execution, job = await _accepted_editor_analysis(
        app, client, editor_client, fake_llm, owner_fallback=True,
    )
    assert execution.model_binding.source == "owner" and execution.embedding_selection.mode == "vector"
    if change == "revoke_owner_ai":
        response = await client.patch(f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}", json={
            "expected_revision": member["revision"], "allow_owner_ai": False,
        })
        assert response.status_code == 200, response.text
    fake_llm.push(analysis_json(summary="已授权的分析"))
    if change == "none":
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            result = [event async for event in events]
        assert result[-1][0] == "done" and any("向量索引暂未生成" in note for note in result[-1][1]["warnings"])
        assert len(fake_llm.calls) == 1 and fake_llm.calls[0]["model"] == "gpt-test"
        identity = execution.task_identity
        response = await editor_client.get(f"/api/v1/tasks/{identity.task_id}", params={"task_key": identity.task_key})
        assert response.status_code == 200
        assert "_project_model" not in response.text and "_analysis_proposal_source" not in response.text
        assert "config_owner_id" not in response.text
    else:
        with pytest.raises(VisibilityPolicyError):
            async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
                async for _ in events:
                    pass
        assert not fake_llm.calls
        await _assert_no_analysis_side_effects(app)


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("change", ["reinvite", "source_delete", "owner_model"])
async def test_editor_proposal_analysis_cannot_save_after_source_changes_during_stream(
    app, client, make_client, fake_llm, change,
):
    editor_client = make_client()
    project, _chapter, editor, member, source_job, _execution, job = await _accepted_editor_analysis(
        app, client, editor_client, fake_llm, owner_fallback=change == "owner_model",
    )
    gate = Gate()
    fake_llm.push([gate, analysis_json(summary="旧来源不得保存")])

    async def consume():
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            return [event async for event in events]

    pending = asyncio.create_task(consume())
    try:
        await gate.wait_reached()
        if change == "reinvite":
            base = f"/api/v1/projects/{project['id']}/collaborators"
            response = await client.delete(f"{base}/{editor['id']}", params={"expected_revision": member["revision"]})
            assert response.status_code == 204, response.text
            invitation = await client.post(base, json={"username": "proposal-editor", "role": "editor"})
            assert invitation.status_code == 201, invitation.text
            new = invitation.json()
            accepted = await editor_client.post(
                f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept",
                json={"expected_revision": new["revision"]},
            )
            assert accepted.status_code == 200 and new["invitation_id"] != member["invitation_id"]
        elif change == "source_delete":
            async with app.state.sessionmaker() as db:
                await db.delete(await db.get(Task, source_job.task_id))
                await db.commit()
        else:
            response = await client.patch(f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}", json={
                "expected_revision": member["revision"], "allow_owner_ai": False,
            })
            assert response.status_code == 200, response.text
        gate.release()
        with pytest.raises(VisibilityPolicyError):
            await asyncio.wait_for(pending, 10)
        assert len(fake_llm.calls) == 1
        await _assert_no_analysis_side_effects(app)
    finally:
        gate.release()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def test_analysis_storage_failure_cannot_become_a_batch_warning(app, client, fake_llm, monkeypatch):
    world = await _world(app, client)
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json(summary="不得部分保存"))

    async def failing_memory(*_args, **_kwargs):
        raise RuntimeError("数据库失败，不得泄露")

    monkeypatch.setattr(memory_service, "sync_from_analysis", failing_memory)
    with pytest.raises(VisibilityPolicyError):
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            async for _ in events:
                pass
    await _assert_no_analysis_side_effects(app)


async def test_analysis_cannot_send_with_a_different_model_after_binding(app, client, fake_llm):
    world = await _world(app, client)
    job = await _job(app, fake_llm, world)
    job = replace(job, model="unbound-model")
    fake_llm.push(analysis_json())
    with pytest.raises(VisibilityPolicyError):
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            async for _ in events:
                pass
    assert fake_llm.calls == []
    await _assert_no_analysis_side_effects(app)


async def _batch_world(app, client, *, model_base_url=None):
    owner, config, project, chapter_id, old_execution = await _world(app, client, model_base_url=model_base_url)
    second = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "下一章", "content": "另一章的正文。",
    })
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    async with app.state.sessionmaker() as db:
        previous = await db.get(Task, old_execution.task_id)
        bindings = {key: deepcopy(previous.input[key]) for key in ("_project_model", "_shared_embedding")}
        await db.delete(previous)
        chapter = await db.get(Chapter, chapter_id)
        fingerprint = (await rich_document_ai.analysis_fingerprints(db, [chapter]))[chapter_id]
        task = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.batch", status="running",
                    step_cursor=0, input={"request": {"project_id": project["id"]},
                    "chapter_ids": [chapter_id, second_id],
                    "_task_actor_token_version": (await db.get(User, owner["id"])).token_version, **bindings})
        task.status = "queued"
        db.add(task)
        await db.flush()
        ref = {"run_key": str(uuid4()), "task_key": task.statistics_key,
               "chapter_id": chapter_id, "content_hash": fingerprint}
        task.checkpoint = {"phase": "analyze", "items": [{"chapter_id": chapter_id}, {"chapter_id": second_id}],
                           "rich_analysis": ref}
        db.add(SceneReviewAnalysisReceipt(run_key=ref["run_key"], task_key=ref["task_key"],
                                          chapter_id=chapter_id, content_hash=fingerprint, status="running"))
        identity = from_queued_task(task, str(uuid4()))
        task.status, task.execution_key = "running", identity.execution_key
        model_binding, selection = analysis_service.analysis_bindings(
            task.input, actor_id=owner["id"], project_id=project["id"],
        )
        execution = analysis_service.AnalysisExecution(
            task_id=task.id, task_key=task.statistics_key, actor_id=owner["id"], project_id=project["id"],
            queued_at=task.queued_at, execution_key=task.execution_key, grant_identity=None,
            task_kind="chapter.batch", batch_cursor=0, input_hash=analysis_service.analysis_input_hash(task.input),
            actor_token_version=task.input["_task_actor_token_version"],
            model_binding=model_binding, embedding_selection=selection, task_identity=identity,
        )
        await db.commit()
    return owner, config, project, chapter_id, second_id, execution, ref


@pytest.mark.parametrize(("point", "change"), [
    ("before", "cursor"), ("before", "phase"), ("before", "reference"),
    ("before", "receipt"), ("before", "chapter"), ("before", "input"),
    ("during", "cursor"), ("during", "phase"), ("during", "reference"),
    ("during", "receipt"), ("during", "chapter"), ("during", "input"),
])
async def test_batch_analysis_cannot_cross_attempt_before_send_or_save(
    app, client, fake_llm, point, change,
):
    owner, _, _, chapter_id, second_id, execution, ref = await _batch_world(app, client)
    async with app.state.sessionmaker() as db:
        job = await analysis_service.build_analysis_job(
            db, user_id=owner["id"], chapter_id=chapter_id, ai_config_id=None, cipher=app.state.cipher,
            factory=fake_llm.factory, ai_timeout=3, allow_private=False, execution=execution, review_analysis=ref,
        )

    async def change_attempt():
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, execution.task_id)
            checkpoint = deepcopy(task.checkpoint)
            if change == "cursor":
                task.step_cursor = 1
            elif change == "phase":
                checkpoint["phase"] = "generate"
            elif change == "reference":
                checkpoint["rich_analysis"]["run_key"] = str(uuid4())
            elif change == "receipt":
                (await db.get(SceneReviewAnalysisReceipt, ref["run_key"])).status = "failed"
            elif change == "input":
                task.input = {**task.input, "request": {**task.input["request"], "other": "changed"}}
            else:
                checkpoint["items"][0]["chapter_id"] = second_id
            task.checkpoint = checkpoint
            await db.commit()

    gate = Gate()
    fake_llm.push([gate, analysis_json()])

    async def consume():
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            return [event async for event in events]

    if point == "before":
        await change_attempt()
    pending = asyncio.create_task(consume())
    try:
        if point == "during":
            await gate.wait_reached()
            await change_attempt()
            gate.release()
        with pytest.raises(VisibilityPolicyError):
            await asyncio.wait_for(pending, 10)
        assert len(fake_llm.calls) == (1 if point == "during" else 0)
        await _assert_no_analysis_side_effects(app)
    finally:
        gate.release()
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


@pytest.mark.parametrize("change", ["none", "cursor", "reference", "receipt", "analysis"])
async def test_completed_batch_analysis_requires_current_attempt_and_result(app, client, fake_llm, change):
    owner, _, _, chapter_id, _, execution, ref = await _batch_world(app, client)
    async with app.state.sessionmaker() as db:
        job = await analysis_service.build_analysis_job(
            db, user_id=owner["id"], chapter_id=chapter_id, ai_config_id=None, cipher=app.state.cipher,
            factory=fake_llm.factory, ai_timeout=3, allow_private=False, execution=execution, review_analysis=ref,
        )
    fake_llm.push(analysis_json())
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        results = [event async for event in events]
    assert results[-1][0] == "done"
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        receipt = await db.get(SceneReviewAnalysisReceipt, ref["run_key"])
        if change == "cursor":
            task.step_cursor = 1
        elif change == "reference":
            checkpoint = deepcopy(task.checkpoint)
            checkpoint["rich_analysis"]["run_key"] = str(uuid4())
            task.checkpoint = checkpoint
        elif change == "receipt":
            receipt.status = "failed"
        elif change == "analysis":
            analysis = await db.get(ChapterAnalysis, receipt.analysis_id)
            analysis.content_hash = "0" * 64
        await db.commit()
    reference = analysis_service.capture_analysis_reference(ref)
    async with app.state.sessionmaker() as db:
        await analysis_service._analysis_write_barrier(db, execution)
        if change == "none":
            await analysis_service.require_completed_batch_analysis(db, execution, reference)
        else:
            with pytest.raises(VisibilityPolicyError):
                await analysis_service.require_completed_batch_analysis(db, execution, reference)
        await db.rollback()


async def test_completed_batch_receipt_can_prove_same_actor_prior_task(app, client, fake_llm):
    owner, _, project, chapter_id, _, execution, ref = await _batch_world(app, client)
    async with app.state.sessionmaker() as db:
        job = await analysis_service.build_analysis_job(
            db, user_id=owner["id"], chapter_id=chapter_id, ai_config_id=None, cipher=app.state.cipher,
            factory=fake_llm.factory, ai_timeout=3, allow_private=False, execution=execution, review_analysis=ref,
        )
    fake_llm.push(analysis_json())
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        assert [event async for event in events][-1][0] == "done"
    async with app.state.sessionmaker() as db:
        old = await db.get(Task, execution.task_id)
        old.status, old.execution_key = "interrupted", None
        retry = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.batch", status="queued",
                     step_cursor=0, input=deepcopy(old.input),
                     checkpoint=deepcopy(old.checkpoint), retry_of_id=old.id)
        db.add(retry)
        await db.flush()
        identity = from_queued_task(retry, str(uuid4()))
        retry.status, retry.execution_key = "running", identity.execution_key
        model_binding, selection = analysis_service.analysis_bindings(
            retry.input, actor_id=owner["id"], project_id=project["id"],
        )
        current = analysis_service.AnalysisExecution(
            task_id=retry.id, task_key=retry.statistics_key, actor_id=owner["id"], project_id=project["id"],
            queued_at=retry.queued_at, execution_key=retry.execution_key, grant_identity=None,
            task_kind="chapter.batch", batch_cursor=0, input_hash=analysis_service.analysis_input_hash(retry.input),
            actor_token_version=retry.input["_task_actor_token_version"],
            model_binding=model_binding, embedding_selection=selection, task_identity=identity,
        )
        await db.commit()
    reference = analysis_service.capture_analysis_reference(ref)
    async with app.state.sessionmaker() as db:
        await analysis_service._analysis_write_barrier(db, current)
        await analysis_service.require_completed_batch_analysis(db, current, reference)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        (await db.get(Task, execution.task_id)).kind = "chapter.analyze"
        await db.commit()
    async with app.state.sessionmaker() as db:
        await analysis_service._analysis_write_barrier(db, current)
        with pytest.raises(VisibilityPolicyError):
            await analysis_service.require_completed_batch_analysis(db, current, reference)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(Task, execution.task_id))
        await db.commit()
    async with app.state.sessionmaker() as db:
        await analysis_service._analysis_write_barrier(db, current)
        with pytest.raises(VisibilityPolicyError):
            await analysis_service.require_completed_batch_analysis(db, current, reference)
        await db.rollback()


async def test_batch_receipt_accepts_proven_intermediate_retry_and_rejects_broken_chain(app, client, fake_llm):
    owner, _, project, chapter_id, _, original, ref = await _batch_world(app, client)

    async def retry_of(previous):
        async with app.state.sessionmaker() as db:
            old = await db.get(Task, previous.task_id)
            old.status, old.execution_key = "interrupted", None
            retry = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.batch", status="queued",
                         step_cursor=0, input=deepcopy(old.input),
                         checkpoint=deepcopy(old.checkpoint), retry_of_id=old.id)
            db.add(retry)
            await db.flush()
            identity = from_queued_task(retry, str(uuid4()))
            retry.status, retry.execution_key = "running", identity.execution_key
            model_binding, selection = analysis_service.analysis_bindings(
                retry.input, actor_id=owner["id"], project_id=project["id"],
            )
            current = analysis_service.AnalysisExecution(
                task_id=retry.id, task_key=retry.statistics_key, actor_id=owner["id"], project_id=project["id"],
                queued_at=retry.queued_at, execution_key=retry.execution_key, grant_identity=None,
                task_kind="chapter.batch", batch_cursor=0, input_hash=analysis_service.analysis_input_hash(retry.input),
                actor_token_version=retry.input["_task_actor_token_version"],
                model_binding=model_binding, embedding_selection=selection, task_identity=identity,
            )
            await db.commit()
            return current

    middle = await retry_of(original)
    async with app.state.sessionmaker() as db:
        job = await analysis_service.build_analysis_job(
            db, user_id=owner["id"], chapter_id=chapter_id, ai_config_id=None, cipher=app.state.cipher,
            factory=fake_llm.factory, ai_timeout=3, allow_private=False, execution=middle, review_analysis=ref,
        )
    fake_llm.push(analysis_json())
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        assert [event async for event in events][-1][0] == "done"
    current = await retry_of(middle)
    reference = analysis_service.capture_analysis_reference(ref)
    async with app.state.sessionmaker() as db:
        await analysis_service._analysis_write_barrier(db, current)
        await analysis_service.require_completed_batch_analysis(db, current, reference)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        (await db.get(Task, middle.task_id)).retry_of_id = None
        await db.commit()
    async with app.state.sessionmaker() as db:
        await analysis_service._analysis_write_barrier(db, current)
        with pytest.raises(VisibilityPolicyError):
            await analysis_service.require_completed_batch_analysis(db, current, reference)
        await db.rollback()


@pytest.mark.parametrize("field", ["run_key", "task_key", "chapter_id", "content_hash", "extra"])
def test_analysis_reference_rejects_malformed_identity(field):
    value = {"run_key": str(uuid4()), "task_key": str(uuid4()), "chapter_id": 1, "content_hash": "a" * 64}
    value[field] = {"run_key": "bad", "task_key": "bad", "chapter_id": True,
                    "content_hash": "not-a-hash", "extra": "unexpected"}[field]
    with pytest.raises(VisibilityPolicyError):
        analysis_service.capture_analysis_reference(value)


async def test_analysis_pending_is_durable_and_required_before_send(app, client, fake_llm):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.checkpoint["_analysis_pending"]["execution_key"] == execution.execution_key
        assert "_analysis_completion" not in task.checkpoint
        task.checkpoint = None
        await db.commit()
    fake_llm.push(analysis_json())
    with pytest.raises(analysis_service.AnalysisCommitUncertain):
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            async for _ in events:
                pass
    assert not fake_llm.calls
    await _assert_no_analysis_side_effects(app)


@pytest.mark.parametrize("outcome", ["committed", "rolled_back"])
async def test_analysis_pending_commit_ack_loss_never_starts_model(app, client, fake_llm, monkeypatch, outcome):
    world = await _world(app, client)
    execution = world[-1]
    original_commit = AsyncSession.commit
    lost = False

    async def uncertain_commit(db):
        nonlocal lost
        if not lost:
            lost = True
            if outcome == "committed":
                await original_commit(db)
            else:
                await db.rollback()
            raise RuntimeError("模拟准备标记提交确认丢失")
        return await original_commit(db)

    monkeypatch.setattr(AsyncSession, "commit", uncertain_commit)
    with pytest.raises(analysis_service.AnalysisCommitUncertain):
        await analysis_service.begin_analysis_pending(app.state.sessionmaker, execution)
    assert lost and not fake_llm.calls
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.status == "running" and task.execution_key == execution.execution_key
        assert bool(task.checkpoint and "_analysis_pending" in task.checkpoint) is (outcome == "committed")
    await _assert_no_analysis_side_effects(app)


async def test_analysis_known_model_failure_clears_only_its_own_pending(app, client, fake_llm):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    fake_llm.push("not json")
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        results = [event async for event in events]
    assert results[-1][0] == "error"
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.checkpoint is None and task.execution_key == execution.execution_key
    await _assert_no_analysis_side_effects(app)


async def test_analysis_cancelled_before_save_clears_pending_without_results(app, client, fake_llm):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    gate = Gate()
    fake_llm.push([gate, analysis_json()])

    async def consume():
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            return [event async for event in events]

    pending = asyncio.create_task(consume())
    await gate.wait_reached()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.checkpoint is None and task.execution_key == execution.execution_key
    await _assert_no_analysis_side_effects(app)


@pytest.mark.parametrize(
    "change", ["none", "resume", "replace", "disabled", "storage", "receipt", "task_result", "input", "token",
               "cancel_requested"],
)
async def test_analysis_index_enqueue_requires_the_original_execution(
    app, client, fake_llm, monkeypatch, change,
):
    world = await _world(app, client, with_embedding=True)
    _, _, project, chapter_id, execution = world
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json())
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        results = [event async for event in events]
    assert results[-1][0] == "done"
    assert any("向量索引暂未生成" in warning for warning in results[-1][1]["warnings"])
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.status, task.result = "succeeded", results[-1][1]
        if change == "resume":
            task.queued_at += timedelta(seconds=1)
            task.execution_key = str(uuid4())
        elif change == "replace":
            await db.delete(task)
            await db.flush()
            db.add(Task(id=execution.task_id, user_id=execution.actor_id, project_id=project["id"],
                        chapter_id=chapter_id, kind="chapter.analyze", status="succeeded",
                        queued_at=execution.queued_at, execution_key=str(uuid4()),
                        input={"request": {"chapter_id": chapter_id}, "chapter_id": chapter_id}))
        elif change == "disabled":
            await db.execute(update(User).where(User.id == execution.actor_id).values(is_active=False))
        elif change == "receipt":
            checkpoint = dict(task.checkpoint)
            checkpoint["_analysis_completion"] = {**checkpoint["_analysis_completion"], "analysis_hash": "0" * 64}
            task.checkpoint = checkpoint
        elif change == "task_result":
            task.result = {**task.result, "summary": "不一致的结果"}
        elif change == "input":
            task.input = {**task.input, "request": {**task.input["request"], "ai_config_id": 999}}
        elif change == "token":
            (await db.get(User, execution.actor_id)).token_version += 1
        elif change == "cancel_requested":
            task.admin_cancel_requested_at = datetime.now(UTC)
        await db.commit()

    if change == "storage":
        async def fail_barrier(*_args, **_kwargs):
            raise RuntimeError("模拟数据库统计失败")

        monkeypatch.setattr(analysis_service, "_analysis_write_barrier", fail_barrier)

    class Engine:
        notifications = 0

        def notify(self):
            self.notifications += 1

    engine = Engine()
    if change == "none":
        outcome = await analysis_service.enqueue_memory_for_execution(app.state.sessionmaker, execution, engine=engine)
        assert outcome.state == "unavailable" and outcome.task_id is None and outcome.warning
        assert engine.notifications == 0
    else:
        with pytest.raises(VisibilityPolicyError):
            await analysis_service.enqueue_memory_for_execution(app.state.sessionmaker, execution, engine=engine)
        assert engine.notifications == 0
    async with app.state.sessionmaker() as db:
        queued = list(await db.scalars(select(Task).where(Task.kind == "kb.index")))
        assert not queued
        assert await db.scalar(select(func.count()).select_from(ChapterAnalysis)) == 1


async def test_analysis_keeps_original_keyword_selection_when_embedding_is_added(app, client, fake_llm):
    world = await _world(app, client)
    execution = world[-1]
    assert execution.embedding_selection.mode == "keyword"
    job = await _job(app, fake_llm, world)
    await create_ai_config(client, name="后来添加的向量模型", model_type="embedding", model="embed")
    fake_llm.push(analysis_json())
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        result = [event async for event in events]
    assert result[-1][0] == "done" and any("关键词检索" in warning for warning in result[-1][1]["warnings"])
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.status, task.result = "succeeded", result[-1][1]
        await db.commit()
    outcome = await analysis_service.enqueue_memory_for_execution(app.state.sessionmaker, execution)
    assert outcome.state == "keyword" and outcome.task_id is None and "关键词检索" in outcome.warning
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "kb.index")) == 0


@pytest.mark.parametrize("change", ["resume", "input", "model_aba", "embedding_aba"])
async def test_real_sdk_analysis_redirect_rechecks_execution_each_send(app, client, fake_llm, monkeypatch, change):
    async with _server(status="307 Temporary Redirect", payload=b"", headers={"Location": "/fallback"}) as (
        url, _received, headers, body, requests,
    ):
        world = await _world(app, client, with_embedding=change == "embedding_aba", model_base_url=f"{url}/v1/")
        execution = world[-1]
        original = DispatchHooks._after_headers
        changed = False

        async def resume_after_first_headers(hooks, response):
            nonlocal changed
            await original(hooks, response)
            if not changed:
                async with app.state.sessionmaker() as db:
                    if change == "resume":
                        await db.execute(update(Task).where(Task.id == execution.task_id).values(
                            queued_at=execution.queued_at + timedelta(seconds=1), execution_key=str(uuid4()),
                        ))
                    elif change == "input":
                        task = await db.get(Task, execution.task_id)
                        task.input = {**task.input, "request": {**task.input["request"], "ai_config_id": 999}}
                    else:
                        config_id = (execution.model_binding.config_id if change == "model_aba"
                                     else execution.embedding_selection.model.config_id)
                        await _replace_config_with_same_id(db, config_id)
                    await db.commit()
                changed = True

        monkeypatch.setattr(DispatchHooks, "_after_headers", resume_after_first_headers)
        headers.set()
        body.set()
        job = await _job(app, fake_llm, world, factory=create_provider, allow_private=True)
        with pytest.raises(VisibilityPolicyError):
            async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
                async for _ in events:
                    pass
        # 只统计带请求体的模型发送：307 的后续跳会原样携带请求体，绕过复核时这里会是 2。
        # 并行运行时本地临时端口偶尔会收到与本用例无关的空请求，不能计入发送次数。
        sent = [request for request in requests if request]
        assert changed and len(sent) == 1, requests
    await _assert_no_analysis_side_effects(app)


async def test_real_sdk_batch_analysis_redirect_rechecks_original_attempt(app, client, monkeypatch):
    async with _server(status="307 Temporary Redirect", payload=b"", headers={"Location": "/fallback"}) as (
        url, _received, headers, body, requests,
    ):
        owner, _, _, chapter_id, _, execution, ref = await _batch_world(app, client, model_base_url=f"{url}/v1/")
        original = DispatchHooks._after_headers
        changed = False

        async def move_after_first_headers(hooks, response):
            nonlocal changed
            await original(hooks, response)
            if not changed:
                async with app.state.sessionmaker() as db:
                    await db.execute(update(Task).where(Task.id == execution.task_id).values(step_cursor=1))
                    await db.commit()
                changed = True

        monkeypatch.setattr(DispatchHooks, "_after_headers", move_after_first_headers)
        headers.set()
        body.set()
        async with app.state.sessionmaker() as db:
            job = await analysis_service.build_analysis_job(
                db, user_id=owner["id"], chapter_id=chapter_id, ai_config_id=None, cipher=app.state.cipher,
                factory=create_provider, ai_timeout=3, allow_private=True, execution=execution, review_analysis=ref,
            )
        with pytest.raises(VisibilityPolicyError):
            async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
                async for _ in events:
                    pass
        # 与上一个用例相同：只统计带请求体的模型发送，忽略并行运行时无关的空请求。
        sent = [request for request in requests if request]
        assert changed and len(sent) == 1, requests
    await _assert_no_analysis_side_effects(app)


@pytest.mark.parametrize("outcome", ["committed", "rolled_back", "cancel_committed", "cancel_rolled_back"])
async def test_analysis_commit_ack_loss_preserves_original_execution(
    app, client, fake_llm, monkeypatch, outcome,
):
    world = await _world(app, client)
    _, _, _, _, execution = world
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json(summary="本轮分析结果"))
    original_commit = AsyncSession.commit
    lost = False

    async def uncertain_commit(db):
        nonlocal lost
        if not lost and any(isinstance(row, ChapterAnalysis) for row in db.identity_map.values()):
            lost = True
            if outcome in {"committed", "cancel_committed"}:
                await original_commit(db)
            else:
                await db.rollback()
            if outcome.startswith("cancel_"):
                raise asyncio.CancelledError
            raise RuntimeError("模拟提交确认丢失")
        return await original_commit(db)

    monkeypatch.setattr(AsyncSession, "commit", uncertain_commit)
    if outcome in {"committed", "cancel_committed"}:
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            result = [event async for event in events]
        assert result[-1][0] == "done" and result[-1][1]["summary"] == "本轮分析结果"
    else:
        with pytest.raises(analysis_service.AnalysisCommitUncertain):
            async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
                async for _ in events:
                    pass
    assert lost and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.status == "running" and task.execution_key == execution.execution_key
        count = await db.scalar(select(func.count()).select_from(ChapterAnalysis))
        if outcome in {"committed", "cancel_committed"}:
            assert count == 1 and "_analysis_completion" in task.checkpoint
            assert "_analysis_pending" not in task.checkpoint
        else:
            assert count == 0 and "_analysis_pending" in task.checkpoint
            assert "_analysis_completion" not in task.checkpoint
    response = await client.get(f"/api/v1/tasks/{execution.task_id}", params={"task_key": execution.task_key})
    assert response.status_code == 200, response.text
    assert "_analysis_pending" not in response.text and "_analysis_completion" not in response.text


async def test_analysis_commit_proof_outage_cannot_report_success(app, client, fake_llm, monkeypatch):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json())
    original_commit = AsyncSession.commit
    lost = False

    async def uncertain_commit(db):
        nonlocal lost
        if not lost and any(isinstance(row, ChapterAnalysis) for row in db.identity_map.values()):
            lost = True
            await original_commit(db)
            raise RuntimeError("模拟已提交但确认丢失")
        return await original_commit(db)

    async def unavailable_proof(*_args, **_kwargs):
        raise RuntimeError("模拟核验数据库不可用")

    monkeypatch.setattr(AsyncSession, "commit", uncertain_commit)
    monkeypatch.setattr(analysis_service, "completed_analysis_receipt", unavailable_proof)
    with pytest.raises(analysis_service.AnalysisCommitUncertain):
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            async for _ in events:
                pass
    assert lost and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        assert task.status == "running" and "_analysis_completion" in task.checkpoint
        assert await db.scalar(select(func.count()).select_from(ChapterAnalysis)) == 1


async def test_completed_analysis_job_cannot_be_sent_or_saved_again(app, client, fake_llm):
    world = await _world(app, client)
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json(summary="原结果"))
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        first = [event async for event in events]
    assert first[-1][0] == "done" and len(fake_llm.calls) == 1
    fake_llm.push(analysis_json(summary="不得重发"))
    with pytest.raises(analysis_service.AnalysisCommitUncertain):
        async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
            async for _ in events:
                pass
    assert len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(ChapterAnalysis)) == 1


async def test_engine_does_not_finalize_success_if_completion_receipt_changes(app, client, fake_llm, monkeypatch):
    await register(client, "analysis-finalize-owner")
    await create_ai_config(client)
    project = await create_project(client)
    chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "终态竞态", "content": "可分析的章节正文。",
    })
    assert chapter.status_code == 201, chapter.text
    engine = app.state.task_engine
    original = engine.complete_state
    reached = asyncio.Event()

    async def corrupt_before_finalize(task, *args, **kwargs):
        if task is not None and task.kind == "chapter.analyze" and kwargs.get("status") == "succeeded":
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task.id)
                checkpoint = dict(row.checkpoint)
                checkpoint["_analysis_completion"] = {
                    **checkpoint["_analysis_completion"], "analysis_hash": "0" * 64,
                }
                row.checkpoint = checkpoint
                await db.commit()
            result = await original(task, *args, **kwargs)
            reached.set()
            return result
        return await original(task, *args, **kwargs)

    monkeypatch.setattr(engine, "complete_state", corrupt_before_finalize)
    fake_llm.push([analysis_json(summary="已提交的分析")])
    accepted = await client.post(f"/api/v1/chapters/{chapter.json()['id']}/analyze", json={})
    assert accepted.status_code == 202, accepted.text
    ref = accepted.json()
    await asyncio.wait_for(reached.wait(), 10)
    for _ in range(500):
        if ref["id"] not in engine._running:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("原执行体未退出")
    response = await client.get(f"/api/v1/tasks/{ref['id']}", params={"task_key": ref["task_key"]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    assert response.json()["stage"] == "结果待核对" and "尚未确认" in response.json()["error"]
    assert (await client.post(f"/api/v1/tasks/{ref['id']}/retry", params={
        "task_key": ref["task_key"],
    })).status_code == 409
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, ref["id"])
        assert task.execution_key is not None and "_analysis_completion" in task.checkpoint
        assert await db.scalar(select(func.count()).select_from(ChapterAnalysis)) == 1


async def test_engine_never_publishes_analysis_delta_after_original_token_revoked(app, client, fake_llm):
    owner = await register(client, "analysis-stream-token-owner")
    await create_ai_config(client)
    project = await create_project(client)
    chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "逐事件核验", "content": "供分析的正文。",
    })
    assert chapter.status_code == 201, chapter.text
    gate = Gate()
    fake_llm.push(["授权有效时的片段", gate, "不得发送给旧会话的片段"])
    accepted = await client.post(f"/api/v1/chapters/{chapter.json()['id']}/analyze", json={})
    assert accepted.status_code == 202, accepted.text
    ref = accepted.json()
    stream = asyncio.create_task(client.get(
        f"/api/v1/tasks/{ref['id']}/stream", params={"task_key": ref["task_key"]},
    ))
    try:
        await gate.wait_reached(within=10)
        async with app.state.sessionmaker() as db:
            (await db.get(User, owner["id"])).token_version += 1
            await db.commit()
        gate.release()
        response = await asyncio.wait_for(stream, 15)
        assert response.status_code == 200
        assert "不得发送给旧会话的片段" not in response.text
        await _assert_no_analysis_side_effects(app)
    finally:
        gate.release()
        if not stream.done():
            stream.cancel()
            with suppress(asyncio.CancelledError):
                await stream


@pytest.mark.parametrize("change", ["summary", "plot_points"])
async def test_analysis_completed_receipt_rejects_changed_result(app, client, fake_llm, change):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json(summary="提交结果"))
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        result = [event async for event in events]
    analysis_id = result[-1][1]["analysis_id"]
    async with app.state.sessionmaker() as db:
        proof = await analysis_service.completed_analysis_receipt(db, execution)
        assert proof is not None and proof[1]["summary"] == "提交结果"
        await db.rollback()
    async with app.state.sessionmaker() as db:
        analysis = await db.get(ChapterAnalysis, analysis_id)
        if change == "summary":
            analysis.summary = "后来不同的内容"
        else:
            analysis.plot_points = [{"text": "后来的不同情节", "importance": 3}]
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert await analysis_service.completed_analysis_receipt(db, execution) is None


async def test_admin_drain_can_only_recover_proven_original_analysis(app, client, fake_llm):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json(summary="取消前已提交"))
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        result = [event async for event in events]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.admin_cancel_operation_id = str(uuid4())
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        with pytest.raises(VisibilityPolicyError):
            analysis_service.capture_analysis_execution(task)
        recovered = analysis_service.capture_analysis_completion_for_recovery(task)
        proof = await analysis_service.completed_analysis_receipt(db, recovered)
        assert proof is not None and proof[1] == result[-1][1]
        await db.rollback()
    async with app.state.sessionmaker() as db:
        with pytest.raises(VisibilityPolicyError):
            await analysis_service._analysis_write_barrier(db, recovered)


@pytest.mark.parametrize("change", [
    "none", "result", "task_result", "receipt", "boolean", "pending", "round", "execution", "input",
])
async def test_terminal_analysis_receipt_checks_original_result_and_round(app, client, fake_llm, change):
    world = await _world(app, client)
    execution = world[-1]
    job = await _job(app, fake_llm, world)
    fake_llm.push(analysis_json(summary="完成的分析"))
    async with aclosing(analysis_service.run_analysis_job(job, app.state.sessionmaker)) as events:
        result = [event async for event in events]
    analysis_id = result[-1][1]["analysis_id"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        task.status, task.execution_key, task.result = "succeeded", None, result[-1][1]
        if change == "receipt":
            checkpoint = dict(task.checkpoint)
            checkpoint["_analysis_completion"] = {**checkpoint["_analysis_completion"], "task_id": task.id + 1}
            task.checkpoint = checkpoint
        elif change == "boolean":
            checkpoint = dict(task.checkpoint)
            checkpoint["_analysis_completion"] = {**checkpoint["_analysis_completion"], "task_id": True}
            # Python considers True == 1; force the JSON write rather than relying on ORM dirty equality.
            await db.execute(update(Task).where(Task.id == task.id).values(checkpoint=checkpoint))
        elif change == "pending":
            task.checkpoint = {**task.checkpoint, "_analysis_pending": {"invalid": True}}
        elif change == "round":
            task.queued_at += timedelta(seconds=1)
        elif change == "execution":
            task.execution_key = str(uuid4())
        elif change == "result":
            (await db.get(ChapterAnalysis, analysis_id)).plot_points = [{"text": "被替换"}]
        elif change == "task_result":
            task.result = {**task.result, "summary": "不一致的结果"}
        elif change == "input":
            task.input = {**task.input, "request": {**task.input["request"], "ai_config_id": 999}}
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, execution.task_id)
        proof = await analysis_service.completed_terminal_analysis_receipt(db, task)
        assert (proof is not None) is (change == "none")
