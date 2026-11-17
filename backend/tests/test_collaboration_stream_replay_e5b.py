"""E5-b：受控任务流对已结束 / 待审核任务的持久回放帧证明。

受控管线（engine.subscribe(guarded=True) + task_sse_response）在实际 ASGI send 的同一租约内，
按订阅原绑定重算 `_finished_events` 的公开投影并比较摘要；不依赖执行体或发送时 SendGuard。

生产路由自 E5-f 起对协作者挂载同一受控管线（tasks.py），此处 /_test 路由便于插入持帧点，作者生产订阅路径不变。
"""

from uuid import uuid4

import pytest
from fastapi import Request
from sqlalchemy import update

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.models import SceneReviewProposal, Task, User
from app.services import task_service
from app.services.ai_dispatch import read_visibility_stamp
from app.services.task_engine import stream_evidence
from app.services.task_engine.engine import TASK_ACCESS_LOST_MESSAGE
from app.services.task_engine.stream_lease import _capture_stream_binding, task_sse_response
from tests.helpers import parse_sse, register, task_params
from tests.test_collaboration_paused_cleanup_e5a import _drain_checks
from tests.test_collaboration_review_read import make_review
from tests.test_collaboration_task_privacy import _setup
from tests.test_task_sse_scope import _binding

DENIED = {"task": None, "detail": TASK_ACCESS_LOST_MESSAGE, "code": "permission_denied"}
COLLAB_FAILED = "协作授权已失效，任务已停止"


async def _replay_endpoint(
    request: Request, task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep,
):
    task = await task_service.get_owned_task(db, user.id, task_id, expected_task_key=task_key)
    binding = await _capture_stream_binding(db, task, db.info["authenticated_actor"])
    await db.close()
    return task_sse_response(_guarded(engine, binding), sessionmaker=request.app.state.sessionmaker, binding=binding)


def _mount(app):
    """与生产订阅入口同样捕获原绑定，但一律走受控管线。"""
    app.add_api_route("/_test/tasks/{task_id}/stream", _replay_endpoint, methods=["GET"])


def _guarded(engine, binding):
    return engine.subscribe(
        binding.task_id, statistics_key=binding.task_key, actor_id=binding.actor.id,
        token_version=binding.actor.token_version, active_grant_required=binding.active_grant_required,
        initial_project_id=binding.project_id, initial_queued_at=binding.queued_at,
        initial_grant=binding.task_grant, guarded=True,
    )


async def _stream(client, identity):
    return await client.get(f"/_test/tasks/{identity[0]}/stream", params=task_params(identity))


async def _send(app, binding, items):
    """以真实中央校验器发送给定帧；返回解析后的事件。"""

    async def events():
        for item in items:
            yield item

    sent = []

    async def send(message):
        sent.append(message)

    await task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding).stream_response(send)
    return parse_sse(b"".join(item.get("body", b"") for item in sent).decode())


async def _collect(app, binding):
    return [item async for item in _guarded(app.state.task_engine, binding)]


# 结果与正文含私人引用及类 JSON 文本，回放必须与详情 / 输出接口经同一投影。
RESULT = {"chapter": {"id": 1, "title": "回放章"}, "log_id": 987654, "note": "仅本人可见的结果"}
OUTPUT = '{"chapter": {"title": "回放章"}, "text": "回放正文"}'


async def _finished(app, user_id, project_id, *, status="succeeded", title="finished", grant=None):
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user_id, project_id=project_id, kind="chapter.generate", status=status, title=title,
                    output_text=OUTPUT, result=RESULT if status == "succeeded" else None,
                    error="" if status == "succeeded" else "回放失败原因",
                    input={"_collaboration_grant": grant} if grant else {})
        db.add(task)
        await db.commit()
        return task.id, task.statistics_key


async def _assert_matches_detail(client, identity, response):
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    detail = await client.get(f"/api/v1/tasks/{identity[0]}", params=task_params(identity))
    output = await client.get(f"/api/v1/tasks/{identity[0]}/output", params=task_params(identity))
    assert detail.status_code == 200 and output.status_code == 200
    assert [event for event, _ in events][0] == "snapshot"
    assert events[0][1] == {"task": detail.json(), "text": output.json()["text"]}
    assert events[-1][1]["task"] == detail.json()
    return events


