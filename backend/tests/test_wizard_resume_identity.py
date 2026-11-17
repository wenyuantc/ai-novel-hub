"""向导续写必须使用客户端实际看到的不可复用旧任务身份。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import BadRequestError
from app.models import KbDocument, Outline, Project, StoryMemory, Task, User
from app.services import project_ai_config
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.collaboration_ai_guard import SendGuard
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.handlers.wizard import WizardHandler
from app.services.wizard_service import WizardResumeChanged, run_wizard_job
from tests.helpers import Gate, create_ai_config, create_project, register


async def _source(app, client):
    owner = await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        source = Task(
            user_id=owner["id"], project_id=project["id"], kind="wizard.outline", status="failed",
            input={"request": {"project_id": project["id"], "volume_count": 1}, "ai_history_epoch": 0,
                   "_wizard_actor_token_version": (await db.get(User, owner["id"])).token_version,
                   "_task_actor_token_version": (await db.get(User, owner["id"])).token_version},
            output_text='{"volumes":[{"title":"旧结果"}',
        )
        db.add(source)
        await db.commit()
        return owner, project, {"id": source.id, "task_key": source.statistics_key}


async def _running(app, task_id):
    """直接调用 Job 的测试也必须使用真实已领取 Task 和原始执行身份。"""
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        key = str(uuid4())
        identity = from_queued_task(task, key)
        task.status, task.execution_key = "running", key
        await db.commit()
        return task, identity


async def test_old_key_cannot_continue_recreated_task(app, client, fake_llm):
    owner, project, old = await _source(app, client)
    async with app.state.sessionmaker() as db:
        source = await db.get(Task, old["id"])
        await db.delete(source)
        await db.flush()
        replacement = Task(
            id=old["id"], user_id=owner["id"], project_id=project["id"], kind="wizard.outline", status="failed",
            input={"request": {"project_id": project["id"], "volume_count": 9}, "ai_history_epoch": 0},
            output_text='{"volumes":[{"title":"替代结果"}',
        )
        db.add(replacement)
        await db.commit()
        assert replacement.statistics_key != old["task_key"]
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": old["id"], "continue_from_task_key": old["task_key"],
    })
    assert response.status_code == 400, response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 1
    assert not fake_llm.calls


async def test_missing_continue_key_is_not_silently_resolved_from_id(app, client, fake_llm):
    _, project, source = await _source(app, client)
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"],
    })
    assert response.status_code == 422, response.text
    assert not fake_llm.calls


async def test_continue_key_without_task_id_is_rejected(app, client):
    _, project, source = await _source(app, client)
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_key": source["task_key"],
    })
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "key", [str(uuid4()), "BAD", ""], ids=["different-uuid", "invalid-format", "empty"],
)
async def test_wrong_or_invalid_key_cannot_continue_source(app, client, fake_llm, key):
    _, project, source = await _source(app, client)
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": key,
    })
    assert response.status_code == (400 if len(key) == 36 else 422), response.text
    assert not fake_llm.calls


async def test_valid_source_key_is_fixed_in_new_task(app, client):
    _, project, source = await _source(app, client)
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
    })
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["task_key"] and accepted["task_key"] != source["task_key"]
    async with app.state.sessionmaker() as db:
        stored = await db.get(Task, accepted["id"])
        assert stored.input["request"]["continue_from_task_id"] == source["id"]
        assert stored.input["request"]["continue_from_task_key"] == source["task_key"]
        assert (await db.get(Project, project["id"])).user_id == stored.user_id


async def test_new_job_wont_read_replacement_source_after_accepted(app, client, fake_llm):
    owner, project, source = await _source(app, client)
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
    })
    assert response.status_code == 202, response.text
    async with app.state.sessionmaker() as db:
        old = await db.get(Task, source["id"])
        await db.delete(old)
        await db.flush()
        db.add(Task(
            id=source["id"], user_id=owner["id"], project_id=project["id"], kind="wizard.outline", status="failed",
            input={"request": {"project_id": project["id"]}, "ai_history_epoch": 0},
            output_text='{"volumes":[{"title":"替代内容"}',
        ))
        await db.commit()
    task, execution = await _running(app, response.json()["id"])
    with pytest.raises(WizardResumeChanged) as exc:
        await WizardHandler().build_job(app.state.task_engine.ctx, task, execution=execution)
    assert exc.value.extra["code"] == "wizard_resume_changed"
    assert not fake_llm.calls


@pytest.mark.parametrize("changed", ["replaced", "updated"])
async def test_changed_source_after_build_prevents_prefix_and_model_send(app, client, fake_llm, changed):
    owner, project, source = await _source(app, client)
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
    })
    assert response.status_code == 202, response.text
    task, execution = await _running(app, response.json()["id"])
    job = await WizardHandler().build_job(app.state.task_engine.ctx, task, execution=execution)
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, source["id"])
        if changed == "updated":
            original.output_text = '{"volumes":[{"title":"同一任务的改动"}'
        else:
            await db.delete(original)
            await db.flush()
            db.add(Task(
                id=source["id"], user_id=owner["id"], project_id=project["id"],
                kind="wizard.outline", status="failed",
                input={"request": {"project_id": project["id"]}, "ai_history_epoch": 0},
                output_text='{"volumes":[{"title":"复用任务的新数据"}',
            ))
        await db.commit()
    events = [event async for event in run_wizard_job(job, app.state.sessionmaker)]
    assert events == [("error", {"detail": "原向导任务已变化，请重新选择"})]
    assert not fake_llm.calls


async def test_changed_source_after_prefix_preflight_is_stopped_before_model_send(app, client, fake_llm):
    _, project, source = await _source(app, client)
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
    })
    task, execution = await _running(app, response.json()["id"])
    job = await WizardHandler().build_job(app.state.task_engine.ctx, task, execution=execution)
    stream = run_wizard_job(job, app.state.sessionmaker)
    try:
        assert (await anext(stream))[0] == "stage"
        assert (await anext(stream))[0] == "delta"
        async with app.state.sessionmaker() as db:
            (await db.get(Task, source["id"])).output_text = '{"volumes":[{"title":"被替换"}'
            await db.commit()
        events = [event async for event in stream]
        assert events == [("error", {"detail": "原向导任务已变化，请重新选择"})]
        assert not fake_llm.calls
    finally:
        await stream.aclose()


@pytest.mark.parametrize("change", ["source", "disabled_actor"])
async def test_changed_source_after_send_does_not_save_old_result(app, client, fake_llm, change):
    user, project, source = await _source(app, client)
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
    })
    task, execution = await _running(app, response.json()["id"])
    job = await WizardHandler().build_job(app.state.task_engine.ctx, task, execution=execution)
    gate = Gate()
    fake_llm.push([gate, ']}]}'])

    async def collect():
        return [event async for event in run_wizard_job(job, app.state.sessionmaker)]

    work = asyncio.create_task(collect())
    try:
        await gate.wait_reached()
        async with app.state.sessionmaker() as db:
            if change == "source":
                (await db.get(Task, source["id"])).output_text = '{"volumes":[{"title":"后来的数据"}'
            else:
                (await db.get(User, user["id"])).is_active = False
            await db.commit()
        gate.release()
        events = await work
        assert events[-1][0] == "error"
        if change == "source":
            assert "原向导任务已变化" in events[-1][1]["detail"]
            assert not (await client.get(f"/api/v1/projects/{project['id']}/outlines")).json()
        else:
            assert "未登录或登录已过期" in events[-1][1]["detail"]
            async with app.state.sessionmaker() as db:
                assert not list(await db.scalars(select(Outline).where(Outline.project_id == project["id"])))
        assert len(fake_llm.calls) == 1
    finally:
        gate.release()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def test_wrong_source_key_does_not_flush_pending_data(app, client):
    owner, project, source = await _source(app, client)
    from app.services import project_ai_config

    # E4-g3c：受理改为受控受理，中央接受的模型绑定在独立会话中取得；受理锁内校验仍不得 flush 调用方暂存。
    async with app.state.sessionmaker() as setup:
        binding = await project_ai_config.resolve_project_model(
            setup, owner["id"], project["id"], "text", cipher=app.state.cipher,
            allow_private=app.state.settings.allow_private_ai_endpoints,
        )
    async with app.state.sessionmaker() as db:
        from app.services.authorization_service import AuthenticatedActor

        db.info["authenticated_actor"] = AuthenticatedActor(
            owner["id"], (await db.get(User, owner["id"])).token_version,
        )
        pending = StoryMemory(project_id=project["id"], chapter_no=1, kind="manual", content="未提交草稿")
        db.add(pending)
        with pytest.raises(BadRequestError):
            await WizardHandler()._claim(
                db, app.state.task_engine.ctx, user_id=owner["id"], kind="wizard.outline", params={
                    "project_id": project["id"], "continue_from_task_id": source["id"],
                    "continue_from_task_key": str(uuid4()),
                }, accepted_model=binding,
            )
        assert pending in db.new and pending.id is None


async def _task_input(app, task_id):
    async with app.state.sessionmaker() as db:
        return (await db.get(Task, task_id)).input


async def test_changed_source_before_kb_embedding_never_sends(app, client, fake_llm, monkeypatch):
    _, project, source = await _source(app, client)
    await create_ai_config(client, name="向量模型", model_type="embedding", model="embedding-test")
    async with app.state.sessionmaker() as db:
        db.add(KbDocument(project_id=project["id"], filename="资料.txt", sha256="1" * 64, text="资料内容"))
        await db.commit()
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"],
    })
    assert response.status_code == 202, response.text
    # E6-f（裁决 13）：作者续写也在受理时冻结共享向量选择，查询向量改由 for_task 守卫（带续写来源）构造；
    # 原 hook 点 retrieval_service.get_embedder 已不在引擎执行路径上，改为在向量 provider 构造完成后变更来源。
    assert (await _task_input(app, response.json()["id"]))["_shared_embedding"]["mode"] == "vector"
    original = SendGuard.prepare_provider

    async def change_after_resolve(guard, *args, **kwargs):
        bound = await original(guard, *args, **kwargs)
        if type(guard._binding) is project_ai_config.ResolvedSharedEmbedding:
            async with app.state.sessionmaker() as writer:
                (await writer.get(Task, source["id"])).output_text = '{"volumes":[{"title":"已变更"}'
                await writer.commit()
        return bound

    monkeypatch.setattr(SendGuard, "prepare_provider", change_after_resolve)
    task, execution = await _running(app, response.json()["id"])
    with pytest.raises(VisibilityPolicyError):
        await WizardHandler().build_job(app.state.task_engine.ctx, task, execution=execution)
    assert not fake_llm.embed_calls and not fake_llm.calls
