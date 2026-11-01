"""SQLite独立事务/Event屏障：实际写前撤权与commit-return发布窗口。

这些测试不声称替代PostgreSQL行锁实测；它们证明同应用runtime、CAS及写前重读契约。
"""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Task, User, WritingStyle
from app.services import authorization_service as authz
from app.services import quota_service
from app.services.audit_writer import AuditOperation
from tests.helpers import Gate, create_ai_config, create_project, register, wait_task
from tests.rbac_helpers import assign_roles, authorization_revision, create_role, set_quota
from tests.test_book_import import _start, upload


async def test_role_edit_commits_before_waiting_management_write_rechecks_actor(
    client, system_admin, make_client, monkeypatch,
):
    actor = await register(client, "operator")
    target_client = make_client()
    target = await register(target_client, "target")
    role = await create_role(system_admin, "账号状态管理", ["users.read", "users.status"])
    await assign_roles(system_admin, actor["id"], [role["id"]])
    reached, release = asyncio.Event(), asyncio.Event()
    original = authz.gate

    async def gate(db):
        if asyncio.current_task().get_name() == "waiting-management-write":
            reached.set()
            await release.wait()
        await original(db)

    monkeypatch.setattr(authz, "gate", gate)
    pending = asyncio.create_task(client.patch(
        f"/api/v1/admin/users/{target['id']}", json={"is_active": False},
    ), name="waiting-management-write")
    try:
        await asyncio.wait_for(reached.wait(), 5)
        changed = await system_admin.patch(f"/api/v1/admin/roles/{role['id']}", json={
            "authorization_revision": await authorization_revision(system_admin),
            "revision": role["revision"], "permissions": [],
        })
        assert changed.status_code == 200, changed.text
    finally:
        release.set()
    response = await pending
    assert response.status_code == 403 and response.json()["code"] == "permission_denied"
    assert (await target_client.get("/api/v1/auth/me")).status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_management_write_holds_shared_sequence_point_against_role_edit(
    app, client, system_admin, make_client, monkeypatch,
):
    actor = await register(client, "operator")
    target = await register(make_client(), "target")
    role = await create_role(system_admin, "状态操作", ["users.read", "users.status"])
    await assign_roles(system_admin, actor["id"], [role["id"]])
    revision = await authorization_revision(system_admin)
    reached, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_commit, original_gate = AuditOperation.commit, authz.gate

    async def commit(audit, **kwargs):
        if audit.values["action"] == "user.update":
            reached.set()
            await release.wait()
        return await original_commit(audit, **kwargs)

    async def gate(db):
        if asyncio.current_task().get_name() == "waiting-role-edit":
            attempted.set()
        await original_gate(db)

    monkeypatch.setattr(AuditOperation, "commit", commit)
    monkeypatch.setattr(authz, "gate", gate)
    writer = asyncio.create_task(client.patch(
        f"/api/v1/admin/users/{target['id']}", json={"is_active": False},
    ), name="management-write")
    editor = None
    try:
        await asyncio.wait_for(reached.wait(), 5)
        editor = asyncio.create_task(system_admin.patch(f"/api/v1/admin/roles/{role['id']}", json={
            "authorization_revision": revision, "revision": role["revision"], "permissions": [],
        }), name="waiting-role-edit")
        await asyncio.wait_for(attempted.wait(), 5)
        runtime = authz.runtime_for_factory(app.state.sessionmaker)
        assert runtime._owner is writer and not editor.done()
    finally:
        release.set()
    assert (await writer).status_code == 200
    response = await editor
    assert response.status_code == 409 and response.json()["code"] == "stale_revision"
    response = await system_admin.patch(f"/api/v1/admin/roles/{role['id']}", json={
        "authorization_revision": await authorization_revision(system_admin),
        "revision": role["revision"], "permissions": [],
    })
    assert response.status_code == 200
    async with app.state.sessionmaker() as db:
        assert (await db.get(User, target["id"])).is_active is False
    assert (await client.get("/api/v1/admin/users")).status_code == 403


