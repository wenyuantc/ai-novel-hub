"""用户和作品生命周期不得抹去未知提交的任务收据。"""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import (
    AgentChange,
    AgentConversation,
    AuthorizationState,
    ChapterAnalysis,
    ChapterVersion,
    Project,
    Task,
    User,
)
from app.services import admin_service, project_service, recycle_bin_service
from tests.helpers import analysis_json, create_ai_config, create_project, register, wait_task


async def _settled_checkpoint(app, task_id):
    for _ in range(500):
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, task_id)
            if task.execution_key is None and task.id not in app.state.task_engine._running:
                return task.checkpoint
        await asyncio.sleep(0.01)
    pytest.fail("已成功的任务未安全收尾")


@pytest.mark.parametrize(
    ("target", "case", "chapter_only"),
    [
        ("user", "analysis_pending", False),
        ("user", "personal_pending", False),
        ("user", "analysis_completion_null", True),
        ("user", "analysis_completion_damaged", False),
        ("user", "single_pending", True),
        ("user", "single_completion_damaged", False),
        ("user", "batch_pending", True),
        ("user", "batch_completion_damaged", False),
        ("user", "wrong_kind_receipt", False),
        ("user", "malformed_checkpoint", False),
        ("user", "external_task_input", False),
        ("user", "external_task_checkpoint", False),
        ("user", "external_mac_task_input", False),
        ("user", "external_mac_task_checkpoint", False),
        ("user", "batch_outcome_checkpoint", False),
        ("user", "batch_outcome_input", False),
        ("project", "analysis_pending", True),
        ("project", "analysis_completion_null", False),
        ("project", "single_pending", True),
        ("project", "single_completion_damaged", False),
        ("project", "batch_pending", True),
        ("project", "batch_completion_damaged", False),
        ("project", "cross_project_pending", False),
        ("project", "wrong_kind_receipt", False),
        ("project", "malformed_input", False),
        ("project", "external_task_input", False),
        ("project", "external_task_checkpoint", False),
        ("project", "external_mac_task_input", False),
        ("project", "external_mac_task_checkpoint", False),
        ("project", "batch_outcome_checkpoint", False),
        ("project", "batch_outcome_input", False),
        ("purge", "analysis_completion_damaged", True),
        ("purge", "single_pending", False),
        ("purge", "batch_pending", True),
        ("purge", "batch_completion_damaged", False),
        ("purge", "cross_project_pending", False),
        ("purge", "wrong_kind_receipt", False),
        ("purge", "malformed_checkpoint", False),
        ("purge", "external_task_input", False),
        ("purge", "external_task_checkpoint", False),
        ("purge", "external_mac_task_input", False),
        ("purge", "external_mac_task_checkpoint", False),
        ("purge", "batch_outcome_checkpoint", False),
        ("purge", "batch_outcome_input", False),
    ],
)
async def test_lifecycle_deletion_preserves_unresolved_receipt(
    app, client, make_client, system_admin, target, case, chapter_only,
):
    await app.state.task_engine.stop()
    await register(client, "receipt-owner")
    guest = make_client()
    user = await register(guest, "receipt-guest")
    project = await create_project(client, title="不能泄露的收据作品")
    other_project = await create_project(client, title="其他作品") if case == "cross_project_pending" else None
    chapter = await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "私人章节", "content": "原稿"},
    )
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    kinds = {
        "analysis_pending": ("chapter.analyze", "failed", {"_analysis_pending": None}, {}),
        "personal_pending": ("chapter.analyze", "failed", {"_analysis_pending": None}, {}),
        "cross_project_pending": ("chapter.analyze", "failed", {"_analysis_pending": None}, {}),
        "analysis_completion_null": ("chapter.analyze", "succeeded", {"_analysis_completion": None}, {}),
        "analysis_completion_damaged": ("chapter.analyze", "succeeded", {"_analysis_completion": {"schema": 1}}, {}),
        "single_pending": ("chapter.generate", "failed", None, {"_chapter_claim": None}),
        "single_completion_damaged": ("chapter.generate", "succeeded", {"_chapter_claim": {"schema": 1}}, {}),
        "batch_pending": ("chapter.batch", "cancelled", {"current": {"_chapter_claim": None}}, {}),
        "batch_completion_damaged": ("chapter.batch", "succeeded", {"_batch_completion": {"schema": 1}}, {}),
        "wrong_kind_receipt": ("chapter.generate", "succeeded", {"_analysis_completion": None}, {}),
        "malformed_checkpoint": ("chapter.analyze", "succeeded", ["收据损坏"], {}),
        "malformed_input": ("chapter.generate", "failed", None, ["输入损坏"]),
        "external_task_input": ("agent.turn", "failed", None, {"_external_execution": None}),
        "external_task_checkpoint": ("agent.turn", "failed", {"_external_execution": "损坏"}, {}),
        "external_mac_task_input": ("agent.turn", "failed", None, {"_external_snapshot_mac": None}),
        "external_mac_task_checkpoint": ("agent.turn", "failed", {"_external_snapshot_mac": "损坏"}, {}),
        "batch_outcome_checkpoint": ("chapter.batch", "failed", {"_batch_outcome_unknown": None}, {}),
        "batch_outcome_input": ("chapter.analyze", "succeeded", None, {"_batch_outcome_unknown": "损坏"}),
    }
    kind, status, checkpoint, task_input = kinds[case]
    task_project_id = (
        other_project["id"] if other_project is not None
        else None if chapter_only or case == "personal_pending"
        else project["id"]
    )
    stamp = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        task = Task(
            user_id=user["id"], project_id=task_project_id,
            chapter_id=None if case == "personal_pending" else chapter_id,
            kind=kind, status=status, checkpoint=checkpoint, input=task_input, title="私人待核对任务",
        )
        db.add(task)
        if target == "purge":
            (await db.get(Project, project["id"])).deleted_at = stamp
        await db.commit()
        task_id, task_key = task.id, task.statistics_key

    if target == "user":
        response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
        expected_code = "user_has_pending_tasks"
    elif target == "project":
        response = await client.delete(f"/api/v1/projects/{project['id']}")
        expected_code = "project_tasks_not_stopped"
    else:
        response = await system_admin.post(
            f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge",
            json={"deleted_at": stamp.isoformat(), "title": project["title"], "confirm": True},
        )
        expected_code = "project_tasks_not_stopped"
    assert response.status_code == 409 and response.json()["code"] == expected_code, response.text
    assert "私人" not in response.text and "不能泄露" not in response.text
    async with app.state.sessionmaker() as db:
        saved = await db.get(Project, project["id"])
        assert saved is not None
        assert (saved.deleted_at is not None) == (target == "purge")
        assert saved.deletion_started_at is None
        assert await db.get(User, user["id"]) is not None
        saved_task = await db.get(Task, task_id)
        assert saved_task is not None and saved_task.statistics_key == task_key
        assert saved_task.status == status and saved_task.checkpoint == checkpoint and saved_task.input == task_input


