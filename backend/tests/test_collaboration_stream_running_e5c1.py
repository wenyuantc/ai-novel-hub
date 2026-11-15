"""E5-c1：受控任务流对章节（chapter.generate/polish/revise）与分析（chapter.analyze）运行中帧的发布身份证明。

真实引擎执行（fake_llm + Gate），经受控 `/_test` 路由（engine.subscribe(guarded=True) + task_sse_response）
与本机 TCP 实际发送；不替换中央帧校验器。
生产路由自 E5-f 起对协作者挂载同一受控管线（tasks.py），此处 /_test 路由便于插入持帧点，作者生产订阅路径不变。

撤权类矩阵采用「引擎已产出带证明的帧、发送前改变授权」：持住一帧再改变，证明拒绝来自发送时的帧租约与
`SendGuard.require_task_read` 的锁内只读核对，而不是运行体自身停止（无通知时运行体仍阻塞在 Gate、任务仍 running）。
"""

import asyncio
import socket
from contextlib import aclosing, asynccontextmanager
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import select, update

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.core.errors import ConflictError
from app.models import Chapter, SceneReviewProposal, Task
from app.services import task_service
from app.services.ai_dispatch import read_visibility_stamp
from app.services.collaboration_ai_guard import publication_execution_matches
from app.services.task_engine import execution_identity, stream_evidence
from app.services.task_engine.stream_lease import _capture_stream_binding, task_sse_response
from tests.helpers import Gate, analysis_json, create_ai_config, parse_sse, register, wait_until
from tests.test_collaboration_chapter_entry_e4f import _change, _world
from tests.test_collaboration_stream_replay_e5b import DENIED, _guarded, _send
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scenes import setup_scene
from tests.test_task_sse_scope import _binding

PASSWORD, NEW_PASSWORD = "secret123", "secret456new"


@pytest.fixture
def settings_overrides():
    # 章节成功后的自动分析会另起模型调用并改变 Task 终态视图之外的状态；本文件按帧断言，关闭自动分析。
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


class _Hold:
    """在引擎已产出（运行体已核验并发布）、但响应尚未发送某帧时停住。"""

    def __init__(self, predicate):
        self.predicate = predicate
        self.reached, self.proceed = asyncio.Event(), asyncio.Event()
        self.item = None


def _mount(app, holds: list[_Hold] | None = None):
    """与生产订阅入口同样以真实认证捕获原绑定，但一律走受控管线。"""
    holds = holds if holds is not None else []

    async def endpoint(task_id: int, task_key: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep):
        task = await task_service.get_owned_task(db, user.id, task_id, expected_task_key=task_key)
        binding = await _capture_stream_binding(db, task, db.info["authenticated_actor"])
        await db.close()

        async def events():
            async with aclosing(_guarded(engine, binding)) as source:
                async for item in source:
                    for hold in holds:
                        if not hold.reached.is_set() and hold.predicate(item):
                            hold.item = item
                            hold.reached.set()
                            await hold.proceed.wait()
                            break
                    yield item

        return task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding)

    app.add_api_route("/_test/tasks/{task_id}/stream", endpoint, methods=["GET"])
    return holds


@asynccontextmanager
async def _serve(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started, within=10)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(runner, 15)
        sock.close()


class _Stream:
    """经真实 TCP 增量读取 SSE；只解析已完整到达的帧。"""

    def __init__(self, base, client: httpx.AsyncClient, task: dict):
        self.text, self.status, self.closed = "", None, asyncio.Event()
        cookies = {cookie.name: cookie.value for cookie in client.cookies.jar}
        self._client = httpx.AsyncClient(base_url=base, cookies=cookies, timeout=20)
        self._runner = asyncio.create_task(self._run(task))

    async def _run(self, task):
        try:
            async with self._client.stream("GET", f"/_test/tasks/{task['id']}/stream",
                                           params={"task_key": task["task_key"]}) as response:
                self.status = response.status_code
                async for chunk in response.aiter_text():
                    self.text += chunk
        finally:
            self.closed.set()

    def events(self) -> list[tuple[str, dict]]:
        end = self.text.rfind("\n\n")
        return parse_sse(self.text[:end + 2]) if end >= 0 else []

    async def wait_for(self, predicate, within: float = 10.0):
        await wait_until(lambda: predicate(self.events()) or self.closed.is_set(), within=within)
        assert predicate(self.events()), (self.status, self.text)

    async def finish(self, within: float = 15.0) -> list[tuple[str, dict]]:
        await asyncio.wait_for(self.closed.wait(), within)
        await self._runner
        return self.events()

    async def aclose(self):
        if not self._runner.done():
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        await self._client.aclose()