async def test_role_cas_and_deleted_user_id_reuse_cannot_apply_old_quota_request(
    client, system_admin,
):
    user = await register(client)
    role = await create_role(system_admin, "CAS角色", ["users.read"])
    body = {"revision": role["revision"], "authorization_revision": await authorization_revision(system_admin)}
    first, second = await asyncio.gather(
        system_admin.patch(f"/api/v1/admin/roles/{role['id']}", json={**body, "name": "版本甲"}),
        system_admin.patch(f"/api/v1/admin/roles/{role['id']}", json={**body, "name": "版本乙"}),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    stale = first if first.status_code == 409 else second
    assert stale.json()["code"] == "stale_revision"
    path = f"/api/v1/admin/users/{user['id']}/quotas"
    quota = (await system_admin.get(path)).json()
    assert (await system_admin.delete(f"/api/v1/admin/users/{user['id']}")).status_code == 204
    new = await system_admin.post("/api/v1/admin/users", json={"username": "新账号", "password": "secret123"})
    assert new.status_code == 201 and new.json()["id"] == user["id"]
    response = await system_admin.patch(path, json={
        "revision": quota["revision"], "authorization_revision": quota["authorization_revision"],
        "changes": {"project_count": {"mode": "limited", "value": 0}},
    })
    assert response.status_code == 409 and response.json()["code"] == "stale_revision"
    current = (await system_admin.get(path)).json()
    assert current["username"] == "新账号" and current["project_count"]["config"] == {"mode": "inherit"}


async def test_mixed_creation_paths_compete_for_last_slot_under_owner_lock(
    app, client, system_admin, monkeypatch,
):
    user = await register(client)
    seed = await create_project(client)
    await client.post(f"/api/v1/projects/{seed['id']}/chapters", json={"title": "原稿", "content": "原正文"})
    backup = await client.get(f"/api/v1/projects/{seed['id']}/backup")
    preview = await client.post("/api/v1/projects/import/preview", files={
        "file": ("backup.json", backup.content, "application/json"),
    })
    assert preview.status_code == 200, preview.text
    token = preview.json()["token"]
    book = await upload(client)
    assert book.status_code == 201, book.text
    inspiration = (await client.post("/api/v1/inspiration/sessions", json={"idea": "星河"})).json()
    await set_quota(system_admin, user["id"], {"project_count": {"mode": "limited", "value": 2}})
    async with app.state.sessionmaker() as db:
        before_styles = await db.scalar(select(func.count(WritingStyle.id)))
    reached, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Queue()
    original = quota_service.check_project_creation

    async def check(db, user_id):
        name = asyncio.current_task().get_name()
        if name.startswith("competing-project-"):
            attempted.put_nowait(name)
        await original(db, user_id)
        if name == "first-project":
            reached.set()
            await release.wait()

    monkeypatch.setattr(quota_service, "check_project_creation", check)
    first = asyncio.create_task(client.post("/api/v1/projects", json={"title": "先到"}), name="first-project")
    competitors = []
    try:
        await asyncio.wait_for(reached.wait(), 5)
        competitors = [
            asyncio.create_task(client.post(
                f"/api/v1/inspiration/sessions/{inspiration['id']}/convert", json={"title_override": "后到"},
            ), name="competing-project-inspiration"),
            asyncio.create_task(client.post("/api/v1/projects/import", json={"token": token}),
                                name="competing-project-backup"),
            asyncio.create_task(_start(client, book.json()), name="competing-project-book"),
        ]
        observed = {await asyncio.wait_for(attempted.get(), 5) for _ in competitors}
        assert observed == {task.get_name() for task in competitors}
    finally:
        release.set()
    assert (await first).status_code == 201
    responses = await asyncio.gather(*competitors)
    assert all(response.status_code == 409 and response.json()["code"] == "quota_exceeded" for response in responses)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id)).where(Project.user_id == user["id"])) == 2
        assert await db.scalar(select(func.count(WritingStyle.id))) == before_styles
        assert await db.scalar(select(func.count(Task.id))) == 0
    assert (await client.get(f"/api/v1/book-imports/{book.json()['id']}")).json()["revision"] == book.json()["revision"]
    assert (await client.get(f"/api/v1/inspiration/sessions/{inspiration['id']}")).json()["status"] == "active"
    # 失败不消费备份预览token；在相同满额条件下重试仍是quota拒绝，不是过期凭据。
    retry = await client.post("/api/v1/projects/import", json={"token": token})
    assert retry.status_code == 409 and retry.json()["code"] == "quota_exceeded"


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
async def test_candidate_rechecks_lowered_quota_and_claim_holds_commit_return_gap(
    app, client, system_admin, fake_llm, monkeypatch,
):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 2}})
    engine = app.state.task_engine
    original_claim = engine._claim_and_run
    candidates: dict[int, asyncio.Event] = {}
    selected = asyncio.Queue()

    async def selected_claim(task_id, key):
        event = candidates.setdefault(task_id, asyncio.Event())
        selected.put_nowait(task_id)
        await event.wait()
        await original_claim(task_id, key)

    monkeypatch.setattr(engine, "_claim_and_run", selected_claim)
    llm_gate = Gate()
    fake_llm.push([llm_gate, "正文"])
    rows = []
    for index in range(2):
        chapter = (await client.post(
            f"/api/v1/projects/{project['id']}/chapters", json={"title": str(index)},
        )).json()
        response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
        assert response.status_code == 202, response.text
        rows.append(response.json())
    ids = [await asyncio.wait_for(selected.get(), 5) for _ in range(2)]
    assert set(ids) == {row["id"] for row in rows}
    assert engine.admitted_count(user["id"]) == 0
    # 两个预候选都已登记，后来的降额必须在真实claim复核；候选本身不耗占用。
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 1}})
    committed, return_commit, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_commit = AsyncSession.commit
    original_acquire = engine.runtime.acquire
    paused_once = False

    async def commit(db):
        nonlocal paused_once
        await original_commit(db)
        if asyncio.current_task().get_name() == f"task-{ids[0]}" and not paused_once:
            paused_once = True
            committed.set()
            await return_commit.wait()

    async def acquire():
        if asyncio.current_task().get_name() == f"task-{ids[1]}":
            attempting.set()
        return await original_acquire()

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(engine.runtime, "acquire", acquire)
    completed = asyncio.Event()
    original_done = engine._on_done

    def done(task_id):
        original_done(task_id)
        if task_id == ids[1]:
            completed.set()

    monkeypatch.setattr(engine, "_on_done", done)
    try:
        candidates[ids[0]].set()
        await asyncio.wait_for(committed.wait(), 5)
        assert engine.admitted_count(user["id"]) == 0
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, ids[0])).status == "running"
        candidates[ids[1]].set()
        await asyncio.wait_for(attempting.wait(), 5)
        assert engine.runtime._owner is engine._running[ids[0]]
        assert not engine._running[ids[1]].done()
        return_commit.set()
        await llm_gate.wait_reached()
        await asyncio.wait_for(completed.wait(), 5)
        assert engine.admitted_count(user["id"]) == 1
        assert (await client.get(f"/api/v1/tasks/{ids[1]}")).json()["status"] == "queued"
        assert len(fake_llm.calls) == 1
        # 降到0不会取消已准入任务，也不会让排队轮次提前claim。
        await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 0}})
    finally:
        return_commit.set()
        for event in candidates.values():
            event.set()
        llm_gate.release()
    assert (await wait_task(client, ids[0]))["status"] == "succeeded"
    assert (await client.post(f"/api/v1/tasks/{ids[1]}/cancel")).status_code == 200


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
async def test_database_terminal_state_does_not_release_post_success_execution_slot(
    app, client, system_admin, fake_llm, monkeypatch,
):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 1}})
    engine = app.state.task_engine
    original_success = engine._on_success
    terminal, finish_success = asyncio.Event(), asyncio.Event()
    first_id = None

    async def success(handler, task):
        if task.id == first_id:
            terminal.set()
            await finish_success.wait()
        await original_success(handler, task)

    monkeypatch.setattr(engine, "_on_success", success)
    generation = Gate()
    fake_llm.push([generation, "第一章"])
    second_generation = Gate()
    fake_llm.push([second_generation, "第二章"])
    rows = []
    for index in range(2):
        chapter = (await client.post(
            f"/api/v1/projects/{project['id']}/chapters", json={"title": str(index)},
        )).json()
        response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
        assert response.status_code == 202, response.text
        rows.append(response.json())
        if index == 0:
            first_id = rows[0]["id"]
    try:
        await generation.wait_reached()
        generation.release()
        await asyncio.wait_for(terminal.wait(), 5)
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, first_id)).status == "succeeded"
        assert engine.admitted_count(user["id"]) == 1
        quota = (await system_admin.get(f"/api/v1/admin/users/{user['id']}/quotas")).json()
        assert quota["task_concurrency"]["used"] == 1
        assert (await client.get(f"/api/v1/tasks/{rows[1]['id']}")).json()["status"] == "queued"
        assert not second_generation.reached.is_set()
        finish_success.set()
        await second_generation.wait_reached()
        assert engine.admitted_count(user["id"]) == 1
    finally:
        generation.release()
        finish_success.set()
        second_generation.release()
    for row in rows:
        assert (await wait_task(client, row["id"]))["status"] == "succeeded"


