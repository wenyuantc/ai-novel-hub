"""富文档提案只能由实际章节执行体持久化并按原轮次采纳。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from traceback import format_exception
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError, ConflictError
from app.models import AIConfig, Chapter, GenerationLog, Project, Scene, SceneReviewProposal, Task, User
from app.schemas.rich_document_review import ReviewAccept
from app.schemas.scene_ai import ChapterTarget, SceneTarget
from app.services import (
    analysis_service,
    authorization_service,
    chapter_claim,
    project_ai_config,
    rich_document_review,
    scene_ai_context,
)
from app.services.rich_document_proposal_mapping import build_scene_proposal
from app.services.rich_document_store import capture_documents
from app.services.task_engine import execution_identity
from tests.helpers import create_ai_config
from tests.test_rich_document_api import KEY, add_section, save_payload
from tests.test_scenes import setup_scene


async def _claimed_proposal_world(app, client):
    await app.state.task_engine.stop()
    project, chapter, url, scene = await setup_scene(client, "原稿🙂甲乙")
    config = await create_ai_config(client)
    document_url = f"{url}/scenes/{scene['id']}/document"
    payload = save_payload((await client.get(document_url)).json())
    add_section(payload, enabled=True)
    payload["document"]["sections"][0]["anchor"] = 2
    payload["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 1, "end": 4}]
    saved = await client.put(document_url, json=payload)
    assert saved.status_code == 200, saved.text
    source = saved.json()
    queued_at = datetime.now(UTC)
    execution = str(uuid4())
    async with app.state.sessionmaker() as db:
        actor_id = await db.scalar(select(Project.user_id).where(Project.id == project["id"]))
        model_binding = project_ai_config._bind(
            await db.get(Project, project["id"]), actor_id, None, await db.get(AIConfig, config["id"]), source="actor",
        )
        task = Task(user_id=actor_id, project_id=project["id"], chapter_id=chapter["id"],
                    kind="chapter.generate", status="queued", resource_key=f"chapter:{chapter['id']}",
                    queued_at=queued_at, input={"chapter_id": chapter["id"], "_task_actor_token_version": 0,
                                                "request": {
                        "project_id": project["id"], "chapter_id": chapter["id"],
                    }})
        db.add(task)
        await db.flush()
        claim = await chapter_claim.claim_queued_chapter(
            db, task_id=task.id, task_key=task.statistics_key, actor_id=actor_id, project_id=project["id"],
            chapter_id=chapter["id"], queued_at=queued_at, grant_identity=None,
        )
        original_execution = execution_identity.from_queued_task(task, execution)
        await db.execute(update(Task).where(Task.id == task.id).values(status="running", execution_key=execution))
        claim = await chapter_claim.activate_queued_chapter(db, claim, execution_key=execution)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key
    async with app.state.sessionmaker() as db:
        stamp = await scene_ai_context.read_stamp(db, project["id"], chapter["id"])
        row = await db.get(Scene, scene["id"])
        documents, _ = await capture_documents(db, [row])
        plan = build_scene_proposal(documents[row.id], "完全不同的候选稿")
        assert plan.requires_review
    job = SimpleNamespace(
        task_id=task_id, task_key=task_key, chapter_id=chapter["id"], project_id=project["id"],
        user_id=actor_id, chapter_claim=claim, model_binding=model_binding, prev_status="draft", context_stamp=stamp,
        execution_identity=original_execution,
        mode="generate",
        run_key=str(uuid4()), target=SceneTarget(kind="scene", scene_id=scene["id"]),
        scenes=[{"id": scene["id"], "separator_before": row.separator_before}],
    )
    logs = {"project_id": project["id"], "chapter_id": chapter["id"], "task_id": task_id,
            "actor_user_id": actor_id, "kind": "generate", "status": "success", "ai_config_id": config["id"],
            "provider": config["provider"], "model": config["model"], "params": {}, "messages": []}
    return job, {scene["id"]: plan}, logs, document_url, source, scene


async def _persist(app, job, plans, logs, *, pause=False):
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, job.chapter_id)
        identity = await rich_document_review.persist_pending(db, chapter, job, plans, logs)
        if pause:
            # 模拟调度器在本执行体持久暂停后释放 execution_key。
            task = await db.get(Task, job.task_id)
            task.status, task.execution_key = "paused", None
        await db.commit()
        return identity


@pytest.mark.parametrize("change", ["none", "claim", "event", "input", "checkpoint"])
async def test_single_pending_event_requires_original_claim_and_exact_transition(app, client, change):
    job, plans, logs, _document_url, _source, _scene = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs)
    claim, event = job.chapter_claim, dict(identity)
    if change == "claim":
        claim = replace(claim, claim_key=str(uuid4()))
    elif change == "event":
        event["run_key"] = str(uuid4())
    elif change in {"input", "checkpoint"}:
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, job.task_id)
            if change == "input":
                task.input = {**task.input, "unexpected": "changed"}
            else:
                task.checkpoint = {**task.checkpoint, "unexpected": "changed"}
            await db.commit()
    async with app.state.sessionmaker() as db:
        if change == "none":
            await rich_document_review.verify_single_pending_event(db, job.execution_identity, claim, event)
        else:
            with pytest.raises(ConflictError):
                await rich_document_review.verify_single_pending_event(db, job.execution_identity, claim, event)


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
@pytest.mark.parametrize("invalidate", [False, True])
async def test_accepted_proposal_analysis_binds_accepting_request_token(app, client, monkeypatch, invalidate):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    job.target = ChapterTarget()
    identity = await _persist(app, job, plans, logs, pause=True)
    captured = []
    original = analysis_service.enqueue_auto_in_transaction

    async def observe(db, project, chapter_id, *, actor=None, cipher=None, allow_private=False):
        captured.append(actor)
        if actor is None:
            return None  # 旧调用不会传本次认证快照，本测试必须暴露而非猜测当前用户。
        if invalidate:
            await db.execute(update(User).where(User.id == actor.id).values(token_version=actor.token_version + 1))
        return await original(db, project, chapter_id, actor=actor, cipher=cipher, allow_private=allow_private)

    monkeypatch.setattr(analysis_service, "enqueue_auto_in_transaction", observe)
    response = await client.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 2}],
        "confirm_format_loss": True,
    })
    assert len(captured) == 1 and type(captured[0]) is authorization_service.AuthenticatedActor
    assert captured[0].id == job.user_id and captured[0].token_version == 0
    if invalidate:
        assert response.status_code == 401
        assert (await client.get(document_url)).json() == source
    else:
        assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        children = list(await db.scalars(select(Task).where(Task.kind == "chapter.analyze")))
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        if invalidate:
            assert not children and proposal.status == "pending"
            assert (await db.get(User, job.user_id)).token_version == 0
        else:
            assert len(children) == 1 and children[0].user_id == job.user_id
            assert children[0].input["_task_actor_token_version"] == captured[0].token_version
            assert proposal.status == "applied"


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])
async def test_accepted_proposal_warns_when_auto_analysis_model_is_unavailable(app, client):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    job.target = ChapterTarget()
    identity = await _persist(app, job, plans, logs, pause=True)
    assert (await client.delete(f"/api/v1/ai-configs/{logs['ai_config_id']}")).status_code == 204
    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
    body = {"task_key": identity["task_key"], "run_key": identity["run_key"],
            "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 2}],
            "confirm_format_loss": True}
    response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json().get("analysis_warning") == "自动分析未排队，请检查模型设置后手动分析"
    assert (await client.get(document_url)).json()["document"]["content"] != source["document"]["content"]
    repeated = await client.post(path, json=body)
    assert repeated.status_code == 200 and repeated.json() == response.json()
    async with app.state.sessionmaker() as db:
        assert not list(await db.scalars(select(Task).where(Task.kind == "chapter.analyze")))
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "applied"


async def test_accepted_proposal_does_not_rebind_log_to_reused_model_id(app, client):
    job, plans, logs, _document_url, _source, scene = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs, pause=True)
    async with app.state.sessionmaker() as db:
        config = await db.get(AIConfig, logs["ai_config_id"])
        original_key, fingerprint = config.instance_key, project_ai_config._fingerprint(config)
        values = {column.key: getattr(config, column.key) for column in AIConfig.__mapper__.column_attrs
                  if column.key not in {"id", "instance_key", "created_at", "updated_at"}}
        config_id = config.id
        await db.delete(config)
        await db.flush()
        replacement = AIConfig(id=config_id, instance_key=str(uuid4()), **values)
        db.add(replacement)
        await db.flush()
        assert replacement.instance_key != original_key and project_ai_config._fingerprint(replacement) == fingerprint
        await db.commit()
    response = await client.post(f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept", json={
        "task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 2}],
        "confirm_format_loss": True,
    })
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == job.task_id))
        assert log.ai_config_id is None and log.model_source == "actor" and log.model_config_owner_id == job.user_id


@pytest.mark.parametrize("change", ["claim", "queued_at", "execution", "task_id", "chapter_id", "no_claim"])
async def test_stale_execution_cannot_persist_or_release_another_claim(app, client, change):
    job, plans, logs, document_url, source, _ = await _claimed_proposal_world(app, client)
    claim = job.chapter_claim
    if change == "claim":
        job.chapter_claim = replace(claim, claim_key=str(uuid4()))
    elif change == "queued_at":
        job.chapter_claim = replace(claim, queued_at=claim.queued_at + timedelta(microseconds=1))
    elif change == "execution":
        job.chapter_claim = replace(claim, execution_key=str(uuid4()))
    elif change == "task_id":
        job.task_id += 100
    elif change == "chapter_id":
        job.chapter_id += 100
    else:
        job.chapter_claim = None
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, claim.chapter_id)
        with pytest.raises(ConflictError):
            await rich_document_review.persist_pending(db, chapter, job, plans, logs)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, claim.chapter_id)
        task = await db.get(Task, claim.task_id)
        assert (chapter.status, chapter.generation_claim_key, chapter.content) == (
            "generating", claim.claim_key, "原稿🙂甲乙",
        )
        assert task.input["_chapter_claim"]["claim_key"] == claim.claim_key
        assert await db.scalar(select(func.count()).select_from(SceneReviewProposal)) == 0
    assert (await client.get(document_url)).json() == source


async def test_proposal_requires_fresh_content_stamp_even_if_claim_matches(app, client):
    job, plans, logs, _, _, _ = await _claimed_proposal_world(app, client)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == job.chapter_id).values(
            content_revision=Chapter.content_revision + 1,
        ))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await rich_document_review.persist_pending(db, await db.get(Chapter, job.chapter_id), job, plans, logs)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, job.chapter_id)).generation_claim_key == job.chapter_claim.claim_key
        assert await db.scalar(select(func.count()).select_from(SceneReviewProposal)) == 0


async def test_proposal_atomically_releases_exact_claim(app, client):
    job, plans, logs, document_url, source, _ = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs)
    async with app.state.sessionmaker() as db:
        chapter, task = await db.get(Chapter, job.chapter_id), await db.get(Task, job.task_id)
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert (chapter.status, chapter.generation_claim_key, chapter.generation_claim_task_key,
                chapter.generation_claim_prev_status) == ("draft", None, None, None)
        assert "_chapter_claim" not in task.input
        assert proposal.status == "pending" and task.checkpoint["rich_review"]["token"] == identity["token"]
    assert (await client.get(document_url)).json() == source


async def test_claimed_proposal_requires_server_model_binding(app, client):
    job, plans, logs, document_url, source, _ = await _claimed_proposal_world(app, client)
    job.model_binding = None
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await rich_document_review.persist_pending(db, await db.get(Chapter, job.chapter_id), job, plans, logs)
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, job.chapter_id)
        assert chapter.status == "generating" and chapter.generation_claim_key == job.chapter_claim.claim_key
        assert await db.scalar(select(func.count()).select_from(SceneReviewProposal)) == 0
    assert (await client.get(document_url)).json() == source


async def test_payload_failure_cannot_commit_a_partial_claim_release(app, client, monkeypatch):
    job, plans, logs, document_url, source, _ = await _claimed_proposal_world(app, client)
    original = rich_document_review._write_payload

    async def fail_after_write(db, proposal, payload):
        await original(db, proposal, payload)
        raise RuntimeError("injected payload failure")

    monkeypatch.setattr(rich_document_review, "_write_payload", fail_after_write)
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError, match="injected payload failure"):
            await rich_document_review.persist_pending(db, await db.get(Chapter, job.chapter_id), job, plans, logs)
        # 即使调用方捕获异常并错误地提交，也不能提交提案或半个释放。
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter, task = await db.get(Chapter, job.chapter_id), await db.get(Task, job.task_id)
        assert chapter.status == "generating" and chapter.generation_claim_key == job.chapter_claim.claim_key
        assert task.input["_chapter_claim"]["claim_key"] == job.chapter_claim.claim_key
        assert await db.scalar(select(func.count()).select_from(SceneReviewProposal)) == 0
    assert (await client.get(document_url)).json() == source


async def test_proposal_database_error_does_not_expose_private_sql(app, client, monkeypatch):
    job, plans, logs, _document_url, _source, _ = await _claimed_proposal_world(app, client)
    secret = "候选正文与私有配置指纹"

    async def fail_write(db, proposal, payload):
        await db.flush()
        raise IntegrityError("INSERT proposal", {"payload": secret}, RuntimeError(secret))

    monkeypatch.setattr(rich_document_review, "_write_payload", fail_write)
    async with app.state.sessionmaker() as db:
        with pytest.raises(AppError) as error:
            await rich_document_review.persist_pending(db, await db.get(Chapter, job.chapter_id), job, plans, logs)
        assert secret not in "".join(format_exception(error.value))
        assert error.value.__context__ is None
        await db.commit()
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, job.chapter_id)
        assert chapter.status == "generating" and chapter.generation_claim_key == job.chapter_claim.claim_key
        assert await db.scalar(select(func.count()).select_from(SceneReviewProposal)) == 0


@pytest.mark.parametrize("change", ["queued_at", "execution", "grant", "missing_receipt", "receipt_actor_bool"])
async def test_running_proposal_recovery_requires_original_execution_facts(app, client, change):
    job, plans, logs, _document_url, _source, _ = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, job.task_id)
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        if change == "queued_at":
            task.queued_at += timedelta(microseconds=1)
        elif change == "execution":
            task.execution_key = str(uuid4())
        elif change == "grant":
            task.input = {**task.input, "_collaboration_grant": {"invitation_id": str(uuid4()), "revision": 1}}
        else:
            if change == "missing_receipt":
                payload.pop("execution_receipt")
            else:
                assert job.user_id == 1
                payload["execution_receipt"]["actor_id"] = True
            proposal.payload = payload
        await db.commit()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, job.task_id)
        with pytest.raises(ConflictError):
            await rich_document_review.recover_pending_identity(db, task)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending"


async def test_running_proposal_recovery_accepts_original_execution(app, client):
    job, plans, logs, _document_url, _source, _ = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, job.task_id)
        assert await rich_document_review.recover_pending_identity(db, task) == identity


@pytest.mark.parametrize("change", ["queued_at", "execution", "chapter_id", "grant", "task_key"])
async def test_accept_rejects_replaced_task_round_without_changing_proposal(app, client, change):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs, pause=True)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, job.task_id)
        if change == "queued_at":
            task.queued_at += timedelta(microseconds=1)
        elif change == "execution":
            task.execution_key = str(uuid4())
        elif change == "chapter_id":
            task.chapter_id = None
        elif change == "grant":
            task.input = {**task.input, "_collaboration_grant": {"invitation_id": str(uuid4()), "revision": 1}}
        else:
            task.statistics_key = str(uuid4())
        await db.commit()
    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
    result = await client.post(path, json={"task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}], "confirm_format_loss": True})
    # 作者任务被注入协作授权属于身份不符：_accept_review 先按授权来源隐藏为 404，与 task_key 替换同类。
    assert result.status_code == (404 if change in ("task_key", "grant") else 409), result.text
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending"


async def test_accept_does_not_overwrite_later_chapter_claim(app, client):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs, pause=True)
    new_execution, new_queued = str(uuid4()), datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=job.user_id, project_id=job.project_id, chapter_id=None, kind="chapter.batch",
                    status="running", resource_key=f"batch:{job.project_id}", queued_at=new_queued,
                    execution_key=new_execution, input={"request": {"project_id": job.project_id},
                                                       "chapter_ids": [job.chapter_id]},
                    checkpoint={"current": None})
        db.add(task)
        await db.flush()
        new_claim = await chapter_claim.claim_batch_chapter(
            db, task_id=task.id, task_key=task.statistics_key, actor_id=job.user_id, project_id=job.project_id,
            chapter_id=job.chapter_id, queued_at=new_queued, execution_key=new_execution, grant_identity=None,
        )
        await db.commit()
    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
    result = await client.post(path, json={"task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}], "confirm_format_loss": True})
    assert result.status_code == 409, result.text
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, job.chapter_id)
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert chapter.status == "generating" and chapter.generation_claim_key == new_claim.claim_key
        assert proposal.status == "pending"


async def test_accept_rejects_boolean_in_execution_receipt_identity(app, client):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    assert job.user_id == 1
    identity = await _persist(app, job, plans, logs, pause=True)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        payload["execution_receipt"]["actor_id"] = True
        proposal.payload = payload
        await db.commit()
    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
    result = await client.post(path, json={"task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}], "confirm_format_loss": True})
    assert result.status_code == 409, result.text
    assert (await client.get(document_url)).json() == source


async def test_accept_rejects_boolean_in_generation_log_identity(app, client):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    assert job.user_id == 1
    identity = await _persist(app, job, plans, logs, pause=True)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        payload = await rich_document_review._read_payload(db, proposal)
        payload["log_values"]["actor_user_id"] = True
        proposal.payload = payload
        await db.commit()
    path = f"/api/v1/tasks/{job.task_id}/rich-proposals/{identity['token']}/accept"
    result = await client.post(path, json={"task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}], "confirm_format_loss": True})
    assert result.status_code == 409, result.text
    assert (await client.get(document_url)).json() == source


async def test_accept_failure_rolls_back_already_flushed_document(app, client):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    logs["actor_user_id"] = job.user_id + 100
    identity = await _persist(app, job, plans, logs, pause=True)
    decision = ReviewAccept.model_validate({"task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}], "confirm_format_loss": True})
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await rich_document_review.accept_review(
                db, app.state.task_engine, job.user_id, job.task_id, identity["token"], decision,
            )
        await db.commit()
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending" and (await db.get(Task, job.task_id)).status == "paused"


async def test_accept_commit_error_does_not_expose_private_sql(app, client, monkeypatch):
    job, plans, logs, document_url, source, scene = await _claimed_proposal_world(app, client)
    identity = await _persist(app, job, plans, logs, pause=True)
    decision = ReviewAccept.model_validate({"task_key": identity["task_key"], "run_key": identity["run_key"],
        "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 3}], "confirm_format_loss": True})
    secret = "不能泄露的候选和模型配置"

    async def fail_commit(db):
        await db.flush()
        raise IntegrityError("UPDATE proposal", {"payload": secret}, RuntimeError(secret))

    monkeypatch.setattr(rich_document_review, "commit_versioned", fail_commit)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError) as error:
            await rich_document_review.accept_review(
                db, app.state.task_engine, job.user_id, job.task_id, identity["token"], decision,
            )
        assert secret not in "".join(format_exception(error.value))
        assert error.value.__context__ is None
        await db.commit()
    assert (await client.get(document_url)).json() == source
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == identity["token"]))
        assert proposal.status == "pending"
