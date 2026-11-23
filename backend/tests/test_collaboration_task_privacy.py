"""个人任务的协作权限与流在撤权后的行为。"""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.core.errors import NotFoundError, UnauthorizedError
from app.models import Chapter, GenerationLog, ProjectCollaborator, Task, User
from app.services import personal_projection, task_service
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import bind_task_grant, task_grant_identity
from tests.helpers import create_ai_config, create_project, register, task_params


async def _setup(app, owner, guest):
    await app.state.task_engine.stop()
    owner_user = await register(owner, "task-owner")
    guest_user = await register(guest, "task-guest")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invitation = await owner.post(f"{base}/collaborators", json={"username": "task-guest", "role": "editor"})
    assert invitation.status_code == 201, invitation.text
    invite = invitation.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
        json={"expected_revision": invite["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return owner_user, guest_user, project, accepted.json()


async def _tasks(app, owner_id, guest_id, project_id, invitation_id, revision):
    valid_grant = {"invitation_id": invitation_id, "revision": revision}
    async with app.state.sessionmaker() as db:
        entries = [
            Task(user_id=owner_id, project_id=project_id, kind="chapter.generate", status="succeeded",
                 output_text="作者私人输出", title="owner"),
            Task(user_id=guest_id, project_id=None, kind="book.import", status="succeeded", title="personal"),
            Task(user_id=guest_id, project_id=project_id, kind="chapter.generate", status="succeeded",
                 output_text="自己的完成输出", title="finished"),
            Task(user_id=guest_id, project_id=project_id, kind="chapter.generate", status="running",
                 input={"_collaboration_grant": valid_grant}, output_text="自己的活动输出", title="valid"),
            Task(user_id=guest_id, project_id=project_id, kind="chapter.generate", status="queued",
                 input={}, title="missing"),
            Task(user_id=guest_id, project_id=project_id, kind="chapter.generate", status="queued",
                 input={"_collaboration_grant": {"invitation_id": invitation_id, "revision": str(revision)}},
                 title="string-revision"),
            Task(user_id=guest_id, project_id=project_id, kind="chapter.generate", status="queued",
                 input={"_collaboration_grant": {"invitation_id": invitation_id, "revision": True}},
                 title="bool-revision"),
            Task(user_id=guest_id, project_id=project_id, kind="chapter.generate", status="paused",
                 input={"_collaboration_grant": {"invitation_id": str(uuid4()), "revision": revision}},
                 title="old-grant"),
        ]
        db.add_all(entries)
        await db.commit()
        return {entry.title: (entry.id, entry.statistics_key) for entry in entries}


async def test_task_http_filters_owner_membership_and_active_grant_before_limit(app, client, make_client):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    base = "/api/v1/tasks"

    visible = await guest.get(base, params={"limit": 1})
    assert visible.status_code == 200 and [row["id"] for row in visible.json()] == [tasks["valid"][0]]
    output = await guest.get(f"{base}/{tasks['valid'][0]}/output", params=task_params(tasks["valid"]))
    assert output.json()["text"] == "自己的活动输出"
    for name in ("owner", "missing", "string-revision", "bool-revision", "old-grant"):
        assert (await guest.get(f"{base}/{tasks[name][0]}", params=task_params(tasks[name]))).status_code == 404
    assert (await guest.get(f"{base}/{tasks['personal'][0]}", params=task_params(tasks["personal"]))).status_code == 200
    other = await client.get(f"{base}/{tasks['finished'][0]}", params=task_params(tasks["finished"]))
    assert other.status_code == 404

    project_base = f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}"
    changed = await client.patch(project_base, json={"expected_revision": member["revision"], "role": "viewer"})
    assert changed.status_code == 200
    assert (await guest.get(f"{base}/{tasks['valid'][0]}", params=task_params(tasks["valid"]))).status_code == 404
    output = await guest.get(f"{base}/{tasks['finished'][0]}/output", params=task_params(tasks["finished"]))
    assert output.json()["text"] == "自己的完成输出"
    async with app.state.sessionmaker() as db:
        (await db.get(User, guest_user["id"])).is_admin = True
        await db.commit()
    assert (await guest.get(f"{base}/{tasks['valid'][0]}", params=task_params(tasks["valid"]))).status_code == 404
    revoked = await client.delete(project_base, params={"expected_revision": changed.json()["revision"]})
    assert revoked.status_code == 204
    assert (await guest.get(f"{base}/{tasks['finished'][0]}", params=task_params(tasks["finished"]))).status_code == 404
    assert (await guest.get(f"{base}/{tasks['personal'][0]}", params=task_params(tasks["personal"]))).status_code == 200
    assert [row["id"] for row in (await guest.get(base)).json()] == [tasks["personal"][0]]


async def test_stream_rechecks_membership_before_queued_data_and_new_invitation(app, client, make_client):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    task_id, key = tasks["valid"]
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task_id, statistics_key=key, status="running", text="此前内容")
    stream = engine.subscribe(task_id, statistics_key=key, actor_id=guest_user["id"], token_version=0,
                              active_grant_required=True)
    event, payload = await anext(stream)
    assert event == "snapshot" and payload["text"] == "此前内容"
    hub.append("撤权后不可发送的内容")
    project_base = f"/api/v1/projects/{project['id']}"
    assert (await client.delete(f"{project_base}/collaborators/{guest_user['id']}", params={
        "expected_revision": member["revision"],
    })).status_code == 204
    event, payload = await anext(stream)
    assert event == "error" and payload == {"task": None, "detail": "任务访问权限已失效", "code": "permission_denied"}
    await stream.aclose()

    invitation = await client.post(f"{project_base}/collaborators", json={"username": "task-guest", "role": "editor"})
    new = invitation.json()
    assert new["invitation_id"] != member["invitation_id"]
    assert (await guest.post(f"/api/v1/collaboration/invitations/project/{new['invitation_id']}/accept", json={
        "expected_revision": new["revision"],
    })).status_code == 200
    assert (await guest.get(f"/api/v1/tasks/{task_id}", params=task_params((task_id, key)))).status_code == 404
    async with app.state.sessionmaker() as db:
        stored = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == guest_user["id"]))
        assert stored.invitation_id != member["invitation_id"]