@pytest.mark.parametrize("commit_was_applied", [False, True])
@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
async def test_uncertain_claim_commit_reconciles_same_uuid_without_duplicate_execution(
    app, client, system_admin, fake_llm, monkeypatch, commit_was_applied,
):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    await set_quota(system_admin, user["id"], {"task_concurrency": {"mode": "limited", "value": 1}})
    original = AsyncSession.commit
    injected = False
    claimed = asyncio.Event()

    async def commit(db):
        nonlocal injected
        if asyncio.current_task().get_name().startswith("task-") and not injected:
            injected = True
            if commit_was_applied:
                await original(db)
            claimed.set()
            raise RuntimeError("可控的提交确认故障")
        await original(db)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    fake_llm.push("只执行一次")
    chapter = (await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "提交确认"},
    )).json()
    started = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
    assert started.status_code == 202, started.text
    await asyncio.wait_for(claimed.wait(), 5)
    finished = await wait_task(client, started.json()["id"])
    assert finished["status"] == "succeeded"
    assert len(fake_llm.calls) == 1
    assert finished["id"] == started.json()["id"]


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
async def test_unknown_settings_commit_pauses_admission_until_confirmed_publish(
    app, client, system_admin, fake_llm, monkeypatch,
):
    user = await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    engine, manager = app.state.task_engine, app.state.system_settings
    original_claim, original_commit, original_reload = engine._claim_and_run, AsyncSession.commit, manager._reload
    selected, start_claim, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    committed, allow_read = False, False

    async def claim(task_id, key):
        selected.set()
        await start_claim.wait()
        await original_claim(task_id, key)

    async def commit(db):
        nonlocal committed
        await original_commit(db)
        if asyncio.current_task().get_name() == "system-settings-write" and not committed:
            committed = True
            raise RuntimeError("提交已落库但确认丢失")

    async def reload():
        if committed and not allow_read:
            raise RuntimeError("事实读取暂不可用")
        return await original_reload()

    original_done = engine._on_done

    def done(task_id):
        original_done(task_id)
        rejected.set()

    monkeypatch.setattr(engine, "_claim_and_run", claim)
    monkeypatch.setattr(engine, "_on_done", done)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(manager, "_reload", reload)
    generation = Gate()
    fake_llm.push([generation, "恢复后正文"])
    chapter = (await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "确认后启动"},
    )).json()
    started = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
    assert started.status_code == 202, started.text
    await asyncio.wait_for(selected.wait(), 5)
    current = (await system_admin.get("/api/v1/admin/system-settings")).json()
    try:
        response = await system_admin.patch("/api/v1/admin/system-settings", json={
            "revision": current["revision"], "changes": {"task_max_per_user": 1},
        })
        assert response.status_code == 500
        assert committed and not engine.runtime.settings_confirmed
        start_claim.set()
        await asyncio.wait_for(rejected.wait(), 5)
        assert engine.admitted_count(user["id"]) == 0 and not fake_llm.calls
        assert (await client.get(f"/api/v1/tasks/{started.json()['id']}")).json()["status"] == "queued"
        allow_read = True
        await manager.load()
        assert engine.runtime.settings_confirmed and app.state.settings.task_max_per_user == 1
        await generation.wait_reached()
        assert engine.admitted_count(user["id"]) == 1
    finally:
        allow_read = True
        start_claim.set()
        generation.release()
    assert (await wait_task(client, started.json()["id"]))["status"] == "succeeded"
    assert len(fake_llm.calls) == 1


