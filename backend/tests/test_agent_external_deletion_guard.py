"""作品与用户级联不能抹去仍可能执行中的外部提案证据。"""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import AdminAuditLog, AgentChange, AgentConversation, Project, Task, User
from app.services import project_lifecycle, recycle_bin_service, task_service
from tests.helpers import create_project, register, wait_task
from tests.mcp_fakes import FakeMcpConnector
from tests.test_agent_mcp_skills import call, setup


async def _users_and_project(app, owner, guest):
    await app.state.task_engine.stop()
    owner_user = await register(owner, "external-owner")
    guest_user = await register(guest, "external-guest")
    project = await create_project(owner, title="外部调用私有作品")
    return owner_user, guest_user, project


async def _seed_change(db, *, project_id, conversation_user_id, conversation_project_id=None,
                       task_user_id=None, task_project_id=None, chapter_id=None, binding="id",
                       kind="external", status="executing", result=None):
    conversation = AgentConversation(user_id=conversation_user_id, project_id=conversation_project_id or project_id,
                                     title="私有会话")
    db.add(conversation)
    task = None
    if task_user_id is not None:
        task = Task(user_id=task_user_id, project_id=task_project_id, chapter_id=chapter_id,
                    kind="agent.turn", status="cancelled", title="私有任务")
        db.add(task)
    await db.flush()
    payload = result if result is not None else {"_external_claim_id": str(uuid4())}
    if binding == "key" and task is not None:
        payload = {**payload, "_external_task_key": task.statistics_key}
    change = AgentChange(project_id=project_id, conversation_id=conversation.id,
                         turn_task_id=task.id if binding == "id" and task is not None else None,
                         turn_task_key=task.statistics_key if binding == "turn_key" and task is not None else None,
                         tool_call_id=f"call-{uuid4()}", tool_name="private-tool", arguments={"secret": "private"},
                         kind=kind, status=status, result=payload, executed_at=datetime.now(UTC))
    db.add(change)
    await db.flush()
    return conversation.id, task.id if task is not None else None, change.id


@pytest.mark.parametrize("binding", ["conversation", "id", "key", "turn_key"])
async def test_delete_user_rejects_external_execution_even_when_task_is_terminal_or_missing(
    app, client, make_client, system_admin, binding,
):
    guest = make_client()
    owner, user, project = await _users_and_project(app, client, guest)
    async with app.state.sessionmaker() as db:
        conversation_id, task_id, change_id = await _seed_change(
            db, project_id=project["id"], conversation_user_id=user["id"] if binding == "conversation" else owner["id"],
            task_user_id=None if binding == "conversation" else user["id"], task_project_id=project["id"],
            binding=binding,
        )
        await db.commit()
    response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
    assert response.status_code == 409 and response.json()["code"] == "user_has_pending_tasks", response.text
    assert "外部调用私有作品" not in response.text and "私有" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None
        assert await db.get(AgentConversation, conversation_id) is not None
        assert (await db.get(AgentChange, change_id)).status == "executing"
        if task_id is not None:
            assert (await db.get(Task, task_id)).status == "cancelled"
        audit = await db.scalar(select(AdminAuditLog).where(
            AdminAuditLog.action == "user.delete", AdminAuditLog.target_id == user["id"],
        ))
        assert audit.result == "failure" and audit.project_id is None and audit.error_code == "conflict"


async def test_delete_user_preserves_terminal_unknown_external_outcome_without_claim_key(
    app, client, make_client, system_admin,
):
    guest = make_client()
    owner, user, project = await _users_and_project(app, client, guest)
    async with app.state.sessionmaker() as db:
        _, task_id, change_id = await _seed_change(
            db, project_id=project["id"], conversation_user_id=owner["id"],
            task_user_id=user["id"], task_project_id=project["id"], binding="turn_key", status="failed",
            result={"error": "结果未知", "_external_outcome_unknown": True},
        )
        await db.commit()
    response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
    assert response.status_code == 409 and response.json()["code"] == "user_has_pending_tasks", response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None and await db.get(Task, task_id) is not None
        assert (await db.get(AgentChange, change_id)).result["_external_outcome_unknown"] is True