async def _task_row(app, task_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Task, task_id)


async def _settled(app, task_id, within=15.0):
    async with asyncio.timeout(within):
        while True:
            row = await _task_row(app, task_id)
            if row.status not in ("queued", "running") and task_id not in app.state.task_engine._running:
                return row
            await asyncio.sleep(0.02)


def _text(events) -> str:
    snapshot = next(data["text"] for event, data in events if event == "snapshot")
    return snapshot + "".join(data["text"] for event, data in events if event == "delta")


async def _start(kind, owner, guest, world, fake_llm, chunks, *, actor="editor"):
    client = guest if actor == "editor" else owner
    if kind == "chapter":
        fake_llm.push(chunks)
        created = await client.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    else:
        fake_llm.push(chunks)
        created = await client.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert created.status_code == 202, created.text
    return created.json()


def _split_analysis():
    output = analysis_json(summary="协作者运行帧分析的概要")
    return output[:40], output[40:80], output[80:]


# ---------------------------------------------------------------- 正例：各类帧均可证明


@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_chapter_running_frames_including_custom_events_pass_proof_over_tcp(
    app, client, make_client, fake_llm, actor,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    holds = _mount(app)
    gate = Gate()
    first, second = "第一段正文", "第二段正文"
    # 首轮字数不足触发两轮自动续写：continue 是 handler 自定义事件，经 hub.publish 携带同一发布来源。
    fake_llm.push(["开头", first, gate, second])
    fake_llm.push("续写第一轮")
    fake_llm.push("续写第二轮")
    client_for = guest if actor == "editor" else client
    created = await client_for.post(f"/api/v1/chapters/{world.chapter['id']}/generate",
                                    json={"auto_continue": True, "target_words": 300})
    assert created.status_code == 202, created.text
    task = created.json()
    engine = app.state.task_engine
    await gate.wait_reached()
    # 已有两段不同的 delta：快照来源必须是同一 guard 的同一发布来源（否则视为混合来源而拒绝）。
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text == "开头" + first)
    async with _serve(app) as base:
        stream = _Stream(base, client_for, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            events = await stream.finish()
        finally:
            gate.release()
            await stream.aclose()
    assert stream.status == 200, stream.text
    names = [event for event, _ in events]
    assert names[0] == "snapshot" and events[0][1]["text"] == "开头" + first
    assert {"delta", "progress", "continue"} <= set(names), events
    assert names[-1] == "done", events[-1]
    done = events[-1][1]
    assert done["task"]["status"] == "succeeded" and done["task"]["task_key"] == task["task_key"]
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, world.chapter["id"])
    assert _text(events) == chapter.content == done["result"]["chapter"]["content"]
    assert "permission_denied" not in stream.text
    assert holds == []