async def test_settings_load_publishes_only_inside_shared_runtime(app, monkeypatch):
    runtime, manager = app.state.task_engine.runtime, app.state.system_settings
    original = runtime.acquire
    attempting = asyncio.Event()

    async def acquire():
        if asyncio.current_task().get_name() == "settings-load":
            attempting.set()
        return await original()

    monkeypatch.setattr(runtime, "acquire", acquire)
    async with runtime.hold():
        loading = asyncio.create_task(manager.load(), name="settings-load")
        await asyncio.wait_for(attempting.wait(), 5)
        assert not loading.done() and runtime._owner is asyncio.current_task()
    result = await asyncio.wait_for(loading, 5)
    assert result.revision == 0 and runtime.settings_confirmed


@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": False}])
@pytest.mark.parametrize("claim_gap", ["commit_return", "session_exit"])
@pytest.mark.parametrize("stop_action", ["owner_cancel", "shutdown"])
async def test_cancel_in_claim_gap_defers_signal_until_safe_finalize(
    app, client, fake_llm, monkeypatch, claim_gap, stop_action,
):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    engine = app.state.task_engine
    original_commit, original_exit, original_cancel = AsyncSession.commit, AsyncSession.__aexit__, engine.cancel_running
    committed, return_commit, cancellation = asyncio.Event(), asyncio.Event(), asyncio.Event()
    paused = False

    def is_execution():
        name = asyncio.current_task().get_name()
        return name.startswith("task-") and name[5:].isdigit()

    async def commit(db):
        nonlocal paused
        await original_commit(db)
        if claim_gap == "commit_return" and is_execution() and not paused:
            paused = True
            committed.set()
            await return_commit.wait()

    async def exit_session(db, *args):
        nonlocal paused
        if claim_gap == "session_exit" and is_execution() and not paused:
            paused = True
            committed.set()
            await return_commit.wait()
        return await original_exit(db, *args)

    async def cancel(task_id, **kwargs):
        cancellation.set()
        await original_cancel(task_id, **kwargs)

    class ShutdownIntent(set):
        def add(self, task_id):
            super().add(task_id)
            cancellation.set()

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(AsyncSession, "__aexit__", exit_session)
    monkeypatch.setattr(engine, "cancel_running", cancel)
    monkeypatch.setattr(engine, "_shutdown", ShutdownIntent())
    fake_llm.push("不应开始执行")
    chapter = (await client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"title": "领取窗口取消"},
    )).json()
    response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    pending = None
    try:
        await asyncio.wait_for(committed.wait(), 5)
        assert task_id in engine._claiming
        assert engine.admitted_count() == (1 if claim_gap == "session_exit" else 0)
        pending = asyncio.create_task(
            client.post(f"/api/v1/tasks/{task_id}/cancel") if stop_action == "owner_cancel" else engine.stop()
        )
        await asyncio.wait_for(cancellation.wait(), 5)
        assert task_id in (engine._cancel_requested if stop_action == "owner_cancel" else engine._shutdown)
        assert not engine._running[task_id].cancelled() and task_id in engine._claiming
    finally:
        return_commit.set()
    response = await asyncio.wait_for(pending, 5)
    if stop_action == "owner_cancel":
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        assert task.status == ("cancelled" if stop_action == "owner_cancel" else "interrupted")
    assert not fake_llm.calls
    assert engine.admitted_count() == 0 and task_id not in engine._claiming
    chapter = (await client.get(f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}")).json()
    assert chapter["status"] == "draft"


async def test_role_definition_can_make_target_management_before_waiting_status_write(
    client, system_admin, make_client, monkeypatch,
):
    actor = await register(client, "普通账号维护者")
    target = await register(make_client(), "空角色账号")
    operator = await create_role(system_admin, "状态维护", ["users.read", "users.status"])
    initially_empty = await create_role(system_admin, "后来变为管理角色", [])
    await assign_roles(system_admin, actor["id"], [operator["id"]])
    await assign_roles(system_admin, target["id"], [initially_empty["id"]])
    reached, release = asyncio.Event(), asyncio.Event()
    original = authz.gate

    async def gate(db):
        if asyncio.current_task().get_name() == "waiting-target-write":
            reached.set()
            await release.wait()
        await original(db)

    monkeypatch.setattr(authz, "gate", gate)
    writer = asyncio.create_task(client.patch(
        f"/api/v1/admin/users/{target['id']}", json={"is_active": False},
    ), name="waiting-target-write")
    try:
        await asyncio.wait_for(reached.wait(), 5)
        response = await system_admin.patch(f"/api/v1/admin/roles/{initially_empty['id']}", json={
            "authorization_revision": await authorization_revision(system_admin),
            "revision": initially_empty["revision"], "permissions": ["projects.read"],
        })
        assert response.status_code == 200, response.text
    finally:
        release.set()
    response = await writer
    assert response.status_code == 403 and response.json()["code"] == "management_account_protected"
    detail = (await system_admin.get(f"/api/v1/admin/users/{target['id']}")).json()
    assert detail["is_active"] is True and detail["is_management_account"] is True


async def test_role_delete_assignment_race_and_database_restrict(app, client, system_admin):
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError

    from app.models import Role, UserRole

    user = await register(client)
    role = await create_role(system_admin, "删除分配竞争", ["projects.read"])
    revision = await authorization_revision(system_admin)
    deletion, assignment = await asyncio.gather(
        system_admin.delete(f"/api/v1/admin/roles/{role['id']}", params={
            "revision": role["revision"], "authorization_revision": revision,
        }),
        system_admin.put(f"/api/v1/admin/users/{user['id']}/roles", json={
            "authorization_revision": revision, "role_ids": [role["id"]],
        }),
    )
    assert (deletion.status_code, assignment.status_code) in ((204, 409), (409, 200))
    conflict = assignment if assignment.status_code == 409 else deletion
    assert conflict.json()["code"] == "stale_revision"
    if deletion.status_code == 204:
        role = await create_role(system_admin, "数据库限制", ["projects.read"])
        await assign_roles(system_admin, user["id"], [role["id"]])
    async with app.state.sessionmaker() as db:
        assert await db.get(UserRole, (user["id"], role["id"])) is not None
        with pytest.raises(IntegrityError):
            await db.execute(delete(Role).where(Role.id == role["id"]))
        await db.rollback()
        assert await db.get(Role, role["id"]) is not None


async def test_two_super_admins_cannot_concurrently_remove_each_other(app, client, system_admin):
    from app.models import Role

    second = await register(client, "第二超管")
    async with app.state.sessionmaker() as db:
        super_id = await db.scalar(select(Role.id).where(Role.system_key == "super_admin"))
    await assign_roles(system_admin, second["id"], [super_id])
    first = (await system_admin.get("/api/v1/auth/me")).json()
    revision = first["authorization_revision"]
    left, right = await asyncio.gather(
        system_admin.put(f"/api/v1/admin/users/{second['id']}/roles", json={
            "authorization_revision": revision, "role_ids": [],
        }),
        client.put(f"/api/v1/admin/users/{first['id']}/roles", json={
            "authorization_revision": revision, "role_ids": [],
        }),
    )
    assert sorted([left.status_code, right.status_code]) == [200, 403]
    async with app.state.sessionmaker() as db:
        supers = await db.scalar(select(func.count(User.id)).where(User.is_admin.is_(True), User.is_active.is_(True)))
        assert supers == 1