async def test_finished_stream_is_authorized_in_http_and_denied_after_revocation(app, client, make_client):
    """E5-f：协作者经生产入口受控回放本人已结束任务；撤权后入口 404。"""
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    task_id = tasks["finished"][0]
    response = await guest.get(f"/api/v1/tasks/{task_id}/stream", params=task_params(tasks["finished"]))
    assert response.status_code == 200 and "自己的完成输出" in response.text
    assert "permission_denied" not in response.text and "event: done" in response.text
    assert (await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}", params={
        "expected_revision": member["revision"],
    })).status_code == 204
    denied = await guest.get(f"/api/v1/tasks/{task_id}/stream", params=task_params(tasks["finished"]))
    assert denied.status_code == 404



async def test_stream_entry_for_viewer_and_admin_member_keeps_finished_replay_only(app, client, make_client):
    """E5-f：降级为 viewer 后本人已结束记录仍可受控回放、活动任务入口 404；兼任管理员不改变协作者判定。
    非成员（撤权后）与他人任务的入口响应一致（404 同一 detail）。"""
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    base = f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}"
    changed = await client.patch(base, json={"expected_revision": member["revision"], "role": "viewer"})
    assert changed.status_code == 200, changed.text

    async def stream(name):
        return await asyncio.wait_for(guest.get(f"/api/v1/tasks/{tasks[name][0]}/stream",
                                                params=task_params(tasks[name])), 10)

    for admin in (False, True):
        if admin:
            async with app.state.sessionmaker() as db:
                (await db.get(User, guest_user["id"])).is_admin = True
                await db.commit()
        finished = await stream("finished")
        assert finished.status_code == 200, finished.text
        assert "自己的完成输出" in finished.text and "event: done" in finished.text
        assert "permission_denied" not in finished.text
        active = await stream("valid")
        assert active.status_code == 404 and "自己的活动输出" not in active.text
    owner_task = await stream("owner")
    assert owner_task.status_code == 404
    assert (await client.delete(base, params={"expected_revision": changed.json()["revision"]})).status_code == 204
    revoked = await stream("finished")
    assert revoked.status_code == 404 and revoked.json() == owner_task.json()