async def test_soft_delete_checks_external_claim_before_freezing_or_cancelling_other_tasks(app, client, make_client):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    async with app.state.sessionmaker() as db:
        _, task_id, change_id = await _seed_change(
            db, project_id=project["id"], conversation_user_id=user["id"],
            task_user_id=user["id"], task_project_id=project["id"],
        )
        other = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="queued")
        db.add(other)
        await db.commit()
        other_id = other.id
    response = await client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    async with app.state.sessionmaker() as db:
        saved = await db.get(Project, project["id"])
        assert saved.deleted_at is None and saved.deletion_started_at is None and saved.deletion_token is None
        assert (await db.get(Task, task_id)).status == "cancelled"
        assert (await db.get(Task, other_id)).status == "queued"
        assert (await db.get(AgentChange, change_id)).status == "executing"


@pytest.mark.parametrize("ending", ["unchanged", "settled", "cancelled", "timeout"])
async def test_local_work_only_triggers_wait_and_never_proves_an_external_claim_stopped(
    app, client, make_client, ending,
):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    engine = app.state.task_engine
    if ending == "timeout":
        engine.cancel_wait_seconds = 0.2
    async with app.state.sessionmaker() as db:
        _, task_id, change_id = await _seed_change(
            db, project_id=project["id"], conversation_user_id=user["id"],
            task_user_id=user["id"], task_project_id=project["id"],
        )
        unrelated = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="queued")
        db.add(unrelated)
        await db.commit()
        unrelated_id = unrelated.id
    entered, release = asyncio.Event(), asyncio.Event()

    async def do_unrelated_work():
        with engine.cleanup_scope(unrelated):
            entered.set()
            await release.wait()

    work = asyncio.create_task(do_unrelated_work())
    await asyncio.wait_for(entered.wait(), 5)
    deletion = asyncio.create_task(client.delete(f"/api/v1/projects/{project['id']}"))
    try:
        async with asyncio.timeout(5):
            while True:
                assert not deletion.done(), "未决外部操作应先等待本地工作或直接失败关闭"
                async with app.state.sessionmaker() as db:
                    row = await db.get(Project, project["id"])
                    if row.deletion_started_at is not None:
                        assert (await db.get(Task, unrelated_id)).status == "queued"
                        assert (await db.get(Task, task_id)).status == "cancelled"
                        break
                await asyncio.sleep(0.005)
        if ending == "settled":
            async with app.state.sessionmaker() as db:
                claim = await db.get(AgentChange, change_id)
                claim.status, claim.result = "executed", {"text": "完成"}
                await db.commit()
        if ending == "cancelled":
            deletion.cancel()
            with pytest.raises(asyncio.CancelledError):
                await deletion
        else:
            if ending != "timeout":
                release.set()
            response = await asyncio.wait_for(deletion, 5)
            if ending == "settled":
                assert response.status_code == 204, response.text
            else:
                assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped"
        async with app.state.sessionmaker() as db:
            saved = await db.get(Project, project["id"])
            assert saved.deletion_started_at is None and saved.deletion_token is None
            if ending == "settled":
                assert saved.deleted_at is not None
                assert (await db.get(Task, unrelated_id)).status == "cancelled"
            else:
                assert saved.deleted_at is None
                assert (await db.get(Task, unrelated_id)).status == "queued"
                assert (await db.get(AgentChange, change_id)).status == "executing"
    finally:
        release.set()
        await work
        if not deletion.done():
            deletion.cancel()
            with suppress(asyncio.CancelledError):
                await deletion