@pytest.mark.parametrize(
    ("target", "key", "marker"), [
        ("user", "_external_execution", None),
        ("project", "_external_execution", "损坏"),
        ("purge", "_external_execution", {"execution_key": "不完整"}),
        ("user", "_external_snapshot_mac", None),
        ("user", "_external_snapshot_mac", "损坏"),
        ("project", "_external_snapshot_mac", None),
        ("project", "_external_snapshot_mac", "损坏"),
        ("purge", "_external_snapshot_mac", None),
        ("purge", "_external_snapshot_mac", "损坏"),
    ],
)
async def test_lifecycle_deletion_preserves_detached_external_execution_marker(
    app, client, make_client, system_admin, target, key, marker,
):
    await app.state.task_engine.stop()
    await register(client, "external-receipt-owner")
    guest = make_client()
    user = await register(guest, "external-receipt-user")
    project = await create_project(client, title="外部调用保密作品")
    stamp = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(project_id=project["id"], user_id=user["id"])
        task = Task(user_id=user["id"], project_id=project["id"], kind="agent.turn", status="cancelled")
        db.add_all([conversation, task])
        await db.flush()
        change = AgentChange(
            project_id=project["id"], conversation_id=conversation.id, turn_task_id=task.id,
            turn_task_key=task.statistics_key, tool_call_id="external-receipt", tool_name="external",
            kind="external", status="failed", arguments={}, result={"error": "明确失败", key: marker},
        )
        db.add(change)
        if target == "purge":
            (await db.get(Project, project["id"])).deleted_at = stamp
        await db.commit()
        task_id, change_id = task.id, change.id
    if target == "user":
        response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
        expected = "user_has_pending_tasks"
    elif target == "project":
        response = await client.delete(f"/api/v1/projects/{project['id']}")
        expected = "project_tasks_not_stopped"
    else:
        response = await system_admin.post(
            f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge",
            json={"deleted_at": stamp.isoformat(), "title": project["title"], "confirm": True},
        )
        expected = "project_tasks_not_stopped"
    assert response.status_code == 409 and response.json()["code"] == expected, response.text
    assert "外部调用保密作品" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None
        assert await db.get(Project, project["id"]) is not None
        assert await db.get(Task, task_id) is not None
        change = await db.get(AgentChange, change_id)
        assert change is not None and change.result == {"error": "明确失败", key: marker}