async def test_guarded_replay_owner_editor_and_downgraded_viewer_match_detail(app, client, make_client):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    _mount(app)
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    own = await _finished(app, owner["id"], project["id"], title="owner")
    done = await _finished(app, editor["id"], project["id"], grant=grant)
    failed = await _finished(app, editor["id"], project["id"], status="failed", title="failed", grant=grant)

    events = await _assert_matches_detail(client, own, await _stream(client, own))
    assert events[-1][0] == "done" and events[-1][1]["result"]["note"] == RESULT["note"]
    assert events[-1][1]["result"]["log_id"] is None
    events = await _assert_matches_detail(guest, done, await _stream(guest, done))
    assert [name for name, _ in events] == ["snapshot", "done"]
    assert events[-1][1]["result"]["note"] == RESULT["note"]
    events = await _assert_matches_detail(guest, failed, await _stream(guest, failed))
    assert events[-1] == ("error", {"task": events[0][1]["task"], "detail": "回放失败原因"})

    member_path = f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}"
    changed = await client.patch(member_path, json={"expected_revision": member["revision"], "role": "viewer"})
    assert changed.status_code == 200, changed.text
    for identity in (done, failed):
        await _assert_matches_detail(guest, identity, await _stream(guest, identity))
    # 作者看不到编辑者的任务，编辑者也看不到作者的任务。
    assert (await _stream(client, done)).status_code == 404
    assert (await _stream(guest, own)).status_code == 404


@pytest.mark.parametrize("change", ["revoke", "reinvite", "admin_only", "token"])
async def test_replay_frames_produced_before_change_are_denied_at_send(app, client, make_client, change):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    _mount(app)
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    identity = await _finished(app, editor["id"], project["id"], grant=grant)
    binding = await _binding(app, editor["id"], *identity)
    items = await _collect(app, binding)
    assert [item[0] for item in items] == ["snapshot", "done"]

    base = f"/api/v1/projects/{project['id']}"
    if change == "token":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == editor["id"]).values(
                token_version=User.token_version + 1,
            ))
            await db.commit()
    else:
        revoked = await client.delete(f"{base}/collaborators/{editor['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204, revoked.text
    if change == "reinvite":
        invitation = (await client.post(f"{base}/collaborators", json={
            "username": "task-guest", "role": "editor",
        })).json()
        assert (await guest.post(f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
                                 json={"expected_revision": invitation["revision"]})).status_code == 200
    elif change == "admin_only":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == editor["id"]).values(is_admin=True))
            await db.commit()

    # 引擎在变化前已产出带证明的帧，发送时的租约仍须拒绝。
    events = await _send(app, binding, items)
    assert events == [("error", DENIED)]
    if change == "reinvite":
        # 新轮次的新订阅按 accepted 成员读取本人已结束记录。
        await _assert_matches_detail(guest, identity, await _stream(guest, identity))
    elif change != "token":
        assert (await _stream(guest, identity)).status_code == 404


@pytest.mark.parametrize("forgery", ["view", "result", "output", "event", "status", "positive"])
async def test_forged_or_tampered_replay_frames_are_denied(app, client, make_client, forgery):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    identity = await _finished(app, editor["id"], project["id"], grant=grant)
    binding = await _binding(app, editor["id"], *identity)
    items = await _collect(app, binding)
    (snap_event, snap, snap_evidence), (end_event, end, end_evidence) = items
    source, stamp = snap_evidence.source, snap_evidence.visibility
    if forgery == "positive":
        # 以同一持锁事实重新签发的未篡改帧可以通过，证明下列拒绝来自内容比较。
        frames = [(snap_event, snap, stream_evidence.frame_evidence(snap_event, snap, source, stamp)),
                  (end_event, end, stream_evidence.frame_evidence(end_event, end, source, stamp))]
        events = await _send(app, binding, frames)
        assert [event for event, _ in events] == ["snapshot", "done"]
        return
    if forgery == "view":
        forged = stream_evidence.TaskViewEvidence(source.task_id, source.task_key, source.queued_at, source.status,
                                                  "0" * 64)
        frames = [(snap_event, snap, stream_evidence.frame_evidence(snap_event, snap, forged, stamp))]
    elif forgery == "result":
        data = {**end, "result": {**end["result"], "note": "伪造结果"}}
        frames = [(end_event, data, stream_evidence.frame_evidence(end_event, data, source, stamp))]
    elif forgery == "output":
        data = {**snap, "text": "伪造正文"}
        frames = [(snap_event, data, stream_evidence.frame_evidence(snap_event, data, source, stamp))]
    elif forgery == "event":
        # 成功任务的结果不能改名为待审核或失败帧发送。
        frames = [("paused", end, stream_evidence.frame_evidence("paused", end, source, stamp))]
    else:
        data = {**end, "task": {**end["task"], "status": "failed"}}
        frames = [(end_event, data, stream_evidence.frame_evidence(end_event, data, source, stamp))]
    events = await _send(app, binding, frames)
    assert events == [("error", DENIED)]