async def test_external_wait_rechecks_before_waiting_for_other_cancellable_work(app, client, make_client, monkeypatch):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    engine = app.state.task_engine
    async with app.state.sessionmaker() as db:
        _, task_id, change_id = await _seed_change(
            db, project_id=project["id"], conversation_user_id=user["id"],
            task_user_id=user["id"], task_project_id=project["id"],
        )
        original_task = await db.get(Task, task_id)
        unrelated = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="queued")
        db.add(unrelated)
        await db.commit()
        unrelated_id = unrelated.id
    entered_external, entered_other, release_external, release_other = (asyncio.Event() for _ in range(4))
    entered_stop, release_stop = asyncio.Event(), asyncio.Event()
    original_stop = task_service.stop_project_tasks

    async def external_work():
        with engine.cleanup_scope(original_task):
            entered_external.set()
            await release_external.wait()
            async with app.state.sessionmaker() as db:
                await project_lifecycle.lock_record(db, project["id"])
                claim = await db.get(AgentChange, change_id)
                claim.status, claim.result = "failed", {"error": "已收尾"}
                await db.commit()

    async def other_work():
        with engine.cleanup_scope(unrelated):
            entered_other.set()
            await release_other.wait()

    async def stop(db, engine, project_id):
        entered_stop.set()
        await release_stop.wait()
        return await original_stop(db, engine, project_id)

    monkeypatch.setattr(task_service, "stop_project_tasks", stop)
    jobs = [asyncio.create_task(external_work()), asyncio.create_task(other_work())]
    deletion = None
    try:
        await asyncio.gather(entered_external.wait(), entered_other.wait())
        deletion = asyncio.create_task(client.delete(f"/api/v1/projects/{project['id']}"))
        async with asyncio.timeout(5):
            while True:
                async with app.state.sessionmaker() as db:
                    if (await db.get(Project, project["id"])).deletion_started_at is not None:
                        break
                await asyncio.sleep(0.005)
        release_external.set()
        await asyncio.wait_for(entered_stop.wait(), 5)
        assert not jobs[1].done() and not deletion.done()
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, unrelated_id)).status == "queued"
            assert (await db.get(AgentChange, change_id)).status == "failed"
        release_other.set()
        release_stop.set()
        assert (await asyncio.wait_for(deletion, 5)).status_code == 204
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, unrelated_id)).status == "cancelled"
            assert (await db.get(Project, project["id"])).deleted_at is not None
    finally:
        release_external.set()
        release_other.set()
        release_stop.set()
        await asyncio.gather(*jobs)
        if deletion is not None and not deletion.done():
            deletion.cancel()
            with suppress(asyncio.CancelledError):
                await deletion


async def test_soft_delete_waits_for_real_external_confirm_before_cancelling_its_task(app, client, fake_llm):
    entered, release = asyncio.Event(), asyncio.Event()

    async def remote(_name, _arguments):
        entered.set()
        await release.wait()
        return "远端已经执行"

    connector = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = connector
    project, base, url, _ = await setup(client, connector, handler=remote)
    fake_llm.push_tools([call("mcp__dict__send", {"to": "编辑部"})])
    response = await client.post(f"{url}/messages", json={"content": "请帮我查一下"})
    assert response.status_code == 202, response.text
    task = response.json()["task"]
    task_id = task["id"]
    state = await wait_task(client, task, statuses=("paused", "failed"))
    assert state["status"] == "paused", state
    change = (await client.get(url)).json()["changes"][0]
    change_id = change["id"]
    confirm = asyncio.create_task(client.post(f"{base}/changes/{change_id}/confirm", json={
        "change_key": change["change_key"], "view_version": change["view_version"],
    }))
    deletion = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        deletion = asyncio.create_task(client.delete(f"/api/v1/projects/{project['id']}"))
        async with asyncio.timeout(5):
            while True:
                assert not deletion.done()
                async with app.state.sessionmaker() as db:
                    assert (await db.get(Task, task_id)).status == "paused"
                    assert (await db.get(AgentChange, change_id)).status == "executing"
                    saved = await db.get(Project, project["id"])
                    if saved.deletion_started_at is not None:
                        assert saved.deleted_at is None
                        break
                await asyncio.sleep(0.005)
        release.set()
        assert (await asyncio.wait_for(confirm, 10)).status_code == 409
        assert (await asyncio.wait_for(deletion, 10)).status_code == 204
        assert len(connector.calls) == 1
        async with app.state.sessionmaker() as db:
            change = await db.get(AgentChange, change_id)
            assert change.status == "failed" and "远端已经执行" not in str(change.result)
            assert (await db.get(Task, task_id)).status == "cancelled"
    finally:
        release.set()
        if not confirm.done():
            confirm.cancel()
            with suppress(asyncio.CancelledError):
                await confirm
        if deletion is not None and not deletion.done():
            deletion.cancel()
            with suppress(asyncio.CancelledError):
                await deletion