@pytest.mark.parametrize("target", ["user", "project", "purge"])
@pytest.mark.parametrize("key", ["_external_execution", "_external_snapshot_mac"])
async def test_external_execution_linked_only_by_task_key_blocks_cross_project_deletion(
    app, client, make_client, system_admin, target, key,
):
    await app.state.task_engine.stop()
    owner = await register(client, "external-cross-owner")
    guest = make_client()
    user = await register(guest, "external-cross-user")
    source = await create_project(client, title="来源作品")
    destination = await create_project(client, title="关联作品")
    stamp = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(project_id=source["id"], user_id=owner["id"])
        task = Task(user_id=user["id"], project_id=destination["id"], kind="agent.turn", status="cancelled")
        db.add_all([conversation, task])
        await db.flush()
        change = AgentChange(
            project_id=source["id"], conversation_id=conversation.id, turn_task_id=None,
            turn_task_key=task.statistics_key, tool_call_id="cross-project-external", tool_name="external",
            kind="external", status="failed", arguments={}, result={"error": "已失败", key: None},
        )
        db.add(change)
        if target == "purge":
            (await db.get(Project, destination["id"])).deleted_at = stamp
        await db.commit()
        task_id, change_id = task.id, change.id
    if target == "user":
        response = await system_admin.delete(f"/api/v1/admin/users/{user['id']}")
        expected = "user_has_pending_tasks"
    elif target == "project":
        response = await client.delete(f"/api/v1/projects/{destination['id']}")
        expected = "project_tasks_not_stopped"
    else:
        response = await system_admin.post(
            f"/api/v1/admin/recycle-bin/projects/{destination['id']}/purge",
            json={"deleted_at": stamp.isoformat(), "title": destination["title"], "confirm": True},
        )
        expected = "project_tasks_not_stopped"
    assert response.status_code == 409 and response.json()["code"] == expected, response.text
    assert "来源作品" not in response.text and "关联作品" not in response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None
        assert await db.get(Project, destination["id"]) is not None
        task, change = await db.get(Task, task_id), await db.get(AgentChange, change_id)
        assert task is not None and change is not None and change.turn_task_key == task.statistics_key
        assert change.result == {"error": "已失败", key: None}


@pytest.mark.parametrize("target", ["user", "project"])
async def test_finished_legacy_without_unsettled_evidence_allows_deletion(
    app, client, make_client, system_admin, target,
):
    await app.state.task_engine.stop()
    await register(client, "legacy-receipt-owner")
    guest = make_client()
    user = await register(guest, "legacy-receipt-user")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="succeeded"))
        await db.commit()
    response = (
        await system_admin.delete(f"/api/v1/admin/users/{user['id']}") if target == "user"
        else await client.delete(f"/api/v1/projects/{project['id']}")
    )
    assert response.status_code == 204, response.text


