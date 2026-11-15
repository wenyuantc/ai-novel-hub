"""编辑者章节执行证明：真实模型 HTTP，固定原任务、授权和日志实例。"""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update

from app.ai.providers import create_provider
from app.core.errors import ConflictError
from app.models import GenerationLog, ProjectCollaborator, Task, User
from app.schemas.chapter import GenerateChapterRequest, PolishChapterRequest
from app.services import chapter_claim, generation_service, project_ai_config
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.handlers.chapter import ChapterHandler, bind_queued_claim
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_collaboration_rewrite_identity import _http_clients, _world


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True}


async def _running_editor_task(app, project, chapter, actor, *, mode="polish", build=True):
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == actor["id"],
        ))
        grant = (member.invitation_id, member.revision)
        user = await db.get(User, actor["id"])
        binding = await project_ai_config.resolve_project_model(
            db, actor["id"], project["id"], "text", cipher=app.state.cipher, allow_private=True, for_update=True,
        )
        embedding = await project_ai_config.resolve_shared_embedding(
            db, actor["id"], project["id"], cipher=app.state.cipher, allow_private=True, for_update=True,
        )
        request_type = GenerateChapterRequest if mode == "generate" else PolishChapterRequest
        request = {"chapter_id": chapter["id"], **request_type().model_dump(mode="json"),
                   "target": {"kind": "chapter"}}
        task = Task(
            user_id=actor["id"], project_id=project["id"], chapter_id=chapter["id"], kind=f"chapter.{mode}",
            title="编辑者润色", resource_key=f"chapter:{chapter['id']}", status="queued", input={
                "chapter_id": chapter["id"], "prev_status": chapter["status"], "request": request,
                "_collaboration_grant": {"invitation_id": grant[0], "revision": grant[1]},
                "_task_actor_token_version": user.token_version,
                "_project_model": project_ai_config.serialize_project_model(binding),
                "_shared_embedding": project_ai_config.serialize_shared_embedding(embedding),
            },
        )
        db.add(task)
        await db.flush()
        queued = await bind_queued_claim(db, task)
        execution_key = str(uuid4())
        execution = from_queued_task(task, execution_key)
        task.status, task.execution_key = "running", execution_key
        await db.flush()
        claim = await chapter_claim.activate_queued_chapter(db, queued, execution_key=execution_key)
        await db.commit()
    job = None
    if build:
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, execution.task_id)
            job = await ChapterHandler().build_job(app.state.task_engine.ctx, task, claim=claim, execution=execution)
    return execution, claim, binding, job


async def test_editor_generation_never_calls_legacy_owner_retrieval(app, monkeypatch):
    """E4-f 起协作者 generate 可组装检索上下文，但只能用原执行冻结的共享向量选择与真实 actor；
    作者身份的旧检索（按 project.user_id 取向量模型）与「本人默认向量」均不得出现，
    无执行身份的直接调用仍在组装上下文前失败关闭。"""
    from app.services import kb_service, retrieval_service

    await app.state.task_engine.stop()
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            app.state.task_engine.ctx.factory = create_provider
            project, chapter, actor = await _world(owner, editor, url, source="owner")
            execution, claim, binding, _job = await _running_editor_task(
                app, project, chapter, actor, mode="generate", build=False,
            )
            contexts = []
            legacy = []

            async def recorded_context(*_args, **kwargs):
                contexts.append(kwargs)
                return "受控上下文"

            async def legacy_retrieval(*_args, **_kwargs):
                legacy.append(True)
                raise AssertionError("不得使用作者身份或本人默认向量的旧检索")

            monkeypatch.setattr(generation_service, "build_chapter_context", recorded_context)
            monkeypatch.setattr(retrieval_service, "get_embedder", legacy_retrieval)
            monkeypatch.setattr(retrieval_service, "default_embedding_config", legacy_retrieval)
            monkeypatch.setattr(kb_service, "knowledge_for_project", legacy_retrieval)
            async with app.state.sessionmaker() as db:
                # 无原执行身份的直接调用：仍在读取任何上下文前拒绝。
                with pytest.raises(ConflictError):
                    await generation_service.build_chapter_job(
                        db, user_id=actor["id"], chapter_id=chapter["id"], mode="generate",
                        req=GenerateChapterRequest(), prev_status=claim.prev_status, ai_config_id=None,
                        cipher=app.state.cipher, factory=create_provider, ai_timeout=5, allow_private=True,
                        context_budget=8000, task_id=execution.task_id, task_key=execution.task_key,
                        claim=claim, model_binding=binding,
                    )
            assert contexts == [] and legacy == []
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, execution.task_id)
                job = await ChapterHandler().build_job(
                    app.state.task_engine.ctx, task, claim=claim, execution=execution,
                )
            # 作者未配置向量：冻结选择为 keyword，不构造任何向量模型；上下文按真实 actor 组装。
            assert job.embedding_selection is not None and job.embedding_selection.mode == "keyword"
            assert len(contexts) == 1 and contexts[0]["embedder"] is None
            assert contexts[0]["actor_user_id"] == actor["id"]
            assert legacy == [] and not requests