@pytest.mark.parametrize("via", ["conversation", "task", "task_key", "turn_key", "chapter"])
async def test_soft_delete_detects_cross_project_corrupt_references_without_touching_either_book(
    app, client, make_client, via,
):
    guest = make_client()
    owner, user, project = await _users_and_project(app, client, guest)
    second = await create_project(client, title="另一本作品")
    chapter_response = await client.post(f"/api/v1/projects/{second['id']}/chapters", json={"title": "章节"})
    assert chapter_response.status_code == 201
    async with app.state.sessionmaker() as db:
        conversation_id, task_id, change_id = await _seed_change(
            db, project_id=project["id"],
            conversation_user_id=user["id"],
            conversation_project_id=second["id"] if via == "conversation" else project["id"],
            task_user_id=owner["id"],
            task_project_id=second["id"] if via in ("task", "task_key", "turn_key") else project["id"],
            chapter_id=chapter_response.json()["id"] if via == "chapter" else None,
            binding="key" if via == "task_key" else "turn_key" if via == "turn_key" else "id",
        )
        await db.commit()
    response = await client.delete(f"/api/v1/projects/{second['id']}")
    assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    assert "外部调用私有作品" not in response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).deleted_at is None
        assert (await db.get(Project, second["id"])).deleted_at is None
        assert await db.get(AgentConversation, conversation_id) is not None
        assert await db.get(Task, task_id) is not None and (await db.get(AgentChange, change_id)).status == "executing"


@pytest.mark.parametrize("kind,state,result", [
    ("external", "executing", None),
    ("external", "performing", {}),
    ("unknown", "performing", {}),
    ("external", "failed", None),
    ("external", "failed", "invalid-result"),
    ("external", "failed", {}),
    ("external", "failed", {"error": None}),
    ("external", "executed", {}),
    ("external", "executed", {"text": None}),
    ("external", "failed", {"_external_claim_id": None}),
    ("external", "failed", {"_external_task_key": None}),
    ("external", "failed", {"error": "结果未知", "_external_outcome_unknown": True}),
    ("external", "failed", {"error": "结果未知", "_external_outcome_unknown": None}),
])
async def test_damaged_external_claims_are_not_treated_as_finished(app, client, make_client, kind, state, result):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=user["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        change = AgentChange(project_id=project["id"], conversation_id=conversation.id, turn_task_id=None,
                             tool_call_id="damaged", tool_name="external", kind=kind, status=state,
                             arguments={}, preview={}, result=result)
        db.add(change)
        await db.commit()
        change_id = change.id
    response = await client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change_id)).status == state
        assert (await db.get(Project, project["id"])).deleted_at is None