def test_task_grant_binding_overwrites_untrusted_reserved_key():
    invitation = str(uuid4())
    malicious = {"request": {"ai_config_id": 2}, "_collaboration_grant": {
        "invitation_id": str(uuid4()), "revision": 999,
    }}
    saved = bind_task_grant(malicious, (invitation, 3))
    assert task_grant_identity(saved) == (invitation, 3) and malicious["_collaboration_grant"]["revision"] == 999
    assert "_collaboration_grant" not in bind_task_grant(malicious, None)
    for invalid in (True, "3", 0, -1, 1.5):
        assert task_grant_identity({"_collaboration_grant": {"invitation_id": invitation, "revision": invalid}}) is None
    with pytest.raises(ValueError):
        bind_task_grant({}, (invitation, True))


async def test_noncanonical_stored_invitation_cannot_authorize_active_task(app, client, make_client):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    async with app.state.sessionmaker() as db:
        collab = await db.scalar(select(ProjectCollaborator).where(ProjectCollaborator.user_id == guest_user["id"]))
        collab.invitation_id = "------------------------------------"
        task = await db.get(Task, tasks["valid"][0])
        task.input = {"_collaboration_grant": {"invitation_id": collab.invitation_id, "revision": collab.revision}}
        await db.commit()
    denied = await guest.get(f"/api/v1/tasks/{tasks['valid'][0]}", params=task_params(tasks["valid"]))
    assert denied.status_code == 404


async def test_http_task_read_rechecks_original_token_version(app, client, make_client, monkeypatch):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    reached, release = asyncio.Event(), asyncio.Event()
    original = task_service.get_owned_task

    async def blocked(db, user_id, task_id, **kwargs):
        reached.set()
        await release.wait()
        return await original(db, user_id, task_id, **kwargs)

    monkeypatch.setattr(task_service, "get_owned_task", blocked)
    request = asyncio.create_task(guest.get(
        f"/api/v1/tasks/{tasks['finished'][0]}/output", params=task_params(tasks["finished"]),
    ))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        async with app.state.sessionmaker() as db:
            (await db.get(User, guest_user["id"])).token_version += 1
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 401 and "自己的完成输出" not in response.text
        denied = await guest.get(f"/api/v1/tasks/{tasks['finished'][0]}", params=task_params(tasks["finished"]))
        assert denied.status_code == 401
    finally:
        release.set()
        if not request.done():
            request.cancel()


async def test_task_denial_does_not_flush_unrelated_pending_writes(app, client, make_client):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = AuthenticatedActor(guest_user["id"], 999)
        pending = Chapter(project_id=project["id"], title="不得提前写入")
        db.add(pending)
        flushes = []
        event.listen(db.sync_session, "before_flush", lambda *_: flushes.append(True))
        with pytest.raises(UnauthorizedError):
            await task_service.get_owned_task(db, guest_user["id"], tasks["finished"][0])
        db.info["authenticated_actor"] = AuthenticatedActor(guest_user["id"], 0)
        with pytest.raises(NotFoundError):
            await task_service.get_owned_task(db, guest_user["id"], tasks["owner"][0])
        assert not flushes and pending.id is None
        await db.rollback()