@pytest.mark.parametrize("source", ["actor", "owner"])
async def test_editor_polish_receipt_binds_actor_grant_and_log_instances(app, source):
    await app.state.task_engine.stop()
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            app.state.task_engine.ctx.factory = create_provider
            project, chapter, actor = await _world(owner, editor, url, source=source)
            execution, claim, binding, job = await _running_editor_task(app, project, chapter, actor)
            events = [event async for event in generation_service.run_chapter_job(job, app.state.sessionmaker)]
            assert events[-1][0] == "done", events
            assert len(requests) == 1
            result = events[-1][1]
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, execution.task_id)
                log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task.id))
                receipt = task.checkpoint["_chapter_claim"]
                assert receipt["schema"] == 3 and receipt["grant"] == {
                    "invitation_id": claim.grant_identity[0], "revision": claim.grant_identity[1],
                }
                assert receipt["log_instance_key"] == log.log_instance_key
                assert (receipt["model_config_instance_key"] == log.model_config_instance_key
                        == binding.config_instance_key)
                assert log.actor_user_id == actor["id"] and log.model_source == source
                assert await generation_service.completed_running_chapter_receipt(db, execution, result) == receipt
            async with app.state.sessionmaker() as db:
                await db.execute(update(Task).where(Task.id == execution.task_id).values(
                    status="succeeded", result=result,
                ))
                await db.commit()
            async with app.state.sessionmaker() as db:
                assert await generation_service.completed_success_chapter_receipt(db, execution) == receipt
                task = await db.get(Task, execution.task_id)
                task.execution_key = None
                await db.commit()
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, execution.task_id)
                assert await generation_service.completed_terminal_chapter_receipt(db, task) == receipt
            removed = await owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}",
                params={"expected_revision": claim.grant_identity[1]},
            )
            assert removed.status_code == 204, removed.text
            config_owner = editor if source == "actor" else owner
            deleted_config = await config_owner.delete(f"/api/v1/ai-configs/{binding.config_id}")
            assert deleted_config.status_code == 204, deleted_config.text
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, execution.task_id)
                # 终态历史证明不因后续撤权或配置删除而猜测为另一轮任务。
                assert await generation_service.completed_terminal_chapter_receipt(db, task) == receipt
                altered = deepcopy(task.checkpoint)
                altered["_chapter_claim"]["grant"]["revision"] += 1
                await db.execute(update(Task).where(Task.id == task.id).values(checkpoint=altered))
                await db.commit()
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, execution.task_id)
                assert await generation_service.completed_terminal_chapter_receipt(db, task) is None


@pytest.mark.parametrize("damage", [
    "missing_log_key", "wrong_log_key", "wrong_config_key", "boolean_revision",
    "log_source", "log_owner", "log_model_instance",
])
async def test_editor_terminal_receipt_rejects_damaged_origin(app, damage):
    await app.state.task_engine.stop()
    async with _server(payload=_sse("openai")) as (url, _received, headers, body, _requests):
        headers.set()
        body.set()
        async with _http_clients(app) as (owner, editor):
            app.state.task_engine.ctx.factory = create_provider
            project, chapter, actor = await _world(owner, editor, url, source="owner")
            execution, _claim, _binding, job = await _running_editor_task(app, project, chapter, actor)
            events = [event async for event in generation_service.run_chapter_job(job, app.state.sessionmaker)]
            assert events[-1][0] == "done", events
            async with app.state.sessionmaker() as db:
                await db.execute(update(Task).where(Task.id == execution.task_id).values(
                    status="succeeded", execution_key=None, result=events[-1][1],
                ))
                await db.commit()
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, execution.task_id)
                assert await generation_service.completed_terminal_chapter_receipt(db, task) is not None
                if damage.startswith("log_"):
                    log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task.id))
                    changes = {
                        "log_source": {"model_source": "actor"},
                        "log_owner": {"model_config_owner_id": actor["id"]},
                        "log_model_instance": {"model_config_instance_key": str(uuid4())},
                    }
                    if damage == "log_model_instance":
                        # 模拟库外篡改：绕过应用层与数据库不可变触发器，直接改写已提交日志的模型实例键。
                        if db.bind.dialect.name == "postgresql":
                            await db.execute(text(
                                "DROP TRIGGER generation_logs_instance_keys_immutable ON generation_logs"
                            ))
                        else:
                            await db.execute(text("DROP TRIGGER generation_logs_instance_keys_immutable"))
                    await db.execute(update(GenerationLog).where(GenerationLog.id == log.id).values(**changes[damage]))
                else:
                    checkpoint = deepcopy(task.checkpoint)
                    receipt = checkpoint["_chapter_claim"]
                    if damage == "missing_log_key":
                        receipt.pop("log_instance_key")
                    elif damage == "wrong_log_key":
                        receipt["log_instance_key"] = str(uuid4())
                    elif damage == "wrong_config_key":
                        receipt["model_config_instance_key"] = str(uuid4())
                    else:
                        receipt["grant"]["revision"] = True
                    await db.execute(update(Task).where(Task.id == task.id).values(checkpoint=checkpoint))
                await db.commit()
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, execution.task_id)
                assert await generation_service.completed_terminal_chapter_receipt(db, task) is None