@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_analysis_running_frames_pass_proof_over_tcp(app, client, make_client, fake_llm, actor):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    first, second, rest = _split_analysis()
    task = await _start("analysis", client, guest, world, fake_llm, [first, gate, second, rest], actor=actor)
    engine = app.state.task_engine
    await gate.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text == first)
    client_for = guest if actor == "editor" else client
    async with _serve(app) as base:
        stream = _Stream(base, client_for, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            events = await stream.finish()
        finally:
            gate.release()
            await stream.aclose()
    names = [event for event, _ in events]
    assert names[0] == "snapshot" and events[0][1]["text"] == first
    assert {"delta", "progress"} <= set(names), names
    assert names[-1] == "done", events[-1]
    assert _text(events) == first + second + rest
    done = events[-1][1]
    assert done["task"]["status"] == "succeeded" and done["result"]["summary"] == "协作者运行帧分析的概要"
    assert "permission_denied" not in stream.text


async def test_failed_chapter_error_frame_is_proven_for_editor(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    task = await _start("chapter", client, guest, world, fake_llm, ["半截正文", gate, RuntimeError("模型中断")])
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            events = await stream.finish()
        finally:
            gate.release()
            await stream.aclose()
    assert events[0][0] == "snapshot" and events[-1][0] == "error", events
    assert events[-1][1]["task"] is not None and events[-1][1]["task"]["status"] == "failed"
    assert "code" not in events[-1][1]


async def _scene_review_world(app, owner, guest):
    project, chapter, url, scene = await setup_scene(owner, "作者原稿🙂甲乙")
    await register(guest, "e5c1-review-editor")
    await create_ai_config(guest, name="本人模型", model="editor-model")
    invite = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "e5c1-review-editor", "role": "editor",
    })
    assert invite.status_code == 201, invite.text
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    document_url = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await owner.get(document_url)).json())
    add_section(draft, enabled=True)
    draft["document"]["sections"][0]["anchor"] = 2
    saved = await owner.put(document_url, json=draft)
    assert saved.status_code == 200, saved.text
    return url, scene, saved.json()


@pytest.mark.parametrize("discard", [False, True])
async def test_single_chapter_paused_review_frame_uses_terminal_proof(app, client, make_client, fake_llm, discard):
    guest = make_client()
    url, scene, source = await _scene_review_world(app, client, guest)
    hold = _Hold(lambda item: item[0] == "paused")
    _mount(app, [hold] if discard else [])
    gate = Gate()
    fake_llm.push(["完全不同的", gate, "候选稿正文"])
    created = await guest.post(f"{url}/scenes/{scene['id']}/generate", json={
        "expected_content_revision": source["content_revision"],
        "expected_planning_revision": source["planning_revision"], "target_words": 300,
    })
    assert created.status_code == 202, created.text
    task = created.json()
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            if discard:
                await asyncio.wait_for(hold.reached.wait(), 15)
                # 只丢弃提案、不改 Task 行：终态视图仍一致，仅联表证明能拦截。
                async with app.state.sessionmaker() as db:
                    changed = await db.execute(update(SceneReviewProposal).where(
                        SceneReviewProposal.task_id == task["id"],
                    ).values(status="discarded"))
                    assert changed.rowcount == 1
                    await db.commit()
                hold.proceed.set()
            events = await stream.finish()
        finally:
            gate.release()
            hold.proceed.set()
            await stream.aclose()
    names = [event for event, _ in events]
    assert names[0] == "snapshot" and "delta" in names
    if discard:
        assert events[-1] == ("error", DENIED)
        assert "rich_document_review" not in stream.text
        return
    assert names[-1] == "paused", events[-1]
    result = events[-1][1]["result"]
    assert result["reason"] == "rich_document_review" and result["task_key"] == task["task_key"]
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.task_id == task["id"]))
    assert (proposal.token, proposal.run_key, proposal.status) == (result["token"], result["run_key"], "pending")


# ---------------------------------------------------------------- 撤权矩阵


async def _apply(app, owner, guest, world, change):
    if change == "password":
        response = await guest.post("/api/v1/auth/change-password", json={
            "old_password": PASSWORD, "new_password": NEW_PASSWORD,
        })
        assert response.status_code == 204, response.text
        return
    if change == "reinvite":
        await _change(app, owner, world, "revoked")
        invite = await owner.post(f"/api/v1/projects/{world.project['id']}/collaborators", json={
            "username": "e4f-guest", "role": "editor", "allow_owner_ai": True,
        })
        assert invite.status_code == 201, invite.text
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
        return
    await _change(app, owner, world, {"revoke": "revoked", "viewer": "viewer", "owner_ai": "owner_ai"}[change])