async def test_task_outputs_and_stream_redact_other_private_references(app, client, make_client):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    config = await create_ai_config(client)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    task_id, key = tasks["valid"]
    async with app.state.sessionmaker() as db:
        log = GenerationLog(project_id=project["id"], actor_user_id=owner_user["id"], kind="generate", status="success")
        db.add(log)
        await db.flush()
        private_log_id = log.id
        task = await db.get(Task, task_id)
        task.checkpoint = {"nested": {"ai_config_id": config["id"], "task_id": tasks["owner"][0],
                                      "generation_log_id": private_log_id,
                                      "_project_model": {"config_id": config["id"]},
                                      "content": json.dumps({"_collaboration_grant": {"revision": 2}})}}
        await db.commit()
    response = await guest.get(f"/api/v1/tasks/{task_id}", params=task_params((task_id, key)))
    assert response.status_code == 200, response.text
    nested = response.json()["checkpoint"]["nested"]
    assert nested["ai_config_id"] is nested["task_id"] is nested["generation_log_id"] is None
    assert "_project_model" not in nested and "_collaboration_grant" not in nested["content"]
    listing = await guest.get("/api/v1/tasks")
    listed = next(row for row in listing.json() if row["id"] == task_id)
    assert listed["checkpoint"]["nested"] == nested
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task_id, statistics_key=key, status="running", text="")
    stream = engine.subscribe(task_id, statistics_key=key, actor_id=guest_user["id"], token_version=0,
                              active_grant_required=True)
    assert (await anext(stream))[0] == "snapshot"
    hub.publish("custom", {"result": {"ai_config_id": config["id"], "task_id": tasks["owner"][0],
                                      "log_id": private_log_id, "_project_model": {"config_id": config["id"]}}})
    event_name, payload = await anext(stream)
    assert event_name == "custom"
    assert payload["result"] == {"ai_config_id": None, "task_id": None, "log_id": None}
    await stream.aclose()


async def test_viewer_can_delete_own_finished_and_system_prunes_revoked_history(app, client, make_client, monkeypatch):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    base = f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}"
    changed = await client.patch(base, json={"expected_revision": 2, "role": "viewer"})
    assert changed.status_code == 200
    deleted = await guest.delete(f"/api/v1/tasks/{tasks['finished'][0]}", params=task_params(tasks["finished"]))
    assert deleted.status_code == 204
    async with app.state.sessionmaker() as db:
        old = Task(user_id=guest_user["id"], project_id=project["id"], kind="chapter.generate", status="succeeded")
        newer = Task(user_id=guest_user["id"], project_id=project["id"], kind="chapter.generate", status="succeeded")
        db.add_all([old, newer])
        await db.commit()
        old_id, old_key, newer_id = old.id, old.statistics_key, newer.id
    assert (await client.delete(base, params={"expected_revision": 3})).status_code == 204
    assert (await guest.get(f"/api/v1/tasks/{old_id}", params=task_params((old_id, old_key)))).status_code == 404
    monkeypatch.setattr(task_service, "MAX_FINISHED_PER_USER", 1)
    async with app.state.sessionmaker() as db:
        await task_service.prune_finished(db, guest_user["id"])
        assert await db.get(Task, old_id) is None
        assert await db.get(Task, newer_id) is not None


@pytest.mark.parametrize("endpoint", ["detail", "list", "output"])
async def test_task_http_rechecks_after_projection_downgrade(app, client, make_client, monkeypatch, endpoint):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    task_id = tasks["valid"][0]
    reached, release = asyncio.Event(), asyncio.Event()
    if endpoint == "output":
        original_check = task_service._require_visible_task_result

        async def blocked_check(*args, **kwargs):
            reached.set()
            await release.wait()
            return await original_check(*args, **kwargs)

        monkeypatch.setattr(task_service, "_require_visible_task_result", blocked_check)
    else:
        original_projection = personal_projection.project_payload

        async def blocked_projection(*args, **kwargs):
            projected = await original_projection(*args, **kwargs)
            reached.set()
            await release.wait()
            return projected

        monkeypatch.setattr(personal_projection, "project_payload", blocked_projection)
    path = {"detail": f"/api/v1/tasks/{task_id}", "list": "/api/v1/tasks",
            "output": f"/api/v1/tasks/{task_id}/output"}[endpoint]
    params = task_params(tasks["valid"]) if endpoint != "list" else None
    request = asyncio.create_task(guest.get(path, params=params))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        changed = await client.patch(f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}", json={
            "expected_revision": member["revision"], "role": "viewer",
        })
        assert changed.status_code == 200, changed.text
        release.set()
        response = await asyncio.wait_for(request, 5)
        if endpoint == "list":
            assert response.status_code == 200 and task_id not in [row["id"] for row in response.json()]
        else:
            assert response.status_code == 404
        assert "自己的活动输出" not in response.text
    finally:
        release.set()
        if not request.done():
            request.cancel()