async def test_unrelated_receipt_does_not_block_user_or_project_deletion(app, client, make_client, system_admin):
    await app.state.task_engine.stop()
    await register(client, "receipt-scope-owner")
    affected = make_client()
    user = await register(affected, "receipt-scope-affected")
    unrelated = make_client()
    other_user = await register(unrelated, "receipt-scope-unrelated")
    other_owner = make_client()
    await register(other_owner, "receipt-scope-other-owner")
    project = await create_project(client)
    other_project = await create_project(other_owner)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=project["id"], kind="chapter.analyze", status="failed",
                    checkpoint={"_analysis_pending": None})
        db.add(task)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key
    removed_user = await system_admin.delete(f"/api/v1/admin/users/{other_user['id']}")
    removed_project = await other_owner.delete(f"/api/v1/projects/{other_project['id']}")
    assert removed_user.status_code == 204, removed_user.text
    assert removed_project.status_code == 204, removed_project.text
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None
        assert (await db.get(Project, project["id"])).deleted_at is None
        task = await db.get(Task, task_id)
        assert task is not None and task.statistics_key == task_key and task.checkpoint == {"_analysis_pending": None}


@pytest.mark.parametrize("kind", ["analysis", "chapter"])
async def test_proven_completion_allows_project_deletion(app, client, fake_llm, system_admin, kind):
    await register(client, f"proven-{kind}-owner")
    await create_ai_config(client)
    project = await create_project(client)
    chapter = await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "章", "content": "林远走进山门。"},
    )
    assert chapter.status_code == 201, chapter.text
    fake_llm.push(analysis_json() if kind == "analysis" else "山门前的第一段故事。")
    endpoint = "analyze" if kind == "analysis" else "generate"
    accepted = await client.post(f"/api/v1/chapters/{chapter.json()['id']}/{endpoint}")
    assert accepted.status_code in (200, 202), accepted.text
    task_ref = accepted.json()
    assert (await wait_task(client, task_ref))["status"] == "succeeded"
    checkpoint = await _settled_checkpoint(app, task_ref["id"])
    key = "_analysis_completion" if kind == "analysis" else "_chapter_claim"
    assert isinstance(checkpoint.get(key), dict)
    response = await client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 204, response.text
    listing = await system_admin.get("/api/v1/admin/recycle-bin/projects")
    assert listing.status_code == 200, listing.text
    recycled = next(item for item in listing.json()["items"] if item["id"] == project["id"])
    purged = await system_admin.post(
        f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge",
        json={"deleted_at": recycled["deleted_at"], "title": recycled["title"], "confirm": True},
    )
    assert purged.status_code == 204, purged.text
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, project["id"]) is None
        assert await db.get(Task, task_ref["id"]) is None


@pytest.mark.parametrize("target", ["project", "purge"])
async def test_changed_analysis_result_invalidates_completed_receipt(
    app, client, fake_llm, system_admin, target,
):
    await register(client, f"changed-analysis-{target}")
    await create_ai_config(client)
    project = await create_project(client)
    chapter = await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "章", "content": "林远来到山门。"},
    )
    assert chapter.status_code == 201, chapter.text
    fake_llm.push(analysis_json(summary="原始已提交结果"))
    accepted = await client.post(f"/api/v1/chapters/{chapter.json()['id']}/analyze")
    assert accepted.status_code in (200, 202), accepted.text
    original = accepted.json()
    result = await wait_task(client, original)
    assert result["status"] == "succeeded"
    await _settled_checkpoint(app, original["id"])
    if target == "purge":
        deleted = await client.delete(f"/api/v1/projects/{project['id']}")
        assert deleted.status_code == 204, deleted.text
        listing = await system_admin.get("/api/v1/admin/recycle-bin/projects")
        recycled = next(item for item in listing.json()["items"] if item["id"] == project["id"])
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, original["id"])
        analysis = await db.get(ChapterAnalysis, task.checkpoint["_analysis_completion"]["analysis_id"])
        analysis.summary = "与原收据不同的结果"
        await db.commit()
    if target == "project":
        response = await client.delete(f"/api/v1/projects/{project['id']}")
    else:
        response = await system_admin.post(
            f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge",
            json={"deleted_at": recycled["deleted_at"], "title": recycled["title"], "confirm": True},
        )
    assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, project["id"]) is not None
        task = await db.get(Task, original["id"])
        assert task is not None and task.statistics_key == original["task_key"]