@pytest.mark.parametrize("operation", ["purge", "expire"])
async def test_recycled_project_with_executing_change_is_not_physically_deleted(
    app, client, make_client, system_admin, operation,
):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        if operation == "expire":
            row.deleted_at = datetime.now(UTC) - timedelta(days=31)
            row.last_deleted_at = row.deleted_at
        _, task_id, change_id = await _seed_change(
            db, project_id=project["id"], conversation_user_id=user["id"],
            task_user_id=user["id"], task_project_id=project["id"],
        )
        stamp = row.deleted_at
        await db.commit()
    if operation == "purge":
        listing = await system_admin.get("/api/v1/admin/recycle-bin/projects")
        item = next(value for value in listing.json()["items"] if value["id"] == project["id"])
        response = await system_admin.post(f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge", json={
            "deleted_at": item["deleted_at"], "title": item["title"], "confirm": True,
        })
        assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    else:
        assert await recycle_bin_service.purge_expired(app.state.task_engine) == 0
    async with app.state.sessionmaker() as db:
        saved = await db.get(Project, project["id"])
        assert saved is not None and saved.deleted_at.replace(tzinfo=UTC) == stamp.replace(tzinfo=UTC)
        assert (await db.get(Task, task_id)).status == "cancelled"
        assert (await db.get(AgentChange, change_id)).status == "executing"
        action = "project.purge" if operation == "purge" else "project.expire"
        audit = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == action,
                                                           AdminAuditLog.target_id == project["id"]))
        assert audit is not None and audit.result == "failure"


@pytest.mark.parametrize("operation", ["soft_delete", "purge", "expire"])
async def test_chapter_only_task_execution_prevents_project_cleanup(app, client, make_client, system_admin, operation):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "仍在收尾"})
    assert chapter.status_code == 201
    if operation != "soft_delete":
        assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        if operation == "expire":
            row.deleted_at = datetime.now(UTC) - timedelta(days=31)
            row.last_deleted_at = row.deleted_at
        stamp = row.deleted_at
        task = Task(user_id=user["id"], project_id=None, chapter_id=chapter.json()["id"],
                    kind="chapter.generate", status="cancelled", execution_key=str(uuid4()))
        db.add(task)
        await db.commit()
        task_id = task.id
    if operation == "soft_delete":
        response = await client.delete(f"/api/v1/projects/{project['id']}")
        assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    elif operation == "purge":
        listing = await system_admin.get("/api/v1/admin/recycle-bin/projects")
        item = next(value for value in listing.json()["items"] if value["id"] == project["id"])
        response = await system_admin.post(f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge", json={
            "deleted_at": item["deleted_at"], "title": item["title"], "confirm": True,
        })
        assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    else:
        assert await recycle_bin_service.purge_expired(app.state.task_engine) == 0
    async with app.state.sessionmaker() as db:
        saved = await db.get(Project, project["id"])
        assert saved is not None
        assert saved.deleted_at == stamp or saved.deleted_at.replace(tzinfo=UTC) == stamp.replace(tzinfo=UTC)
        assert saved.deletion_started_at is None
        saved_task = await db.get(Task, task_id)
        assert saved_task.chapter_id == chapter.json()["id"] and saved_task.execution_key is not None


async def test_unrelated_user_and_book_are_not_blocked_by_external_claim(app, client, make_client, system_admin):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    unrelated = make_client()
    unrelated_user = await register(unrelated, "external-unrelated")
    other = await create_project(client, title="独立作品")
    async with app.state.sessionmaker() as db:
        _, _, change_id = await _seed_change(db, project_id=project["id"], conversation_user_id=user["id"])
        await db.commit()
    assert (await system_admin.delete(f"/api/v1/admin/users/{unrelated_user['id']}")).status_code == 204
    assert (await client.delete(f"/api/v1/projects/{other['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change_id)).status == "executing"
        assert (await db.get(Project, project["id"])).deleted_at is None


@pytest.mark.parametrize("state,result", [("proposed", None), ("failed", {"error": "已结束"}),
                                           ("executed", {"text": "已结束"})])
async def test_finished_external_history_without_claim_preserves_normal_deletion(
    app, client, make_client, state, result,
):
    guest = make_client()
    _, user, project = await _users_and_project(app, client, guest)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=user["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add(AgentChange(project_id=project["id"], conversation_id=conversation.id, turn_task_id=None,
                           tool_call_id="finished", tool_name="external", kind="external", status=state,
                           arguments={}, preview={}, result=result))
        await db.commit()
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 204