async def test_replay_after_row_changes_is_denied(app, client, make_client):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    identity = await _finished(app, editor["id"], project["id"], grant=grant)
    binding = await _binding(app, editor["id"], *identity)
    items = await _collect(app, binding)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == identity[0]).values(output_text="之后被改写"))
        await db.commit()
    assert await _send(app, binding, items) == [("error", DENIED)]


async def _paused_review(app, client, guest):
    author, user, project, member, identity = await make_review(app, client, guest)
    review = {"reason": "rich_document_review", "token": identity["token"], "task_key": identity["task_key"],
              "run_key": identity["run_key"]}
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        review["chapter_id"] = task.chapter_id
        task.result = review
        task.checkpoint = {"rich_review": {"phase": "awaiting_review", "token": identity["token"],
                                           "run_key": identity["run_key"], "chapter_id": task.chapter_id}}
        task.output_text = "待审核输出"
        await db.commit()
    return author, user, project, member, identity, review


async def test_paused_rich_review_replay_requires_joint_recheck(app, client, make_client):
    guest = make_client()
    _, user, project, member, identity, review = await _paused_review(app, client, guest)
    _mount(app)
    key = (identity["task_id"], identity["task_key"])
    response = await _stream(guest, key)
    events = await _assert_matches_detail(guest, key, response)
    assert [event for event, _ in events] == ["snapshot", "paused"]
    assert events[-1][1]["result"] == review
    assert events[0][1]["task"]["rich_review"]["token"] == identity["token"]

    # 帧已产出、提案在发送前失效：联表复核失败，不发出提案凭据。
    binding = await _binding(app, user["id"], *key)
    items = await _collect(app, binding)
    async with app.state.sessionmaker() as db:
        await db.execute(update(SceneReviewProposal).where(SceneReviewProposal.id == identity["proposal_id"]).values(
            status="discarded",
        ))
        await db.commit()
    events = await _send(app, binding, items[1:])
    assert events == [("error", DENIED)]
    # 快照帧不含提案引用联表，单独仍按持锁行通过。
    assert [event for event, _ in await _send(app, binding, items[:1])] == ["snapshot"]


async def test_paused_review_forged_token_is_denied(app, client, make_client):
    guest = make_client()
    _, user, project, member, identity, review = await _paused_review(app, client, guest)
    key = (identity["task_id"], identity["task_key"])
    binding = await _binding(app, user["id"], *key)
    (_, snap, evidence), (end_event, end, _) = await _collect(app, binding)
    data = {**end, "result": {**end["result"], "token": str(uuid4())}}
    frames = [(end_event, data, stream_evidence.frame_evidence(end_event, data, evidence.source,
                                                               evidence.visibility))]
    assert await _send(app, binding, frames) == [("error", DENIED)]