@pytest.mark.parametrize(("target", "alter"), [("project", "input"), ("purge", "version")])
async def test_changed_single_completion_invalidates_receipt(app, client, fake_llm, system_admin, target, alter):
    await register(client, f"changed-chapter-{target}")
    await create_ai_config(client)
    project = await create_project(client)
    chapter = await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "章", "content": "旧正文。"},
    )
    assert chapter.status_code == 201, chapter.text
    fake_llm.push("已提交的新正文。")
    accepted = await client.post(f"/api/v1/chapters/{chapter.json()['id']}/generate")
    assert accepted.status_code == 202, accepted.text
    original = accepted.json()
    assert (await wait_task(client, original))["status"] == "succeeded"
    await _settled_checkpoint(app, original["id"])
    if target == "purge":
        deleted = await client.delete(f"/api/v1/projects/{project['id']}")
        assert deleted.status_code == 204, deleted.text
        listing = await system_admin.get("/api/v1/admin/recycle-bin/projects")
        recycled = next(item for item in listing.json()["items"] if item["id"] == project["id"])
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, original["id"])
        if alter == "input":
            task.input = {**task.input, "request": {**task.input["request"], "tampered": "改变"}}
        else:
            version = await db.get(ChapterVersion, task.checkpoint["_chapter_claim"]["version_id"])
            version.content = "版本内容与收据不一致"
        await db.commit()
    if target == "project":
        response = await client.delete(f"/api/v1/projects/{project['id']}")
    else:
        response = await system_admin.post(
            f"/api/v1/admin/recycle-bin/projects/{project['id']}/purge",
            json={"deleted_at": recycled["deleted_at"], "title": recycled["title"], "confirm": True},
        )
    assert response.status_code == 409 and response.json()["code"] == "project_tasks_not_stopped", response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, project["id"]) is not None
        task = await db.get(Task, original["id"])
        assert task is not None and task.statistics_key == original["task_key"]


@pytest.mark.parametrize("change", ["receipt", "delete", "reassign", "unrelated"])
async def test_unflushed_receipt_does_not_flush_or_delete_user(app, client, make_client, system_admin, change):
    await app.state.task_engine.stop()
    owner = await register(client, "unflushed-receipt-owner")
    guest = make_client()
    user = await register(guest, "unflushed-receipt-user")
    project = await create_project(client)
    manager = (await system_admin.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"] if change == "unrelated" else user["id"],
                    project_id=project["id"], kind="chapter.analyze", status="failed")
        db.add(task)
        await db.commit()
        task_id = task.id
    async with app.state.sessionmaker() as db:
        actor, task = await db.get(User, manager["id"]), await db.get(Task, task_id)
        if change in ("receipt", "unrelated"):
            task.checkpoint = {"_analysis_pending": None}
        elif change == "delete":
            await db.delete(task)
        else:
            task.user_id = owner["id"]
        assert task in db.dirty or task in db.deleted
        flushed = []

        def before_flush(session, _context, _instances):
            if task in session.dirty or task in session.deleted:
                flushed.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        with pytest.raises(ConflictError) as error:
            await admin_service.delete_user(db, actor, user["id"])
        assert error.value.extra["code"] == "user_has_pending_tasks"
        assert not flushed
        assert task in (db.deleted if change == "delete" else db.dirty)
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None
        assert (await db.get(Task, task_id)).checkpoint is None


async def test_unchanged_task_does_not_block_user_deletion(app, client, system_admin):
    await app.state.task_engine.stop()
    user = await register(client, "unchanged-task-user")
    manager = (await system_admin.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], kind="chapter.analyze", status="succeeded")
        db.add(task)
        await db.commit()
        task_id = task.id
    async with app.state.sessionmaker() as db:
        actor, task = await db.get(User, manager["id"]), await db.get(Task, task_id)
        task.checkpoint = task.checkpoint
        assert task in db.dirty and not db.is_modified(task, include_collections=True)
        await admin_service.delete_user(db, actor, user["id"])
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is None
        assert await db.get(Task, task_id) is None