@pytest.mark.parametrize("notify", ["notify", "silent"])
@pytest.mark.parametrize("change", ["revoke", "viewer", "owner_ai", "password", "reinvite"])
@pytest.mark.parametrize("kind", ["chapter", "analysis"])
async def test_running_frame_produced_before_change_is_denied_at_send(
    app, client, make_client, fake_llm, monkeypatch, kind, change, notify,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    gate1, gate2 = Gate(), Gate()
    if kind == "chapter":
        marker = "撤权前已产出的帧"
        chunks = ["撤权前的快照", gate1, marker, gate2, "撤权后不得生成"]
    else:
        # 分析输出须是合法 JSON：以中段原文作为被持住帧的标记。
        first, marker, rest = _split_analysis()
        chunks = [first, gate1, marker, gate2, rest]
    hold = _Hold(lambda item: item[0] == "delta" and marker in item[1].get("text", ""))
    _mount(app, [hold])
    task = await _start(kind, client, guest, world, fake_llm, chunks)
    await gate1.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    snapshot_text = engine.hubs[task["id"]].text
    if notify == "silent":
        # 无通知：不唤醒订阅、不提前停止运行体；只有发送时的锁内复查能拦截。
        monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate1.release()
            # 运行体已核验并发布该帧，订阅侧已取得、尚未发送；运行体随后阻塞在 gate2（任务仍 running）。
            await asyncio.wait_for(hold.reached.wait(), 10)
            await gate2.wait_reached()
            await _apply(app, client, guest, world, change)
            if notify == "notify" and change == "password":
                engine.notify_collaboration_change(world.project["id"], world.guest["id"])
            if notify == "silent":
                row = await _task_row(app, task["id"])
                assert row.status == "running" and row.execution_key is not None
            hold.proceed.set()
            events = await stream.finish()
            if notify == "silent":
                # 拒绝来自发送侧：运行体仍阻塞，任务仍为 running。
                assert (await _task_row(app, task["id"])).status == "running"
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            await stream.aclose()
    assert stream.status == 200
    assert events[0] == ("snapshot", events[0][1]) and events[0][1]["text"] == snapshot_text
    assert events[1:] == [("error", DENIED)], events
    assert marker not in stream.text and "撤权后不得生成" not in stream.text
    assert '"result"' not in stream.text
    row = await _settled(app, task["id"])
    assert row.status != "succeeded", (row.status, row.error)


@pytest.mark.parametrize("kind", ["chapter", "analysis"])
async def test_held_running_frame_is_sent_when_nothing_changes(app, client, make_client, fake_llm, monkeypatch, kind):
    """正对照：同样持住已产出帧再发送，未改变授权时该帧与后续帧照常发出。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    marker = "持住后照常发送"
    hold = _Hold(lambda item: item[0] == "delta" and marker in item[1].get("text", ""))
    _mount(app, [hold])
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate1, gate2 = Gate(), Gate()
    if kind == "chapter":
        chunks = ["快照", gate1, marker, gate2, "其余正文"]
    else:
        # 分析输出须是合法 JSON：以中段原文作为被持住帧的标记。
        first, marker, rest = _split_analysis()
        chunks = [first, gate1, marker, gate2, rest]
    task = await _start(kind, client, guest, world, fake_llm, chunks)
    await gate1.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate1.release()
            await asyncio.wait_for(hold.reached.wait(), 10)
            await gate2.wait_reached()
            hold.proceed.set()
            await stream.wait_for(lambda events: any(
                event == "delta" and marker in data["text"] for event, data in events))
            gate2.release()
            events = await stream.finish()
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            await stream.aclose()
    assert events[-1][0] == "done", events[-1]
    assert "permission_denied" not in stream.text
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_notification_closes_idle_running_stream_before_next_frame(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    task = await _start("chapter", client, guest, world, fake_llm, ["通知前的快照", gate, "通知后不得发送"])
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            await _change(app, client, world, "viewer")
            # 本进程通知唤醒空闲订阅，无需等待下一帧或心跳。
            events = await stream.finish(within=5)
        finally:
            gate.release()
            await stream.aclose()
    assert events[1:] == [("error", DENIED)]
    assert "通知后不得发送" not in stream.text
    await _settled(app, task["id"])


# ---------------------------------------------------------------- 只读核对是承重的


async def test_chapter_frame_denied_when_claim_no_longer_held(app, client, make_client, fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    marker = "占用变化前已产出"
    hold = _Hold(lambda item: item[0] == "delta" and marker in item[1].get("text", ""))
    _mount(app, [hold])
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate1, gate2 = Gate(), Gate()
    task = await _start("chapter", client, guest, world, fake_llm, ["快照", gate1, marker, gate2, "其余"])
    await gate1.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate1.release()
            await asyncio.wait_for(hold.reached.wait(), 10)
            await gate2.wait_reached()
            # 只改章节占用，不动 Task 行与授权：Task 视图、成员与认证仍一致，唯有占用只读核对能拦截。
            async with app.state.sessionmaker() as db:
                changed = await db.execute(update(Chapter).where(Chapter.id == world.chapter["id"]).values(
                    generation_claim_key=str(uuid4()),
                ))
                assert changed.rowcount == 1
                await db.commit()
            hold.proceed.set()
            events = await stream.finish()
            assert (await _task_row(app, task["id"])).status == "running"
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            await stream.aclose()
    assert events[1:] == [("error", DENIED)], events
    assert marker not in stream.text
    await _settled(app, task["id"])


async def test_analysis_source_not_derived_from_guard_pending_is_denied(
    app, client, make_client, fake_llm, monkeypatch,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate = Gate()
    first, second, rest = _split_analysis()
    task = await _start("analysis", client, guest, world, fake_llm, [first, gate, second, rest])
    await gate.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    hub = engine.hubs[task["id"]]
    source = hub.source
    assert type(source) is stream_evidence.PublicationSource
    guard, execution = source.guard, source.execution
    assert publication_execution_matches(guard, execution)
    # 把持久断点中的 pending 凭据换成另一份：与之逐字一致的伪造执行身份不能由该 guard 签发。
    checkpoint = execution.checkpoint
    forged_checkpoint = {**checkpoint, "_analysis_pending": {**checkpoint["_analysis_pending"], "nonce": str(uuid4())}}
    forged = execution_identity.advance_checkpoint(execution, forged_checkpoint)
    assert not publication_execution_matches(guard, forged)
    with pytest.raises(ValueError):
        stream_evidence.issue_publication(forged, guard, hub=hub)
    try:
        binding = await _binding(app, world.guest["id"], task["id"], task["task_key"])
        async with app.state.sessionmaker() as db:
            await db.execute(update(Task).where(Task.id == task["id"]).values(checkpoint=forged_checkpoint))
            await db.commit()
            stamp = await read_visibility_stamp(db, world.project["id"])
        # 即使发布层失守（测试内模拟），中央锁内只读核对仍按 guard 自己的 pending 凭据拒绝。
        monkeypatch.setattr("app.services.collaboration_ai_guard.publication_execution_matches",
                            lambda _guard, _execution: True)
        forged_source = stream_evidence.issue_publication(forged, guard, hub=hub)
        data = {"text": "伪造来源的增量"}
        evidence = stream_evidence.frame_evidence("delta", data, forged_source, stamp)
        events = await _send(app, binding, [("delta", data, evidence)])
        assert events == [("error", DENIED)], events
        # 正对照：未伪造的原来源在同一持久断点下仍不可用（断点已被改写），而在原断点下可通过。
        async with app.state.sessionmaker() as db:
            await db.execute(update(Task).where(Task.id == task["id"]).values(checkpoint=checkpoint))
            await db.commit()
        monkeypatch.undo()
        original = stream_evidence.frame_evidence("delta", data, source, stamp)
        assert await _send(app, binding, [("delta", data, original)]) == [("delta", data)]
    finally:
        gate.release()
    await _settled(app, task["id"])


# ---------------------------------------------------------------- 复核补充（check）


def _receipt_identity(guard, *, drop=None, change=None, extra_checkpoint=None):
    """按 analysis_service._write_analysis_completion 的形状，从 guard 自己的 pending 派生完成收据身份。"""
    from app.services import analysis_service

    original = guard._task
    base = dict(original.checkpoint or {})
    receipt = {**analysis_service._pending_document(guard._analysis_pending), "analysis_id": 1, "chapter_no": 1,
               "content_hash": "c", "result_hash": "r", "analysis_hash": "a", "change_counts": {}}
    if drop is not None:
        receipt.pop(drop)
    if change is not None:
        receipt[change[0]] = change[1]
    checkpoint = {**base, "_analysis_completion": receipt, **(extra_checkpoint or {})}
    return execution_identity.advance_checkpoint(original, checkpoint)


@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_publication_receipt_must_contain_every_pending_key(app, client, make_client, fake_llm, actor):
    """完成收据须逐键包含同一 pending 文档：缺键（含值为 None 的 grant）、改值、断点夹带其他键均拒绝。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    gate = Gate()
    first, second, rest = _split_analysis()
    task = await _start("analysis", client, guest, world, fake_llm, [first, gate, second, rest], actor=actor)
    try:
        await gate.wait_reached()
        await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
        guard = app.state.task_engine.hubs[task["id"]].source.guard
        assert publication_execution_matches(guard, guard._task)
        assert publication_execution_matches(guard, _receipt_identity(guard))
        # 作者 pending 的 grant 为 None：缺少该键时不能用 None 冒充。
        assert (guard._analysis_pending.execution.grant_identity is None) == (actor == "owner")
        for key in ("grant", "nonce", "task_key", "execution_key"):
            assert not publication_execution_matches(guard, _receipt_identity(guard, drop=key)), key
        assert not publication_execution_matches(guard, _receipt_identity(guard, change=("nonce", str(uuid4()))))
        assert not publication_execution_matches(guard, _receipt_identity(guard, extra_checkpoint={"x": 1}))
    finally:
        gate.release()
    await _settled(app, task["id"])


async def test_stream_readable_whitelist_rejects_batch_document_and_embedding(app, client, make_client, fake_llm):
    from dataclasses import replace

    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    gate = Gate()
    task = await _start("chapter", client, guest, world, fake_llm, ["白名单", gate, "其余"])
    try:
        await gate.wait_reached()
        await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
        guard = app.state.task_engine.hubs[task["id"]].source.guard
        assert guard._stream_readable()
        sentinel = object()
        denied = {
            "batch_kind": replace(guard, _task=replace(guard._task, kind="chapter.batch"), _chapter=None),
            "batch_attempt": replace(guard, _batch_attempt=sentinel),
            "batch_chapter": replace(guard, _chapter=replace(guard._chapter, kind="batch")),
            "batch_analysis": replace(guard, _analysis_reference=sentinel),
            "document_claim": replace(guard, _document_claim=sentinel),
            "embedding_inputs": replace(guard, _embedding_inputs=()),
            "analysis_on_chapter": replace(guard, _analysis_execution=sentinel),
        }
        assert {name: value._stream_readable() for name, value in denied.items()} == dict.fromkeys(denied, False)
    finally:
        gate.release()
    await _settled(app, task["id"])


async def test_chapter_stream_guard_cache_tracks_every_binding_input(app, client, make_client, fake_llm, monkeypatch):
    """缓存 guard 只在全部绑定输入（按对象身份）不变时交出；任一被替换即拒绝，verify 侧重建新 guard。"""
    from dataclasses import replace

    from app.services.task_engine.handlers import chapter as chapter_handler

    captured = {}
    original_build = chapter_handler.ChapterHandler.build_job

    async def capture(self, *args, **kwargs):
        job = await original_build(self, *args, **kwargs)
        captured["job"] = job
        return job

    monkeypatch.setattr(chapter_handler.ChapterHandler, "build_job", capture)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    gate = Gate()
    task = await _start("chapter", client, guest, world, fake_llm, ["缓存", gate, "其余"])
    try:
        await gate.wait_reached()
        await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
        job, handler = captured["job"], chapter_handler.ChapterHandler()
        guard = app.state.task_engine.hubs[task["id"]].source.guard
        execution = job.execution

        def stream():
            return handler.stream_guard(job, execution=execution, current_execution=execution, event="delta", data={})

        assert stream() is guard and chapter_handler._job_guard(job, execution) is guard
        for name in ("model_binding", "context_stamp", "chapter_claim", "embedding_selection"):
            old = getattr(job, name)
            if old is None:
                continue
            setattr(job, name, replace(old))  # 等值但不同对象：仍视为绑定变化
            with pytest.raises(ConflictError):
                stream()
            rebuilt = chapter_handler._job_guard(job, execution)
            assert rebuilt is not guard
            setattr(job, name, old)
            # 输入还原后缓存仍是重建的那个（按旧输入），与当前输入不一致 → 再次重建、不复活旧缓存外的对象。
            assert chapter_handler._job_guard(job, execution) is not rebuilt
        # 换执行身份（等值新对象）同样拒绝。
        with pytest.raises(ConflictError):
            handler.stream_guard(job, execution=replace(execution), current_execution=replace(execution),
                                 event="delta", data={})
        # 恢复为运行体原 guard，任务照常完成。
        job.publication_guard = (chapter_handler._guard_inputs(job, execution), guard)
        assert stream() is guard
    finally:
        gate.release()
    assert (await _settled(app, task["id"])).status == "succeeded"


@pytest.mark.parametrize("settings_overrides", [{
    "allow_private_ai_endpoints": True, "auto_analyze_enabled": False, "task_max_per_user": 1,
}])
async def test_queued_subscription_receives_claim_frames_and_done(app, client, make_client, fake_llm):
    """E5-e（原已知窗口翻转）：排队中开始的受控订阅在领取瞬间不再被拒。

    领取提交确认后 hub 来源是引擎登记的无输出领取来源（ClaimSource），领取 progress（含排队位次清空的过渡帧）
    由持锁行仍为同一执行证明；随后正文帧照常经首个已核验事件的发布来源证明，直到 done。
    """
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    second = await client.post(f"/api/v1/projects/{world.project['id']}/chapters",
                               json={"title": "第二章", "content": "第二章原文"})
    assert second.status_code == 201, second.text
    _mount(app)
    gate = Gate()
    fake_llm.push(["第一项", gate, "结束"])
    first = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert first.status_code == 202, first.text
    await gate.wait_reached()
    fake_llm.push("排队任务正文")
    queued = await guest.post(f"/api/v1/chapters/{second.json()['id']}/generate", json={})
    assert queued.status_code == 202, queued.text
    task = queued.json()
    assert (await _task_row(app, task["id"])).status == "queued"
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            assert stream.events()[0][1]["task"]["status"] == "queued"
            gate.release()
            events = await stream.finish()
        finally:
            gate.release()
            await stream.aclose()
    names = [event for event, _ in events]
    assert names[0] == "snapshot" and names[-1] == "done", events
    assert any(event == "progress" and data["status"] == "running" for event, data in events), events
    assert "permission_denied" not in stream.text
    assert _text(events) == "排队任务正文"
    assert (await _settled(app, task["id"])).status == "succeeded"


@pytest.mark.parametrize("kind", ["chapter", "analysis"])
async def test_subscription_before_first_verified_event_gets_empty_snapshot(app, client, make_client, fake_llm, kind):
    """E5-e（原已知窗口翻转）：领取后、首个事件核验前（等待模型首个 token）开始的受控订阅。

    hub 来源是领取来源：只发不含模型输出的快照（正文为空、任务摘要按持锁行重算），等首个已核验来源后再发 delta。
    """
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    if kind == "chapter":
        body = "首个事件前订阅的正文"
        chunks = [gate, body]
    else:
        first, second, rest = _split_analysis()
        body, chunks = first + second + rest, [gate, first + second + rest]
    task = await _start(kind, client, guest, world, fake_llm, chunks)
    await gate.wait_reached()
    hub = app.state.task_engine.hubs[task["id"]]
    assert type(hub.source) is stream_evidence.ClaimSource and hub.text == ""
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            snapshot = stream.events()[0][1]
            assert snapshot["text"] == "" and snapshot["task"]["status"] == "running"
            assert body not in stream.text
            gate.release()
            events = await stream.finish()
        finally:
            gate.release()
            await stream.aclose()
    assert events[0][0] == "snapshot" and events[-1][0] == "done", events
    assert "permission_denied" not in stream.text
    assert _text(events) == body
    assert (await _settled(app, task["id"])).status == "succeeded"