async def test_paused_cleaned_by_system_replays_terminal_state(app, client, make_client):
    guest = make_client()
    _, user, project, member, identity, review = await _paused_review(app, client, guest)
    _mount(app)
    key = (identity["task_id"], identity["task_key"])
    paused_binding = await _binding(app, user["id"], *key)
    assert paused_binding.active_grant_required
    paused_items = await _collect(app, paused_binding)
    engine = app.state.task_engine
    await engine.start()
    engine.collaboration_scan_interval = 3600
    await _drain_checks(engine)
    changed = await client.patch(f"/api/v1/projects/{project['id']}/collaborators/{user['id']}", json={
        "expected_revision": member["revision"], "role": "viewer",
    })
    assert changed.status_code == 200, changed.text
    # 降级通知触发 E5-a 系统收尾（数据库为准）。
    await _drain_checks(engine)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, identity["task_id"])
        assert (row.status, row.error) == ("failed", COLLAB_FAILED)

    # 待审核时订阅的旧流按活动轮次绑定，降级后不能再发出其帧（即使是快照）。
    assert await _send(app, paused_binding, paused_items) == [("error", DENIED)]
    # 降级后的本人重新订阅：回放系统收尾后的终态，不含提案凭据。
    response = await _stream(guest, key)
    events = await _assert_matches_detail(guest, key, response)
    assert [event for event, _ in events] == ["snapshot", "error"]
    assert events[-1][1]["task"]["status"] == "failed" and events[-1][1]["detail"] == COLLAB_FAILED
    # 终态帧不含 paused 结果；提案凭据（若仍随任务公开投影出现）只能是与详情一致的非待审核阶段。
    assert "result" not in events[-1][1] and "awaiting_review" not in response.text
    review_out = events[-1][1]["task"]["rich_review"]
    assert review_out is None or review_out["phase"] == "discarded"


async def test_personal_task_replay_and_tamper(app, client):
    user = await register(client, "personal-replay")
    _mount(app)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=None, kind="book.import", status="succeeded",
                    output_text="本人回放内容", result={"summary": "本人结果"})
        db.add(task)
        await db.commit()
        identity = (task.id, task.statistics_key)
    response = await _stream(client, identity)
    events = await _assert_matches_detail(client, identity, response)
    assert events[-1] == ("done", {"task": events[0][1]["task"], "result": {"summary": "本人结果"}})

    binding = await _binding(app, user["id"], *identity)
    items = await _collect(app, binding)
    assert items[0][2].visibility is None
    assert [event for event, _ in await _send(app, binding, items)] == ["snapshot", "done"]
    _, end, evidence = items[1]
    data = {**end, "result": {"summary": "伪造"}}
    frames = [("done", data, stream_evidence.frame_evidence("done", data, evidence.source, None))]
    assert await _send(app, binding, frames) == [("error", DENIED)]
    # 个人任务不能带作品可见性戳冒充作品帧。
    async with app.state.sessionmaker() as db:
        other = await db.scalar(update(Task).where(Task.id == identity[0]).values(title="改名").returning(Task.id))
        await db.commit()
    assert other == identity[0]
    assert await _send(app, binding, items) == [("error", DENIED)]


async def test_visibility_stamp_change_after_projection_denies_project_replay(app, client, make_client):
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    identity = await _finished(app, editor["id"], project["id"], grant=grant)
    binding = await _binding(app, editor["id"], *identity)
    items = await _collect(app, binding)
    async with app.state.sessionmaker() as db:
        before = await read_visibility_stamp(db, project["id"])
    assert items[0][2].visibility == before
    from app.models import Project

    async with app.state.sessionmaker() as db:
        await db.execute(update(Project).where(Project.id == project["id"]).values(
            ai_visibility_revision=Project.ai_visibility_revision + 1,
        ))
        await db.commit()
    assert await _send(app, binding, items) == [("error", DENIED)]


@pytest.mark.parametrize("change", ["none", "revoke", "reinvite", "admin_only", "token"])
async def test_frame_lease_alone_denies_replay_after_change(app, client, make_client, monkeypatch, change):
    """单层用例：中央帧证明一律放行时，只靠租约内认证、成员原轮次与任务可见性复查也须拒绝。"""
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    identity = await _finished(app, editor["id"], project["id"], grant=grant)
    binding = await _binding(app, editor["id"], *identity)
    items = await _collect(app, binding)
    assert [item[0] for item in items] == ["snapshot", "done"]
    calls = []

    async def accept_all(*args, **kwargs):
        calls.append(args[3])
        return True

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", accept_all)
    base = f"/api/v1/projects/{project['id']}"
    if change == "token":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == editor["id"]).values(token_version=User.token_version + 1))
            await db.commit()
    elif change != "none":
        revoked = await client.delete(f"{base}/collaborators/{editor['id']}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204, revoked.text
    if change == "reinvite":
        invitation = (await client.post(f"{base}/collaborators", json={
            "username": "task-guest", "role": "editor",
        })).json()
        assert (await guest.post(f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
                                 json={"expected_revision": invitation["revision"]})).status_code == 200
    elif change == "admin_only":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == editor["id"]).values(is_admin=True))
            await db.commit()

    events = await _send(app, binding, items)
    if change == "none":
        # 正对照：租约成立时放行，证明下列拒绝来自租约本身。
        assert [event for event, _ in events] == ["snapshot", "done"] and calls == ["snapshot", "done"]
    else:
        assert events == [("error", DENIED)] and calls == []


