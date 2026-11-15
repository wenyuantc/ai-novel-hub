"""任务流单帧授权使用请求原始身份，不能重邀或整数 ID 复用后补签。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text, update

from app.core.errors import NotFoundError, UnauthorizedError
from app.models import Project, ProjectCollaborator, Task, User
from app.services import task_service
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine import stream_evidence
from app.services.task_engine.stream_lease import (
    TaskStreamLeaseError,
    _capture_stream_binding,
    _task_frame_lease,
    require_task_stream_lease,
    require_task_stream_terminal,
    task_sse_response,
)
from tests.helpers import parse_sse, register
from tests.test_collaboration_task_privacy import _setup, _tasks


async def _binding(app, user_id, task_id, task_key):
    actor = AuthenticatedActor(user_id, 0)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        task = await task_service.get_owned_task(db, user_id, task_id, expected_task_key=task_key)
        return await _capture_stream_binding(db, task, actor)


async def _valid(_db, _task, _project, _event, _data, evidence):
    return evidence == "原帧证明"


async def _frame(app, binding, monkeypatch, event, data, evidence, verify):
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)

    async def events():
        yield event, data, evidence

    sent = []

    async def send(message):
        sent.append(message)

    await task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding).stream_response(send)
    return b"".join(item.get("body", b"") for item in sent)


async def test_task_frame_scope_requires_original_member_and_frame_proof(app, client, make_client, monkeypatch):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])
    async with _task_frame_lease(app.state.sessionmaker, binding, "snapshot", {}):
        pass
    body = await _frame(app, binding, monkeypatch, "snapshot", {"text": "私有"}, "错误证明", _valid)
    assert b"permission_denied" in body and "私有".encode() not in body
    assert (await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}", params={
        "expected_revision": member["revision"],
    })).status_code == 204
    with pytest.raises(NotFoundError):
        async with _task_frame_lease(app.state.sessionmaker, binding, "snapshot", {}):
            pass


async def test_stream_lease_proof_is_bound_to_same_actor_task_coroutine_and_lifetime(
    app, client, make_client, monkeypatch,
):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])
    saved = []

    async def verify(db, _task, _project, _event, _data, _evidence):
        saved.append(db)
        await require_task_stream_lease(db, editor["id"], project["id"], task_id=binding.task_id,
                                        task_key=binding.task_key)
        with pytest.raises(TaskStreamLeaseError):
            await require_task_stream_lease(db, editor["id"] + 1, project["id"], task_id=binding.task_id,
                                            task_key=binding.task_key)
        with pytest.raises(TaskStreamLeaseError):
            await require_task_stream_lease(db, True, project["id"], task_id=binding.task_id,
                                            task_key=binding.task_key)
        with pytest.raises(TaskStreamLeaseError):
            await require_task_stream_lease(db, editor["id"], project["id"], task_id=binding.task_id,
                                            task_key=str(uuid4()))
        with pytest.raises(TaskStreamLeaseError):
            await asyncio.create_task(require_task_stream_lease(db, editor["id"], project["id"],
                                      task_id=binding.task_id, task_key=binding.task_key))
        async with app.state.sessionmaker() as other:
            with pytest.raises(TaskStreamLeaseError):
                await require_task_stream_lease(other, editor["id"], project["id"], task_id=binding.task_id,
                                                task_key=binding.task_key)
        return True

    async with _task_frame_lease(app.state.sessionmaker, binding, "delta", {}) as (db, _task, _project):
        with pytest.raises(TaskStreamLeaseError):
            await require_task_stream_lease(db, editor["id"], project["id"], task_id=binding.task_id,
                                            task_key=binding.task_key)
    body = await _frame(app, binding, monkeypatch, "delta", {"text": "原始"}, object(), verify)
    assert "原始".encode() in body
    with pytest.raises(TaskStreamLeaseError):
        await require_task_stream_lease(saved[0], editor["id"], project["id"], task_id=binding.task_id,
                                        task_key=binding.task_key)


async def test_terminal_receipt_requires_exact_original_frame_object(app, client, make_client, monkeypatch):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])
    data = {"text": "排队中的增量"}
    receipt = stream_evidence.TerminalEvidence(execution=None, view=None)
    source = stream_evidence.TaskViewEvidence(binding.task_id, binding.task_key, None, "succeeded", "fingerprint")
    evidence = stream_evidence.frame_evidence("delta", data, source, None, receipt)

    async def verify(db, _task, _project, _event, _data, _evidence):
        await require_task_stream_terminal(db, editor["id"], project["id"], task_id=binding.task_id,
                                           task_key=binding.task_key, terminal=receipt)
        copied = stream_evidence.TerminalEvidence(execution=None, view=None)
        with pytest.raises(TaskStreamLeaseError):
            await require_task_stream_terminal(db, editor["id"], project["id"], task_id=binding.task_id,
                                               task_key=binding.task_key, terminal=copied)
        return True

    body = await _frame(app, binding, monkeypatch, "delta", data, evidence, verify)
    assert "排队中的增量".encode() in body


@pytest.mark.parametrize("change", ["commit", "nested", "connection", "payload"])
async def test_stream_lease_proof_cannot_cross_transaction_or_mutate_frame(
    app, client, make_client, monkeypatch, change,
):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])
    payload = {"text": "原始"}

    async def verify(db, _task, _project, _event, data, _evidence):
        if change == "commit":
            await db.commit()
            await db.execute(text("SELECT 1"))
        elif change == "nested":
            nested = await db.begin_nested()
            await nested.rollback()
        elif change == "connection":
            await db.invalidate()
        else:
            data["text"] = "被替换"
        return True

    body = await _frame(app, binding, monkeypatch, "delta", payload, object(), verify)
    assert b"permission_denied" in body and "原始".encode() not in body and "被替换".encode() not in body


async def test_collaborator_stream_uses_guarded_pipeline_and_closed_kinds_stay_404(app, client, make_client):
    """E5-f：生产订阅入口对协作者走受控管线。

    已结束任务按持久回放证明放行；没有引擎帧发布证明的运行中 hub（引擎未运行、直接插入的行）只得到
    permission_denied，不泄露输出；Agent / 知识库等未开放帧证明的类型即使已结束也在入口 404。
    """
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    finished = await guest.get(f"/api/v1/tasks/{tasks['finished'][0]}/stream",
                               params={"task_key": tasks["finished"][1]})
    assert finished.status_code == 200, finished.text
    events = parse_sse(finished.text)
    assert [event for event, _ in events] == ["snapshot", "done"]
    assert events[0][1]["text"] == "自己的完成输出"
    # 若误走非受控管线，运行中 hub 的流不会结束：限时使回归表现为失败而非挂起。
    unproven = await asyncio.wait_for(guest.get(f"/api/v1/tasks/{tasks['valid'][0]}/stream",
                                                params={"task_key": tasks["valid"][1]}), 10)
    assert unproven.status_code == 200
    assert parse_sse(unproven.text) == [("error", {
        "task": None, "detail": "任务访问权限已失效", "code": "permission_denied",
    })]
    assert "自己的活动输出" not in unproven.text
    async with app.state.sessionmaker() as db:
        closed = [Task(user_id=editor["id"], project_id=project["id"], kind=kind, status="succeeded",
                       output_text="未开放类型的输出", title=kind) for kind in ("agent.turn", "kb.index")]
        db.add_all(closed)
        await db.commit()
        closed = [(row.id, row.statistics_key) for row in closed]
    for task_id, task_key in closed:
        detail = await guest.get(f"/api/v1/tasks/{task_id}", params={"task_key": task_key})
        assert detail.status_code == 200, detail.text
        response = await guest.get(f"/api/v1/tasks/{task_id}/stream", params={"task_key": task_key})
        assert response.status_code == 404 and "未开放类型的输出" not in response.text
    # 作者自己的已结束任务仍走原管线回放（行为不变）。
    async with app.state.sessionmaker() as db:
        own = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="succeeded",
                   output_text="作者的输出", title="owner-agent")
        db.add(own)
        await db.commit()
        own = (own.id, own.statistics_key)
    response = await client.get(f"/api/v1/tasks/{own[0]}/stream", params={"task_key": own[1]})
    assert response.status_code == 200 and "作者的输出" in response.text


@pytest.mark.parametrize("allowed", [True, False])
async def test_active_member_frame_lease_rechecks_owner_ai_directly(app, client, make_client, monkeypatch, allowed):
    """E5-f：活动协作订阅的任务依赖作者模型时，帧租约锁内直接核对当前 allow_owner_ai（不递增成员轮次、无通知）。

    中央帧证明以桩放行，只验证租约本身；已结束任务的回放绑定不受作者模型授权影响（本人可读已结束记录）。"""
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    async with app.state.sessionmaker() as db:
        for name, extra in (("valid", {"_collaboration_grant": grant}), ("finished", {})):
            row = await db.get(Task, tasks[name][0])
            row.input = {**extra, "_project_model": {"source": "owner"}}
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
        ).values(allow_owner_ai=True))
        await db.commit()
    active = await _binding(app, editor["id"], *tasks["valid"])
    finished = await _binding(app, editor["id"], *tasks["finished"])
    assert active.active_grant_required and not finished.active_grant_required
    payload = {"text": "作者模型产出"}
    body = await _frame(app, active, monkeypatch, "delta", dict(payload), "原帧证明", _valid)
    assert "作者模型产出".encode() in body and b"permission_denied" not in body
    if not allowed:
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
            ).values(allow_owner_ai=False))
            await db.commit()
    body = await _frame(app, active, monkeypatch, "delta", dict(payload), "原帧证明", _valid)
    assert (b"permission_denied" in body) is (not allowed)
    assert ("作者模型产出".encode() in body) is allowed
    body = await _frame(app, finished, monkeypatch, "delta", dict(payload), "原帧证明", _valid)
    assert "作者模型产出".encode() in body and b"permission_denied" not in body
    # 只收紧依赖作者模型的任务：活动任务绑定本人模型时，作者模型授权关闭不得误拒。
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, tasks["valid"][0])
        row.input = {"_collaboration_grant": grant, "_project_model": {"source": "actor"}}
        await db.commit()
    body = await _frame(app, active, monkeypatch, "delta", {"text": "本人模型产出"}, "原帧证明", _valid)
    assert "本人模型产出".encode() in body and b"permission_denied" not in body


async def test_personal_completed_task_stream_remains_available(app, client):
    user = await register(client, "personal-stream")
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=None, kind="book.import", status="succeeded", output_text="本人内容")
        db.add(task)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key
    response = await client.get(f"/api/v1/tasks/{task_id}/stream", params={"task_key": task_key})
    assert response.status_code == 200 and "本人内容" in response.text


async def test_sqlite_task_frame_send_and_revocation_are_serialized_over_tcp(app, client, make_client, monkeypatch):
    from tests.test_task_sse_scope_postgresql import _tcp_server

    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])
    entered, release, next_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def events():
        yield "snapshot", {"text": "先发送的内容"}, "原帧证明"
        await next_frame.wait()
        yield "delta", {"text": "撤权后的内容"}, "原帧证明"

    async def endpoint():
        return task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding)

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", _valid)
    app.add_api_route("/_test/task-frame", endpoint, methods=["GET"])
    async with _tcp_server(app, entered, release) as tcp:
        pending = asyncio.create_task(tcp.get("/_test/task-frame"))
        revoke = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            revoke = asyncio.create_task(client.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
                params={"expected_revision": member["revision"]},
            ))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(revoke), 0.15)
            release.set()
            assert (await asyncio.wait_for(revoke, 5)).status_code == 204
            next_frame.set()
            response = await asyncio.wait_for(pending, 5)
            assert "先发送的内容" in response.text
            assert "撤权后的内容" not in response.text and "permission_denied" in response.text
        finally:
            release.set()
            next_frame.set()
            for item in (pending, revoke):
                if item is not None and not item.done():
                    item.cancel()
            await asyncio.gather(*(item for item in (pending, revoke) if item is not None), return_exceptions=True)


async def test_terminal_wait_rechecks_original_grant_after_revoke(app, client, make_client, monkeypatch):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])
    waiting, release, seen = asyncio.Event(), asyncio.Event(), []

    class Pending(Exception):
        async def wait(self):
            waiting.set()
            await release.wait()

    async def verify(*_args):
        seen.append(True)
        if len(seen) == 1:
            raise Pending()
        return True

    monkeypatch.setattr(stream_evidence, "TerminalEvidencePending", Pending, raising=False)
    pending = asyncio.create_task(_frame(
        app, binding, monkeypatch, "delta", {"text": "撤权后旧正文"}, object(), verify,
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        revoked = await client.delete(f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204
        release.set()
        body = await asyncio.wait_for(pending, 5)
        assert b"permission_denied" in body and "撤权后旧正文".encode() not in body
        assert len(seen) == 1
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_finished_task_stream_does_not_adopt_later_invitation(app, client, make_client):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["finished"])
    base = f"/api/v1/projects/{project['id']}"
    assert (await client.delete(f"{base}/collaborators/{editor['id']}", params={
        "expected_revision": member["revision"],
    })).status_code == 204
    invitation = (await client.post(f"{base}/collaborators", json={"username": "task-guest", "role": "editor"})).json()
    assert (await guest.post(f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept", json={
        "expected_revision": invitation["revision"],
    })).status_code == 200
    # 新邀请可访问本人的已完成任务，但旧流不可沿用新邀请发送旧帧。
    response = await guest.get(f"/api/v1/tasks/{binding.task_id}", params={"task_key": binding.task_key})
    assert response.status_code == 200
    with pytest.raises(NotFoundError):
        async with _task_frame_lease(app.state.sessionmaker, binding, "done", {}):
            pass


@pytest.mark.parametrize("change", ["login", "queue", "reused_id", "project_lifecycle"])
async def test_task_frame_scope_rejects_relogin_round_or_reused_id(app, client, make_client, change):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, binding.task_id)
        if change == "login":
            user = await db.get(User, editor["id"])
            user.token_version += 1
        elif change == "queue":
            task.queued_at += timedelta(seconds=1)
        elif change == "reused_id":
            task.statistics_key = str(uuid4())
        else:
            (await db.get(Project, project["id"])).last_deleted_at = datetime.now(UTC)
        await db.commit()
    with pytest.raises((NotFoundError, UnauthorizedError)):
        async with _task_frame_lease(app.state.sessionmaker, binding, "delta", {}):
            pass
