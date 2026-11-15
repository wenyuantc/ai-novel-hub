"""E4：协作任务在领取前/运行中按数据库最新授权失败关闭，并按原 UUID/轮次受限回退占用。

测试直接向库插入带 `_collaboration_grant` 的任务模拟受理结果。E4-f 起章节 handler 已声明
collaboration_execution_ready；验证「handler 未就绪」引擎语义的用例在本测试内临时撤销该声明，
运行中用例仍只临时给 outline.continue 打补丁。
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models import Chapter, KbDocument, ProjectCollaborator, Task, User
from app.services import chapter_claim, kb_index_service, project_ai_config
from app.services.task_engine import engine as engine_module
from app.services.task_engine.cancellation import _CHAPTER_INTENT, chapter_cancel_intent
from app.services.task_engine.execution_identity import from_queued_task
from app.services.task_engine.handlers.chapter import bind_queued_claim
from app.services.task_engine.registry import get_handler
from tests.helpers import create_ai_config, create_project, register, stream_task, wait_until

MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES


async def _world(owner, guest, *, allow_owner_ai=False, chapters=2):
    await register(owner, "e4-owner")
    guest_user = await register(guest, "e4-guest")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    rows = []
    for index in range(chapters):
        created = await owner.post(f"{base}/chapters", json={"title": f"第{index + 1}章", "content": "原正文"})
        assert created.status_code == 201, created.text
        rows.append(created.json())
    await _invite(owner, guest, project["id"], allow_owner_ai=allow_owner_ai)
    return project, rows, guest_user


async def _invite(owner, guest, project_id, *, allow_owner_ai=False):
    invite = await owner.post(f"/api/v1/projects/{project_id}/collaborators", json={
        "username": "e4-guest", "role": "editor", "allow_owner_ai": allow_owner_ai,
    })
    assert invite.status_code == 201, invite.text
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text


async def _member(app, project_id, user_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id,
        ))


async def _grant_input(db, project_id, user_id, request, *, extra=None):
    member = await db.scalar(select(ProjectCollaborator).where(
        ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id,
    ))
    user = await db.get(User, user_id)
    return {
        "request": request,
        "_collaboration_grant": {"invitation_id": member.invitation_id, "revision": member.revision},
        "_task_actor_token_version": user.token_version,
        **(extra or {}),
    }


async def _queue_chapter(app, project_id, chapter, actor_id, *, extra=None):
    async with app.state.sessionmaker() as db:
        payload = await _grant_input(db, project_id, actor_id, {"chapter_id": chapter["id"]}, extra=extra)
        task = Task(
            user_id=actor_id, project_id=project_id, chapter_id=chapter["id"], kind="chapter.generate",
            title="协作者排队任务", resource_key=f"chapter:{chapter['id']}", status="queued",
            input={"chapter_id": chapter["id"], "prev_status": chapter["status"], **payload},
        )
        db.add(task)
        await db.flush()
        claim = await bind_queued_claim(db, task)
        await db.commit()
        return task.id, task.statistics_key, claim


async def _change(app, owner, project_id, actor, change):
    member = await _member(app, project_id, actor["id"])
    base = f"/api/v1/projects/{project_id}/collaborators/{actor['id']}"
    if change == "revoked":
        response = await owner.delete(base, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
    elif change == "viewer":
        response = await owner.patch(base, json={"expected_revision": member.revision, "role": "viewer"})
        assert response.status_code == 200, response.text
    elif change == "owner_ai_patch":
        response = await owner.patch(base, json={"expected_revision": member.revision, "allow_owner_ai": False})
        assert response.status_code == 200, response.text
    elif change == "reinvite":
        response = await owner.delete(base, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text  # 新一轮邀请由调用方发起并接受
    elif change == "disabled":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == actor["id"]).values(is_active=False))
            await db.commit()
    elif change == "token":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == actor["id"]).values(token_version=User.token_version + 1))
            await db.commit()
    elif change == "owner_ai_direct":
        # 模拟未来不递增 revision 的授权收紧：必须由直接检查拒绝，不能依赖 grant 失配。
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.id == member.id)
                             .values(allow_owner_ai=False))
            await db.commit()
    else:
        raise AssertionError(change)


async def _wait_status(app, task_id, statuses, within=10.0):
    async with asyncio.timeout(within):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
                if row.status in statuses:
                    return row
            await asyncio.sleep(0.02)


async def _owner_binding(app, project_id, actor_id):
    async with app.state.sessionmaker() as db:
        binding = await project_ai_config.resolve_project_model(
            db, actor_id, project_id, "text", cipher=app.state.cipher, allow_private=True, for_update=True,
        )
        await db.rollback()
    assert binding.source == "owner"
    return project_ai_config.serialize_project_model(binding)


@pytest.fixture
def chapter_unready(monkeypatch):
    """仅本测试临时撤销章节 handler 的协作就绪声明，以覆盖引擎「handler 未就绪」分支。"""
    handler = get_handler("chapter.generate")
    assert handler.collaboration_execution_ready is True
    monkeypatch.setattr(handler, "collaboration_execution_ready", False, raising=False)


EXPECTED = {
    "revoked": "collaboration_revoked", "viewer": "collaboration_revoked", "reinvite": "collaboration_revoked",
    "owner_ai_patch": "collaboration_revoked", "disabled": "collaboration_account_unavailable",
    "token": "collaboration_account_unavailable", "owner_ai_direct": "collaboration_model_revoked",
}


@pytest.mark.parametrize("change", list(EXPECTED))
async def test_queued_grant_task_fails_closed_and_releases_chapter_claim(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    owner_ai = change in ("owner_ai_patch", "owner_ai_direct")
    project, chapters, actor = await _world(client, guest, allow_owner_ai=owner_ai)
    extra = None
    if owner_ai:
        await create_ai_config(client)
        extra = {"_project_model": await _owner_binding(app, project["id"], actor["id"])}
    task_id, task_key, claim = await _queue_chapter(app, project["id"], chapters[0], actor["id"], extra=extra)
    await _change(app, client, project["id"], actor, change)
    if change == "reinvite":
        await _invite(client, guest, project["id"])
    await app.state.task_engine.start()
    row = await _wait_status(app, task_id, ("failed", "succeeded", "cancelled", "interrupted"))
    assert row.status == "failed" and row.error == MESSAGES[EXPECTED[change]]
    assert row.statistics_key == task_key and row.execution_key is None and row.result is None
    assert "_chapter_claim" not in row.input and row.output_text == ""
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapters[0]["id"])
        assert (chapter.status, chapter.content) == (claim.prev_status, "原正文")
        assert (chapter.generation_claim_key, chapter.generation_claim_task_key) == (None, None)
        active = await db.scalar(select(Task.id).where(
            Task.resource_key == f"chapter:{chapters[0]['id']}", Task.status.in_(("queued", "running")),
        ))
        assert active is None
    assert fake_llm.calls == []


async def test_valid_grant_still_waits_and_author_task_unaffected(
    app, client, make_client, fake_llm, chapter_unready,
):
    """handler 未就绪时有效协作任务保持原关闭语义（不运行、不失败、不动占用）；作者任务照常执行。"""
    await app.state.task_engine.stop()
    guest = make_client()
    project, chapters, actor = await _world(client, guest)
    await create_ai_config(client)
    task_id, _, claim = await _queue_chapter(app, project["id"], chapters[0], actor["id"])
    await app.state.task_engine.start()
    fake_llm.push("作者生成的新正文" * 5)
    created = await client.post(f"/api/v1/chapters/{chapters[1]['id']}/generate", json={})
    assert created.status_code == 202, created.text
    events = await stream_task(client, created.json())
    assert events[-1][1]["task"]["status"] == "succeeded", events
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        chapter = await db.get(Chapter, chapters[0]["id"])
        assert row.status == "queued" and row.execution_key is None
        assert chapter.status == "generating" and chapter.generation_claim_key == claim.claim_key
    await app.state.task_engine.stop()


async def test_reinvite_fails_only_old_round_and_keeps_new_round_claim(app, client, make_client, chapter_unready):
    await app.state.task_engine.stop()
    guest = make_client()
    project, chapters, actor = await _world(client, guest)
    old_id, _, old_claim = await _queue_chapter(app, project["id"], chapters[0], actor["id"])
    await _change(app, client, project["id"], actor, "reinvite")
    await _invite(client, guest, project["id"])
    new_id, _, new_claim = await _queue_chapter(app, project["id"], chapters[1], actor["id"])
    assert new_claim.grant_identity != old_claim.grant_identity
    await app.state.task_engine.start()
    old = await _wait_status(app, old_id, ("failed",))
    await app.state.task_engine.stop()
    assert old.error == MESSAGES["collaboration_revoked"]
    async with app.state.sessionmaker() as db:
        new = await db.get(Task, new_id)
        first = await db.get(Chapter, chapters[0]["id"])
        second = await db.get(Chapter, chapters[1]["id"])
        assert new.status == "queued" and new.input["_chapter_claim"]["claim_key"] == new_claim.claim_key
        assert (second.status, second.generation_claim_key) == ("generating", new_claim.claim_key)
        assert (first.status, first.generation_claim_key) == (old_claim.prev_status, None)
    # 旧轮次迟到的复位收尾不得影响任何当前占用。
    await app.state.task_engine._fail_reset_queued_chapter(old_claim)
    async with app.state.sessionmaker() as db:
        second = await db.get(Chapter, chapters[1]["id"])
        assert second.generation_claim_key == new_claim.claim_key
        assert (await db.get(Task, old_id)).error == MESSAGES["collaboration_revoked"]


async def test_revoked_document_task_releases_only_its_document_claim(app, client, make_client):
    await app.state.task_engine.stop()
    guest = make_client()
    project, _, actor = await _world(client, guest, chapters=0)
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="资料", sha256="b" * 64, text="原文")
        db.add(doc)
        await db.flush()
        payload = await _grant_input(db, project["id"], actor["id"], {
            "project_id": project["id"], "document_id": doc.id, "mode": "document",
        })
        task = Task(user_id=actor["id"], project_id=project["id"], kind="kb.index", status="queued",
                    title="协作者索引", resource_key=f"kbdoc:{doc.id}", input=payload)
        db.add(task)
        await db.flush()
        claim = await kb_index_service.bind_queued_document_claim(
            db, task.id, task.statistics_key, queue_claim_key=str(uuid4()),
        )
        task_id, doc_id = task.id, doc.id
        await db.commit()
    assert claim.actor_id == actor["id"]
    await _change(app, client, project["id"], actor, "revoked")
    await app.state.task_engine.start()
    row = await _wait_status(app, task_id, ("failed",))
    assert row.error == MESSAGES["collaboration_revoked"]
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, doc_id)
        assert (doc.status, doc.text) == ("failed", "原文")
        assert (doc.index_task_key, doc.index_claim_key) == (None, None)


async def test_admin_drain_keeps_ownership_of_revoked_grant_task(app, client, make_client):
    """管理员取消意图在先：调度不领取，由 drain 按原意图收尾为 cancelled，而非协作失效路径。"""
    await app.state.task_engine.stop()
    guest = make_client()
    project, chapters, actor = await _world(client, guest)
    task_id, task_key, claim = await _queue_chapter(app, project["id"], chapters[0], actor["id"])
    operation = str(uuid4())
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        intent = chapter_cancel_intent(task, operation)
        assert intent is not None
        await db.execute(update(Task).where(Task.id == task_id).values(
            input={**task.input, _CHAPTER_INTENT: intent}, admin_cancel_operation_id=operation,
            admin_cancel_requested_at=datetime.now(UTC),
        ))
        await db.commit()
    await _change(app, client, project["id"], actor, "revoked")
    await app.state.task_engine.start()
    row = await _wait_status(app, task_id, ("cancelled", "failed"))
    async with asyncio.timeout(10):
        while row.admin_cancel_operation_id is not None:
            await asyncio.sleep(0.02)
            row = await _wait_status(app, task_id, ("cancelled", "failed"))
    assert row.status == "cancelled" and row.error == "任务已取消" and row.statistics_key == task_key
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapters[0]["id"])
        assert (chapter.status, chapter.generation_claim_key) == (claim.prev_status, None)


@pytest.mark.parametrize("disabled", [False, True])
async def test_reset_queued_collaborator_chapter_fails_without_touching_chapter(app, client, make_client, disabled):
    """E4-b：章被人工复位后，协作者排队任务也必须结束；账号停用不能阻止系统收尾。"""
    await app.state.task_engine.stop()
    guest = make_client()
    project, chapters, actor = await _world(client, guest)
    task_id, _, claim = await _queue_chapter(app, project["id"], chapters[0], actor["id"])
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == chapters[0]["id"]).values(
            status="done", generation_claim_key=None, generation_claim_task_key=None,
            generation_claim_prev_status=None,
        ))
        if disabled:
            await db.execute(update(User).where(User.id == actor["id"]).values(is_active=False))
        await db.commit()
    await app.state.task_engine._fail_reset_queued_chapter(claim)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        chapter = await db.get(Chapter, chapters[0]["id"])
        assert row.status == "failed" and row.error == "章节生成状态已被复位，任务未执行"
        assert "_chapter_claim" not in row.input
        assert (chapter.status, chapter.generation_claim_key, chapter.content) == ("done", None, "原正文")


@pytest.mark.parametrize("token", [True, False])
async def test_run_with_unready_handler_finalizes_proven_round(app, client, make_client, token, chapter_unready):
    """E4-c：能证明是本轮执行时不再留下孤立 running，按原 claim 回退章节。"""
    await app.state.task_engine.stop()
    guest = make_client()
    project, chapters, actor = await _world(client, guest)
    task_id, task_key, queued_claim = await _queue_chapter(app, project["id"], chapters[0], actor["id"])
    execution_key = str(uuid4())
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        if not token:
            payload = dict(task.input)
            payload.pop("_task_actor_token_version")
            await db.execute(update(Task).where(Task.id == task_id).values(input=payload))
            await db.commit()
            await db.refresh(task)
        execution = from_queued_task(task, execution_key)
        await db.execute(update(Task).where(Task.id == task_id).values(status="running", execution_key=execution_key))
        running = await chapter_claim.activate_queued_chapter(db, queued_claim, execution_key=execution_key)
        await db.commit()
        queued_at = (await db.get(Task, task_id)).queued_at
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task_id, statistics_key=task_key, status="running", queued_at=queued_at,
                               execution_key=execution_key)
    await engine._run(task_id, hub, execution, claim=running)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        chapter = await db.get(Chapter, chapters[0]["id"])
        assert row.status == "failed" and row.error == "任务执行身份尚未确认" and row.execution_key is None
        assert (chapter.status, chapter.generation_claim_key) == (queued_claim.prev_status, None)
    assert project["id"] not in engine._abort_failures


@pytest.fixture
def blocking_outline(monkeypatch):
    """仅本测试临时让 outline.continue 接受协作执行，并以可控阻塞的 runner 代替模型调用。"""
    handler = get_handler("outline.continue")
    state = SimpleNamespace(reached=asyncio.Event(), release=asyncio.Event(), cancelled=0)

    async def build_job(ctx, task, *, execution=None):
        return SimpleNamespace()

    async def run(job, ctx):
        state.reached.set()
        try:
            await state.release.wait()
        except asyncio.CancelledError:
            state.cancelled += 1
            raise
        yield "delta", {"text": "旧授权下的输出"}
        yield "done", {"kind": "outline.continue", "items": []}

    async def verify(db, task, **kwargs):
        return None

    async def on_abort(ctx, task, *, execution=None):
        return None

    monkeypatch.setattr(handler, "collaboration_execution_ready", True, raising=False)
    monkeypatch.setattr(handler, "build_job", build_job)
    monkeypatch.setattr(handler, "run", run)
    monkeypatch.setattr(handler, "verify_event", verify)
    monkeypatch.setattr(handler, "verify_finalization", verify)
    monkeypatch.setattr(handler, "stream_guard", None)
    monkeypatch.setattr(handler, "on_abort", on_abort)
    return state


async def _start_outline(app, project_id, actor_id, *, extra=None):
    async with app.state.sessionmaker() as db:
        payload = await _grant_input(db, project_id, actor_id, {"project_id": project_id}, extra=extra)
        task = Task(user_id=actor_id, project_id=project_id, kind="outline.continue", status="queued",
                    title="协作者续写", input=payload)
        db.add(task)
        await db.commit()
        task_id = task.id
    app.state.task_engine.notify()
    return task_id


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_patch"])
async def test_notify_wakes_running_executor_after_database_recheck(app, client, make_client, blocking_outline,
                                                                    change):
    """E4-d：撤权提交后的本地通知触发数据库复查，确认失效才停止执行体，并以失败终态收尾。"""
    guest = make_client()
    owner_ai = change == "owner_ai_patch"
    project, _, actor = await _world(client, guest, allow_owner_ai=owner_ai, chapters=0)
    extra = None
    if owner_ai:
        await create_ai_config(client)
        extra = {"_project_model": await _owner_binding(app, project["id"], actor["id"])}
    task_id = await _start_outline(app, project["id"], actor["id"], extra=extra)
    await asyncio.wait_for(blocking_outline.reached.wait(), 10)
    engine = app.state.task_engine
    # 授权仍有效时的通知只复查数据库，不得停止执行体。
    engine.notify_collaboration_change(project["id"], actor["id"])
    await asyncio.gather(*list(engine._collaboration_checks))
    assert task_id in engine._running and blocking_outline.cancelled == 0
    await _change(app, client, project["id"], actor, change)
    row = await _wait_status(app, task_id, ("failed", "succeeded", "cancelled"))
    assert row.status == "failed" and row.error == MESSAGES["collaboration_revoked"]
    assert row.execution_key is None and row.output_text == "" and blocking_outline.cancelled == 1
    await wait_until(lambda: task_id not in engine._collaboration_revoked)


async def test_notify_after_direct_owner_ai_close_fails_running_with_model_message(
    app, client, make_client, blocking_outline,
):
    """不递增 revision 的模型授权关闭：通知后的数据库复查须直接核对开关，以模型授权文案失败关闭。"""
    guest = make_client()
    project, _, actor = await _world(client, guest, allow_owner_ai=True, chapters=0)
    await create_ai_config(client)
    extra = {"_project_model": await _owner_binding(app, project["id"], actor["id"])}
    task_id = await _start_outline(app, project["id"], actor["id"], extra=extra)
    await asyncio.wait_for(blocking_outline.reached.wait(), 10)
    await _change(app, client, project["id"], actor, "owner_ai_direct")
    engine = app.state.task_engine
    engine.notify_collaboration_change(project["id"], actor["id"])
    row = await _wait_status(app, task_id, ("failed", "succeeded", "cancelled"))
    assert row.status == "failed" and row.error == MESSAGES["collaboration_model_revoked"]
    assert row.output_text == "" and blocking_outline.cancelled == 1


async def test_notify_never_stops_running_author_task(app, client, make_client, blocking_outline):
    """作者任务没有协作轮次：即使针对作者本人的通知也只复查，不取消执行体。"""
    guest = make_client()
    project, _, _actor = await _world(client, guest, chapters=0)
    owner_id = (await client.get("/api/v1/auth/me")).json()["id"]
    async with app.state.sessionmaker() as db:
        user = await db.get(User, owner_id)
        task = Task(user_id=owner_id, project_id=project["id"], kind="outline.continue", status="queued",
                    title="作者续写", input={"request": {"project_id": project["id"]},
                                             "_task_actor_token_version": user.token_version})
        db.add(task)
        await db.commit()
        task_id = task.id
    engine = app.state.task_engine
    engine.notify()
    await asyncio.wait_for(blocking_outline.reached.wait(), 10)
    engine.notify_collaboration_change(project["id"], owner_id)
    await asyncio.gather(*list(engine._collaboration_checks))
    assert task_id in engine._running and blocking_outline.cancelled == 0
    assert task_id not in engine._collaboration_revoked
    blocking_outline.release.set()
    row = await _wait_status(app, task_id, ("failed", "succeeded", "cancelled"))
    assert row.status == "succeeded"


async def test_running_checkpoint_rejects_owner_ai_closed_without_revision_bump(app, client, make_client,
                                                                                 blocking_outline):
    """未收到通知时，下一次受控写检查直接核对作者模型授权，不依赖 revision 递增。"""
    guest = make_client()
    project, _, actor = await _world(client, guest, allow_owner_ai=True, chapters=0)
    await create_ai_config(client)
    extra = {"_project_model": await _owner_binding(app, project["id"], actor["id"])}
    task_id = await _start_outline(app, project["id"], actor["id"], extra=extra)
    await asyncio.wait_for(blocking_outline.reached.wait(), 10)
    await _change(app, client, project["id"], actor, "owner_ai_direct")
    blocking_outline.release.set()
    row = await _wait_status(app, task_id, ("failed", "succeeded", "cancelled"))
    assert row.status == "failed" and row.output_text == "" and row.result is None
    # 拒绝发生在输出发布/最终保存的写身份核验：只允许固定安全文案，不透传异常正文。
    # E4-f 起事件核验失败带受控 code，落为「任务输出授权已变化」而非通用服务器错误。
    assert row.error in ("任务执行身份已变化，请核对后重试", "任务输出授权已变化，已停止")
    assert blocking_outline.cancelled == 0


async def test_running_checkpoint_positive_control_succeeds(app, client, make_client, blocking_outline):
    guest = make_client()
    project, _, actor = await _world(client, guest, allow_owner_ai=True, chapters=0)
    await create_ai_config(client)
    extra = {"_project_model": await _owner_binding(app, project["id"], actor["id"])}
    task_id = await _start_outline(app, project["id"], actor["id"], extra=extra)
    await asyncio.wait_for(blocking_outline.reached.wait(), 10)
    blocking_outline.release.set()
    row = await _wait_status(app, task_id, ("failed", "succeeded", "cancelled"))
    assert row.status == "succeeded" and row.output_text == "旧授权下的输出"