@pytest.mark.parametrize("action", ["clear", "delete", "conversation"])
async def test_unflushed_external_proof_is_never_committed_by_user_deletion(
    app, client, make_client, system_admin, action,
):
    await app.state.task_engine.stop()
    await register(client, "unflushed-external-owner")
    guest = make_client()
    user = await register(guest, "unflushed-external-user")
    project = await create_project(client)
    manager = (await system_admin.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(project_id=project["id"], user_id=user["id"])
        task = Task(user_id=user["id"], project_id=project["id"], kind="agent.turn", status="cancelled")
        db.add_all([conversation, task])
        await db.flush()
        change = AgentChange(
            project_id=project["id"], conversation_id=conversation.id, turn_task_id=task.id,
            turn_task_key=task.statistics_key, tool_call_id="pending-external", tool_name="external", kind="external",
            status="failed", arguments={}, result={"error": "结果未知", "_external_execution": None},
        )
        db.add(change)
        await db.commit()
        change_id = change.id
    async with app.state.sessionmaker() as db:
        actor, change = await db.get(User, manager["id"]), await db.get(AgentChange, change_id)
        staged = change
        if action == "clear":
            change.result = {"error": "明确失败"}
        elif action == "delete":
            await db.delete(change)
        else:
            staged = await db.get(AgentConversation, change.conversation_id)
            await db.delete(staged)
        flushed = []

        def before_flush(session, _context, _instances):
            if staged in session.dirty or staged in session.deleted:
                flushed.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        with pytest.raises(ConflictError) as error:
            await admin_service.delete_user(db, actor, user["id"])
        assert error.value.extra["code"] == "user_has_pending_tasks" and not flushed
        assert staged in (db.dirty if action == "clear" else db.deleted)
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None
        change = await db.get(AgentChange, change_id)
        assert change is not None and change.result == {"error": "结果未知", "_external_execution": None}


async def test_unauthorized_user_delete_keeps_unflushed_work(app, client, make_client):
    await app.state.task_engine.stop()
    actor = await register(client, "unflushed-user-unauthorized")
    guest = make_client()
    target = await register(guest, "unflushed-user-target")
    async with app.state.sessionmaker() as db:
        task = Task(user_id=target["id"], kind="agent.turn", status="failed")
        db.add(task)
        await db.commit()
        task_id = task.id
    async with app.state.sessionmaker() as db:
        actor_row, task = await db.get(User, actor["id"]), await db.get(Task, task_id)
        task.input = {"_external_execution": None}
        flushed = []

        def before_flush(session, _context, _instances):
            if task in session.dirty:
                flushed.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        with pytest.raises(ForbiddenError):
            await admin_service.delete_user(db, actor_row, target["id"])
        assert not flushed and task in db.dirty and task.input == {"_external_execution": None}
    async with app.state.sessionmaker() as db:
        assert await db.get(User, target["id"]) is not None
        assert (await db.get(Task, task_id)).input == {}


async def test_user_delete_does_not_commit_unrelated_authorization_change(app, client, system_admin):
    user = await register(client, "unflushed-auth-user")
    manager = (await system_admin.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        actor, auth_state = await db.get(User, manager["id"]), await db.get(AuthorizationState, 1)
        revision = auth_state.revision
        auth_state.revision = revision + 1
        flushed = []

        def before_flush(session, _context, _instances):
            if auth_state in session.dirty:
                flushed.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        with pytest.raises(ConflictError):
            await admin_service.delete_user(db, actor, user["id"])
        assert not flushed and auth_state in db.dirty and auth_state.revision == revision + 1
    async with app.state.sessionmaker() as db:
        assert await db.get(User, user["id"]) is not None
        assert (await db.get(AuthorizationState, 1)).revision == revision


async def test_unflushed_external_proof_is_not_committed_by_project_deletion(app, client):
    await app.state.task_engine.stop()
    owner = await register(client, "unflushed-project-proof-owner")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(project_id=project["id"], user_id=owner["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="cancelled")
        db.add_all([conversation, task])
        await db.flush()
        change = AgentChange(
            project_id=project["id"], conversation_id=conversation.id, turn_task_id=task.id,
            turn_task_key=task.statistics_key, tool_call_id="unflushed-project", tool_name="external", kind="external",
            status="failed", arguments={}, result={"error": "明确失败"},
        )
        db.add(change)
        await db.commit()
        change_id = change.id
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, change_id)
        change.result = {"error": "结果未知", "_external_execution": None}
        flushed = []

        def before_flush(session, _context, _instances):
            if change in session.dirty:
                flushed.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        with pytest.raises(ConflictError) as error:
            await project_service.delete_project(db, owner["id"], project["id"], engine=app.state.task_engine)
        assert error.value.extra["code"] == "project_tasks_not_stopped"
    async with app.state.sessionmaker() as db:
        saved = await db.get(Project, project["id"])
        assert saved is not None and saved.deleted_at is None and saved.deletion_started_at is None
        assert (await db.get(AgentChange, change_id)).result == {"error": "明确失败"}
    assert not flushed


@pytest.mark.parametrize("scenario", ["unrelated", "unauthorized"])
async def test_project_delete_respects_unflushed_other_task_and_authorization(app, client, make_client, scenario):
    await app.state.task_engine.stop()
    owner = await register(client, "unflushed-scope-owner")
    target = await create_project(client)
    outsider = make_client()
    other_user = await register(outsider, "unflushed-scope-other")
    other_project = await create_project(outsider)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=other_user["id"], project_id=other_project["id"], kind="agent.turn", status="failed")
        db.add(task)
        await db.commit()
        task_id = task.id
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task.input = {"_external_execution": None}
        flushed = []

        def before_flush(session, _context, _instances):
            if task in session.dirty:
                flushed.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        actor_id = owner["id"] if scenario == "unrelated" else other_user["id"]
        error_type = ConflictError if scenario == "unrelated" else NotFoundError
        with pytest.raises(error_type):
            await project_service.delete_project(db, actor_id, target["id"], engine=app.state.task_engine)
        assert not flushed
        assert task in db.dirty and task.input == {"_external_execution": None}
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, target["id"])).deleted_at is None
        assert (await db.get(Task, task_id)).input == {}


