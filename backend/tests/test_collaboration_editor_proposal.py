"""编辑者提案采纳只能使用原 actor、邀请轮次和完整章节执行凭据。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import AppError
from app.models import (
    Chapter,
    ChapterAnalysis,
    ChapterVersion,
    GenerationLog,
    Scene,
    SceneReviewProposal,
    Task,
    User,
)
from app.schemas.scene_ai import ChapterTarget
from app.services import analysis_service, chapter_claim, project_ai_config, rich_document_review, scene_ai_context
from app.services.rich_document_proposal_mapping import build_scene_proposal
from app.services.rich_document_store import capture_documents
from app.services.task_engine import execution_identity
from tests.helpers import analysis_json, create_ai_config, register, wait_task
from tests.test_collaboration_review_claim import _persist
from tests.test_rich_document_api import KEY, add_section, save_payload
from tests.test_scenes import setup_scene


async def _editor_proposal(
    app, owner, editor, *, allow_owner_ai=False, editor_config=True, owner_embedding=False, owner_style=False,
):
    await app.state.task_engine.stop()
    project, chapter, url, scene = await setup_scene(owner, "作者原稿🙂")
    editor_user = await register(editor, "proposal-editor")
    owner_config = await create_ai_config(owner) if not editor_config else None
    if owner_embedding:
        await create_ai_config(owner, model_type="embedding", model="owner-embedding")
    selected_config = await create_ai_config(editor) if editor_config else owner_config
    assert selected_config is not None
    style_id = None
    if owner_style:
        response = await owner.post("/api/v1/styles", json={"name": "作者私有风格", "description": "私有风格描述"})
        assert response.status_code == 201, response.text
        style_id = response.json()["id"]
        async with app.state.sessionmaker() as db:
            (await db.get(Chapter, chapter["id"])).style_id = style_id
            (await db.get(User, editor_user["id"])).is_admin = True
            await db.commit()
    invitation = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "proposal-editor", "role": "editor", "allow_owner_ai": allow_owner_ai,
    })
    assert invitation.status_code == 201, invitation.text
    invited = invitation.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invited['invitation_id']}/accept",
        json={"expected_revision": invited["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    grant = (member["invitation_id"], member["revision"])
    document_url = f"{url}/scenes/{scene['id']}/document"
    document = save_payload((await owner.get(document_url)).json())
    add_section(document, enabled=True)
    document["document"]["sections"][0]["anchor"] = 2
    saved = await owner.put(document_url, json=document)
    assert saved.status_code == 200, saved.text
    source = saved.json()
    queued_at, execution = datetime.now(UTC), str(uuid4())
    async with app.state.sessionmaker() as db:
        binding = await project_ai_config.resolve_project_model(
            db, editor_user["id"], project["id"], "text", cipher=app.state.cipher,
            config_id=selected_config["id"] if editor_config else None, for_update=True,
        )
        selection = await project_ai_config.resolve_shared_embedding(
            db, editor_user["id"], project["id"], cipher=app.state.cipher, for_update=True,
        )
        task = Task(
            user_id=editor_user["id"], project_id=project["id"], chapter_id=chapter["id"],
            kind="chapter.generate", status="queued", resource_key=f"chapter:{chapter['id']}", queued_at=queued_at,
            input={"chapter_id": chapter["id"], "request": {"project_id": project["id"], "chapter_id": chapter["id"]},
                   "_task_actor_token_version": 0,
                   "_collaboration_grant": {"invitation_id": grant[0], "revision": grant[1]},
                   "_project_model": project_ai_config.serialize_project_model(binding),
                   "_shared_embedding": project_ai_config.serialize_shared_embedding(selection)},
        )
        db.add(task)
        await db.flush()
        claim = await chapter_claim.claim_queued_chapter(
            db, task_id=task.id, task_key=task.statistics_key, actor_id=editor_user["id"], project_id=project["id"],
            chapter_id=chapter["id"], queued_at=queued_at, grant_identity=grant,
        )
        await db.execute(update(Task).where(Task.id == task.id).values(status="running", execution_key=execution))
        claim = await chapter_claim.activate_queued_chapter(db, claim, execution_key=execution)
        task_id, task_key = task.id, task.statistics_key
        await db.commit()
    async with app.state.sessionmaker() as db:
        stamp = await scene_ai_context.read_stamp(db, project["id"], chapter["id"])
        row = await db.get(Scene, scene["id"])
        docs, _ = await capture_documents(db, [row])
        plan = build_scene_proposal(docs[row.id], "编辑者生成的候选正文")
        assert plan.requires_review
    job = SimpleNamespace(
        task_id=task_id, task_key=task_key, chapter_id=chapter["id"], project_id=project["id"],
        user_id=editor_user["id"], chapter_claim=claim, model_binding=binding, prev_status="draft", context_stamp=stamp,
        mode="generate", run_key=str(uuid4()), target=ChapterTarget(),
        scenes=[{"id": scene["id"], "separator_before": row.separator_before}],
    )
    logs = {"project_id": project["id"], "chapter_id": chapter["id"], "task_id": task_id,
            "actor_user_id": editor_user["id"], "kind": "generate", "status": "success",
            "ai_config_id": selected_config["id"], "provider": selected_config["provider"],
            "model": selected_config["model"], "params": {}, "messages": []}
    identity = await _persist(app, job, {scene["id"]: plan}, logs, pause=True)
    body = {"task_key": identity["task_key"], "run_key": identity["run_key"],
            "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 2}],
            "confirm_format_loss": True}
    return project, chapter, editor_user, member, document_url, source, job, identity, body


async def test_editor_accepts_own_proposal_and_preserves_real_log_actor(app, client, make_client):
    editor = make_client()
    project, chapter, editor_user, member, document_url, _source, job, identity, body = await _editor_proposal(
        app, client, editor,
    )
    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}"
    query = {"task_key": identity["task_key"], "run_key": identity["run_key"]}
    assert (await editor.get(path, params=query)).status_code == 200
    assert (await client.get(path, params=query)).status_code == 404
    response = await editor.post(f"{path}/accept", json=body)
    assert response.status_code == 200, response.text
    assert (await client.get(document_url)).json()["document"]["content"] == "编辑者生成的候选正文"
    assert (await editor.post(f"{path}/accept", json=body)).json() == response.json()
    async with app.state.sessionmaker() as db:
        logs = list(await db.scalars(select(GenerationLog).where(GenerationLog.task_id == job.task_id)))
        versions = list(await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter["id"])))
        assert len(logs) == 1 and logs[0].actor_user_id == editor_user["id"] and logs[0].model_source == "actor"
        assert any(version.generation_log_id == logs[0].id for version in versions)
    assert (await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}", params={
        "expected_revision": member["revision"],
    })).status_code == 204
    new = (await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "proposal-editor", "role": "editor",
    })).json()
    assert (await editor.post(f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept", json={
        "expected_revision": new["revision"],
    })).status_code == 200
    assert (await editor.post(f"{path}/accept", json=body)).status_code == 404


async def test_editor_admin_accept_result_hides_owner_private_style(app, client, make_client):
    editor = make_client()
    _project, _chapter, _editor_user, _member, _url, _source, job, identity, body = await _editor_proposal(
        app, client, editor, owner_style=True,
    )
    response = await editor.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body)
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, job.task_id)
        assert task.result["chapter"]["style_id"] is None
        assert "作者私有风格" not in str(task.result) and "私有风格描述" not in str(task.result)


@pytest.mark.parametrize("change", ["revoke", "downgrade", "reinvite"])
async def test_editor_cannot_accept_old_proposal_after_grant_changes(app, client, make_client, change):
    editor = make_client()
    project, chapter, editor_user, member, document_url, source, job, identity, body = await _editor_proposal(
        app, client, editor,
    )
    base = f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}"
    if change == "downgrade":
        changed = await client.patch(base, json={"expected_revision": member["revision"], "role": "viewer"})
        assert changed.status_code == 200
    else:
        assert (await client.delete(base, params={"expected_revision": member["revision"]})).status_code == 204
        if change == "reinvite":
            new = (await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
                "username": "proposal-editor", "role": "editor",
            })).json()
            assert (await editor.post(f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept", json={
                "expected_revision": new["revision"],
            })).status_code == 200
    response = await editor.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body)
    assert response.status_code == 404
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending" and (await db.get(Chapter, chapter["id"])).content == "作者原稿🙂"
        assert not list(await db.scalars(select(GenerationLog).where(GenerationLog.task_id == job.task_id)))


async def test_editor_revoked_between_initial_read_and_lock_cannot_accept(app, client, make_client, monkeypatch):
    editor = make_client()
    project, chapter, editor_user, member, document_url, source, job, identity, body = await _editor_proposal(
        app, client, editor,
    )
    original = rich_document_review._owned_proposal
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_initial(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        if not kwargs.get("lock", False):
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(rich_document_review, "_owned_proposal", hold_initial)
    pending = asyncio.create_task(editor.post(
        f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        revoked = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204
        release.set()
        response = await asyncio.wait_for(pending, 10)
        assert response.status_code == 404
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending" and (await db.get(Chapter, chapter["id"])).content == "作者原稿🙂"


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("change", ["none", "grant", "token", "input", "receipt", "version", "log"])
async def test_editor_auto_source_proves_original_proposal_in_accept_transaction(
    app, client, make_client, monkeypatch, change,
):
    editor = make_client()
    project, chapter, editor_user, member, document_url, source, job, identity, body = await _editor_proposal(
        app, client, editor,
    )
    captured = []

    async def check(db, project_row, chapter_id, *, actor, cipher, allow_private, review_source):
        captured.append(review_source)
        modified = {
            "grant": {"grant_identity": (str(uuid4()), member["revision"])},
            "token": {"actor_token_version": review_source.actor_token_version + 1},
            "input": {"input_json": "{}"}, "receipt": {"receipt_json": "{}"},
            "version": {"version_id": review_source.version_id + 100}, "log": {"log_id": review_source.log_id + 100},
        }.get(change, {})
        proof = replace(review_source, **modified)
        locked_project, original_input, grant = await rich_document_review.require_review_auto_source(db, proof, actor)
        assert locked_project.id == project["id"] and chapter_id == chapter["id"]
        assert original_input["_collaboration_grant"]["invitation_id"] == member["invitation_id"]
        assert grant == (member["invitation_id"], member["revision"])
        return None

    monkeypatch.setattr(analysis_service, "enqueue_auto_in_transaction", check)
    response = await editor.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body)
    assert len(captured) == 1
    if change == "none":
        assert response.status_code == 200, response.text
        assert (await client.get(document_url)).json()["document"]["content"] == "编辑者生成的候选正文"
    else:
        assert response.status_code in {403, 409}, response.text
        assert (await client.get(document_url)).json() == source
        async with app.state.sessionmaker() as db:
            proposal = await db.scalar(
                select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"])
            )
            assert proposal.status == "pending"
            assert not list(await db.scalars(select(GenerationLog).where(GenerationLog.task_id == job.task_id)))


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_editor_acceptance_enqueues_analysis_for_original_actor_and_grant(app, client, make_client, fake_llm):
    editor = make_client()
    project, chapter, editor_user, member, _url, _source, job, identity, body = await _editor_proposal(
        app, client, editor,
    )
    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
    response = await editor.post(path, json=body)
    assert response.status_code == 200, response.text
    repeat = await editor.post(path, json=body)
    assert repeat.status_code == 200 and repeat.json() == response.json()
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, job.task_id)
        children = list(await db.scalars(select(Task).where(Task.kind == "chapter.analyze")))
        assert len(children) == 1
        child = children[0]
        assert (child.user_id, child.project_id, child.chapter_id) == (editor_user["id"], project["id"], chapter["id"])
        assert child.input["_task_actor_token_version"] == 0
        assert child.input["_collaboration_grant"] == {
            "invitation_id": member["invitation_id"], "revision": member["revision"],
        }
        assert child.input["_project_model"] == original.input["_project_model"]
        assert child.input["_shared_embedding"] == original.input["_shared_embedding"]
    assert not fake_llm.calls


# E4-g1：AnalysisHandler 已声明 collaboration_execution_ready，提案采纳派生的协作分析按原 actor/grant/冻结模型执行。
@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("owner_fallback", [False, True])
async def test_editor_proposal_analysis_runs_as_original_actor_with_frozen_model(
    app, client, make_client, fake_llm, owner_fallback,
):
    editor = make_client()
    project, chapter, editor_user, member, _url, _source, job, identity, body = await _editor_proposal(
        app, client, editor, allow_owner_ai=owner_fallback, editor_config=not owner_fallback,
        owner_embedding=owner_fallback,
    )
    response = await editor.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body)
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        child = await db.scalar(select(Task).where(Task.kind == "chapter.analyze"))
        child_ref = {"id": child.id, "task_key": child.statistics_key}
        assert child.user_id == editor_user["id"]
        assert child.input["_collaboration_grant"]["revision"] == member["revision"]
        assert child.input["_project_model"]["source"] == ("owner" if owner_fallback else "actor")
    fake_llm.push(analysis_json(summary="编辑者触发的自动分析"))
    await app.state.task_engine.start()
    finished = await wait_task(editor, child_ref, within=25, statuses=("succeeded", "failed"))
    assert finished["status"] == "succeeded" and len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        result = await db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.task_id == child_ref["id"]))
        assert result is not None and result.project_id == project["id"] and result.chapter_id == chapter["id"]


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("change", ["none", "source_task", "proposal", "chapter", "version", "grant", "token", "child"])
async def test_editor_analysis_requires_original_applied_proposal_source(app, client, make_client, change):
    editor = make_client()
    project, chapter, editor_user, member, _url, _source, job, identity, body = await _editor_proposal(
        app, client, editor,
    )
    response = await editor.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body)
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        child = await db.scalar(select(Task).where(Task.kind == "chapter.analyze"))
        raw_source = child.input["_analysis_proposal_source"]
        assert "编辑者生成的候选正文" not in str(raw_source)
        frozen = rich_document_review.capture_applied_review_source(raw_source)
        running_key = str(uuid4())
        claimed = execution_identity.from_queued_task(child, running_key)
        await db.execute(update(Task).where(Task.id == child.id).values(status="running", execution_key=running_key))
        await db.commit()
    execution = analysis_service.analysis_execution_from_claim(claimed)
    if change == "grant":
        assert (await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}", params={
            "expected_revision": member["revision"],
        })).status_code == 204
    elif change != "none":
        async with app.state.sessionmaker() as db:
            if change == "source_task":
                (await db.get(Task, job.task_id)).statistics_key = str(uuid4())
            elif change == "proposal":
                proposal = await db.scalar(
                    select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"])
                )
                proposal.status = "discarded"
            elif change == "chapter":
                (await db.get(Chapter, chapter["id"])).content_revision += 1
            elif change == "version":
                await db.delete(await db.get(ChapterVersion, raw_source["version_id"]))
            elif change == "token":
                (await db.get(User, editor_user["id"])).token_version += 1
            else:
                child = await db.get(Task, execution.task_id)
                child.input = {**child.input, "unexpected": "changed"}
            await db.commit()
    async with app.state.sessionmaker() as db:
        if change == "none":
            await rich_document_review.require_applied_review_analysis_source(db, frozen, execution, read_lock=False)
        else:
            with pytest.raises(AppError):
                await rich_document_review.require_applied_review_analysis_source(
                    db, frozen, execution, read_lock=False
                )


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("change", ["extra", "boolean", "grant", "uuid", "hash", "revision"])
async def test_editor_analysis_source_rejects_malformed_identity(app, client, make_client, change):
    editor = make_client()
    _project, _chapter, _editor_user, _member, _url, _source, job, identity, body = await _editor_proposal(
        app, client, editor,
    )
    assert (await editor.post(
        f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json=body,
    )).status_code == 200
    async with app.state.sessionmaker() as db:
        child = await db.scalar(select(Task).where(Task.kind == "chapter.analyze"))
        raw = dict(child.input["_analysis_proposal_source"])
    if change == "extra":
        raw["unexpected"] = "private"
    elif change == "boolean":
        raw["actor_token_version"] = True
    elif change == "grant":
        raw["grant"] = [raw["grant"][0], True]
    elif change == "uuid":
        raw["claim_key"] = "not-a-uuid"
    elif change == "hash":
        raw["receipt_hash"] = "bad"
    else:
        raw["applied_revision"] += 1
    with pytest.raises(AppError):
        rich_document_review.capture_applied_review_source(raw)