async def test_stream_rechecks_after_projection_downgrade(app, client, make_client, monkeypatch):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    task_id, key = tasks["valid"]
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task_id, statistics_key=key, status="running")
    stream = engine.subscribe(task_id, statistics_key=key, actor_id=guest_user["id"], token_version=0,
                              active_grant_required=True)
    assert (await anext(stream))[0] == "snapshot"
    reached, release = asyncio.Event(), asyncio.Event()
    original = personal_projection.project_payload

    async def blocked(*args, **kwargs):
        result = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(personal_projection, "project_payload", blocked)
    hub.append("降级前已入队但尚未发送的内容")
    pending = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        changed = await client.patch(f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}", json={
            "expected_revision": member["revision"], "role": "viewer",
        })
        assert changed.status_code == 200
        release.set()
        event_name, payload = await asyncio.wait_for(pending, 5)
        assert event_name == "error" and payload["code"] == "permission_denied"
        assert "降级前已入队" not in str(payload)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await stream.aclose()


async def test_revoke_notification_wakes_stream_and_database_is_authoritative(app, client, make_client):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    task_id, key = tasks["valid"]
    engine = app.state.task_engine
    engine.heartbeat_seconds = 60
    engine.ensure_hub(task_id, statistics_key=key, status="running")
    stream = engine.subscribe(task_id, statistics_key=key, actor_id=guest_user["id"], token_version=0,
                              active_grant_required=True)
    assert (await anext(stream))[0] == "snapshot"
    pending = asyncio.create_task(anext(stream))
    try:
        revoked = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204
        engine.notify_collaboration_change(project["id"], guest_user["id"])
        event_name, payload = await asyncio.wait_for(pending, 5)
        assert event_name == "error" and payload["code"] == "permission_denied"
    finally:
        if not pending.done():
            pending.cancel()
        await stream.aclose()


async def test_stream_storage_error_is_a_safe_failure(app, client, make_client, monkeypatch):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner_user["id"], guest_user["id"], project["id"],
                         member["invitation_id"], member["revision"])
    task_id, key = tasks["valid"]
    engine = app.state.task_engine
    hub, _ = engine.ensure_hub(task_id, statistics_key=key, status="running")
    stream = engine.subscribe(task_id, statistics_key=key, actor_id=guest_user["id"], token_version=0,
                              active_grant_required=True)
    assert (await anext(stream))[0] == "snapshot"

    async def broken(*_args, **_kwargs):
        raise RuntimeError("数据库私密故障详情")

    monkeypatch.setattr(engine, "_visible_task", broken)
    hub.append("不可输出")
    event_name, payload = await anext(stream)
    assert event_name == "error" and payload["code"] == "permission_denied"
    assert "数据库私密故障详情" not in str(payload) and "不可输出" not in str(payload)
    await stream.aclose()


async def test_system_can_cancel_revoked_collaborator_task(app, client, make_client):
    guest = make_client()
    owner_user, guest_user, project, member = await _setup(app, client, guest)
    async with app.state.sessionmaker() as db:
        # 排队章节任务缺原 _chapter_claim 时取消按设计失败关闭；本用例只验证系统收尾不依赖旧授权，用无资源占用的类型。
        task = Task(user_id=guest_user["id"], project_id=project["id"], kind="outline.continue", status="queued",
                    input={"_collaboration_grant": {"invitation_id": member["invitation_id"], "revision": 2}})
        db.add(task)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key
    revoked = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{guest_user['id']}", params={
        "expected_revision": member["revision"],
    })
    assert revoked.status_code == 204

    async with app.state.sessionmaker() as db:
        assert await task_service.stop_project_tasks(db, app.state.task_engine, project["id"]) == 1
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).status == "cancelled"
    assert (await guest.get(f"/api/v1/tasks/{task_id}", params=task_params((task_id, task_key)))).status_code == 404