@pytest.mark.parametrize("scenario", ["admin", "unauthorized"])
async def test_purge_respects_unflushed_external_proof_and_authorization(app, client, system_admin, scenario):
    await app.state.task_engine.stop()
    owner = await register(client, "unflushed-purge-owner")
    project = await create_project(client)
    manager = (await system_admin.get("/api/v1/auth/me")).json()
    stamp = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(project_id=project["id"], user_id=owner["id"])
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="cancelled")
        db.add_all([conversation, task])
        await db.flush()
        change = AgentChange(
            project_id=project["id"], conversation_id=conversation.id, turn_task_id=task.id,
            turn_task_key=task.statistics_key, tool_call_id="unflushed-purge", tool_name="external", kind="external",
            status="failed", arguments={}, result={"error": "明确失败"},
        )
        db.add(change)
        (await db.get(Project, project["id"])).deleted_at = stamp
        await db.commit()
        change_id = change.id
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, change_id)
        change.result = {"error": "结果未知", "_external_execution": None}
        flushed = []

        def before_flush(session, _context, _instances):
            if change in session.dirty:
                flushed.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        actor_id = manager["id"] if scenario == "admin" else owner["id"]
        error_type = ConflictError if scenario == "admin" else ForbiddenError
        with pytest.raises(error_type):
            await recycle_bin_service.purge(
                db, app.state.task_engine, actor_id, project["id"], stamp, title=project["title"],
            )
        assert not flushed
        assert change in db.dirty and change.result == {"error": "结果未知", "_external_execution": None}
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).deleted_at is not None
        assert (await db.get(AgentChange, change_id)).result == {"error": "明确失败"}