async def test_personal_replay_rejects_project_stamp_and_other_users(app, client, make_client):
    user = await register(client, "personal-stamp")
    other = make_client()
    await register(other, "personal-other")
    _mount(app)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=None, kind="book.import", status="failed",
                    output_text="本人失败输出", error="本人失败原因")
        db.add(task)
        await db.commit()
        identity = (task.id, task.statistics_key)
    events = await _assert_matches_detail(client, identity, await _stream(client, identity))
    assert events[-1] == ("error", {"task": events[0][1]["task"], "detail": "本人失败原因"})
    # 他人的个人任务：订阅入口即 404，不经任何投影。
    assert (await _stream(other, identity)).status_code == 404

    binding = await _binding(app, user["id"], *identity)
    items = await _collect(app, binding)
    assert all(item[2].visibility is None for item in items)
    # 同一未篡改帧附带任意作品可见性戳冒充作品帧 → 拒绝。
    stamp = stream_evidence.VisibilityStamp(1, 0, 0, None)
    frames = [(event, data, stream_evidence.frame_evidence(event, data, evidence.source, stamp))
              for event, data, evidence in items]
    assert await _send(app, binding, frames) == [("error", DENIED)]
    assert [event for event, _ in await _send(app, binding, items)] == ["snapshot", "error"]


async def test_stream_binding_only_inside_real_frame_send(app, client, make_client, monkeypatch):
    from app.services.task_engine import stream_lease

    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    grant = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
    identity = await _finished(app, editor["id"], project["id"], grant=grant)
    binding = await _binding(app, editor["id"], *identity)
    args = (editor["id"], project["id"])
    kwargs = {"task_id": identity[0], "task_key": identity[1]}
    # 裸锁 scope 没有发送登记，取不到原绑定。
    async with stream_lease._task_frame_lease(app.state.sessionmaker, binding, "snapshot", {}) as (db, _, _p):
        with pytest.raises(stream_lease.TaskStreamLeaseError):
            await stream_lease.task_stream_binding(db, *args, **kwargs)

    seen = []
    original = stream_evidence.verify_stream_frame

    async def capture(db, task, proj, event, data, evidence):
        seen.append((db, await stream_lease.task_stream_binding(db, *args, **kwargs)))
        # 其他任务 / 其他作品身份不能借本帧租约取得绑定。
        with pytest.raises(stream_lease.TaskStreamLeaseError):
            await stream_lease.task_stream_binding(db, editor["id"], project["id"], task_id=identity[0] + 1,
                                                   task_key=identity[1])
        return False

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", capture)
    items = await _collect(app, binding)
    assert await _send(app, binding, items[:1]) == [("error", DENIED)]
    assert len(seen) == 1 and seen[0][1] is binding
    # 帧结束后登记已清除，同一会话不能跨帧复用。
    assert seen[0][0] not in stream_lease._LIVE_STREAM_LEASES
    with pytest.raises(stream_lease.TaskStreamLeaseError):
        await stream_lease.task_stream_binding(seen[0][0], *args, **kwargs)

    seen.clear()

    async def forged(db, task, proj, event, data, evidence):
        # 篡改登记中的绑定（换成另一个订阅绑定实例）后同一租约即失效。
        state = stream_lease._LIVE_STREAM_LEASES[db]
        state.binding = object()
        with pytest.raises(stream_lease.TaskStreamLeaseError):
            await stream_lease.task_stream_binding(db, *args, **kwargs)
        seen.append(state.valid)
        return await original(db, task, proj, event, data, evidence)

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", forged)
    assert await _send(app, binding, items[:1]) == [("error", DENIED)]
    assert seen == [False]
