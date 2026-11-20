"""协作者 agent.turn 实时进度订阅（受控 SSE 管线）专项测试。

覆盖：
1. 完整实时事件流（snapshot / progress / reset / delta / tool_start / tool_end / proposal / executed / paused / done），
   包含作者模型回退 + 共享向量绑定，确认后继续并完成，且零内部字段泄露；
2. 自动执行 + 多轮跨断点（round 1 -> round 2）中途重连快照（TaskHub.text_source 沿 publication_successor 推进）；
3. 排队中早订阅（queued -> running -> done）；
4. SQLite 慢模型响应头（> frame_timeout）期间 _send_lease 与 _task_frame_lease 并发无阻塞不断流；
5. 实时撤权矩阵（撤权 / 降为 viewer / 关 allow_owner_ai（HTTP + 直接改库无通知）/ 改密 / 停用 / 重邀 /
   收紧 AI 可见性）立即断开并返回 error {code: "permission_denied"} 且零泄露后续输出；
6. 身份与帧安全边界：viewer 不可订阅活动/暂停轮次、可回放本人已结束轮次；撤权/他人/纯管理员 404；
   AgentChange 被删或键不匹配时锁内拒绝；publication_only 守卫不可发送/预检/写库。
"""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, update

from app.models import AgentChange, ProjectCollaborator, Task, User
from app.services import collaboration_ai_guard
from app.services.task_engine import stream_evidence
from app.services.task_engine.registry import get_handler
from tests.helpers import (
    Gate,
    create_ai_config,
    create_project,
    parse_sse,
    register,
    task_params,
    wait_task,
    wait_until,
)
from tests.test_agent_entries_e4id import _detail, _invite, _new_conversation, _send
from tests.test_agent_mcp_skills import change_action
from tests.test_agent_runtime import call
from tests.test_collaboration_stream_early_subscribe_e5e import _QuotaBlock
from tests.test_collaboration_stream_running_e5c1 import _serve, _settled
from tests.test_consistency_stream_slow_model import slow_headers

OWNER_KEY = "sk-agent-sse-owner-0001"
EMBED_KEY = "sk-agent-sse-embed-0001"
EDITOR_KEY = "sk-agent-sse-editor-0001"
PASSWORD = "secret123"
NEW_PASSWORD = "secret456new"
STOP = ("paused", "succeeded", "failed", "cancelled", "interrupted")
DENIED = {"task": None, "detail": "任务访问权限已失效", "code": "permission_denied"}
PRIVATE_TOKENS = (
    "_project_model",
    "_shared_embedding",
    "_collaboration_grant",
    "_task_actor_token_version",
    "_agent_actor_token_version",
    "config_fingerprint",
    "config_instance_key",
    OWNER_KEY,
    EMBED_KEY,
    EDITOR_KEY,
)


class _ProdStream:
    """经真实 TCP 连接生产路由 GET /api/v1/tasks/{id}/stream 的增量 SSE 读取器。"""

    def __init__(self, base: str, client: httpx.AsyncClient, task: dict):
        self.text = ""
        self.status: int | None = None
        self.closed = asyncio.Event()
        cookies = {cookie.name: cookie.value for cookie in client.cookies.jar}
        self._client = httpx.AsyncClient(base_url=base, cookies=cookies, timeout=25)
        self._runner = asyncio.create_task(self._run(task))

    async def _run(self, task: dict) -> None:
        try:
            async with self._client.stream(
                "GET", f"/api/v1/tasks/{task['id']}/stream", params={"task_key": task["task_key"]},
            ) as response:
                self.status = response.status_code
                async for chunk in response.aiter_text():
                    self.text += chunk
        finally:
            self.closed.set()

    def events(self) -> list[tuple[str, dict]]:
        end = self.text.rfind("\n\n")
        return parse_sse(self.text[:end + 2]) if end >= 0 else []

    async def wait_for(self, predicate, within: float = 10.0) -> list[tuple[str, dict]]:
        await wait_until(lambda: predicate(self.events()) or self.closed.is_set(), within=within)
        current = self.events()
        assert predicate(current), (self.status, self.text)
        return current

    async def finish(self, within: float = 20.0) -> list[tuple[str, dict]]:
        await asyncio.wait_for(self.closed.wait(), within)
        await self._runner
        return self.events()

    async def aclose(self) -> None:
        if not self._runner.done():
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        await self._client.aclose()


async def _setup_world(
    app, client, make_client, *, editor_own_model: bool = False, owner_embedding: bool = True,
    allow_owner_ai: bool = True, auto_execute: bool = False, prefix: str = "agsse",
):
    owner = await register(client, f"{prefix}-owner")
    await create_ai_config(client, api_key=OWNER_KEY)
    if owner_embedding:
        await create_ai_config(client, name="作者向量", model_type="embedding", model="emb-1", api_key=EMBED_KEY)
    project = await create_project(client, title="实时协作测试作品")
    await client.patch(f"/api/v1/projects/{project['id']}", json={"synopsis": "原简介", "theme": "原主题"})
    editor_client = make_client()
    editor = await register(editor_client, f"{prefix}-editor")
    if editor_own_model:
        await create_ai_config(editor_client, api_key=EDITOR_KEY)
    member = await _invite(client, editor_client, project["id"], f"{prefix}-editor", allow_owner_ai=allow_owner_ai)
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)
    if auto_execute:
        patched = await editor_client.patch(f"{base}/conversations/{cid}", json={"auto_execute": True})
        assert patched.status_code == 200, patched.text
    return SimpleNamespace(
        owner=owner, owner_client=client, editor=editor, editor_client=editor_client,
        editor_name=f"{prefix}-editor", project=project, member=member, base=base, cid=cid,
    )


async def test_editor_agent_stream_full_lifecycle_proposal_confirm_and_resume(app, client, make_client, fake_llm):
    """协作者订阅 agent.turn：收到实时 delta / tool / proposal / paused，确认续跑后收到 reset / delta / done。"""
    world = await _setup_world(app, client, make_client, editor_own_model=False, owner_embedding=True, prefix="life")
    gate1 = Gate()
    fake_llm.push_tools([
        gate1,
        ("text", {"text": "先读取作品并提交修改提案。"}),
        call("get_project_overview", {}, "read_1"),
        call("update_project", {"synopsis": "协作者实时新简介"}, "write_1"),
    ])

    turn = await _send(world.editor_client, world.base, world.cid)
    async with _serve(app) as base:
        stream = _ProdStream(base, world.editor_client, turn)
        try:
            await asyncio.wait_for(gate1.reached.wait(), 10)
            await stream.wait_for(lambda evs: any(name == "snapshot" for name, _ in evs))
            gate1.release()
            events = await stream.finish(within=15)
        finally:
            gate1.release()
            await stream.aclose()

    assert stream.status == 200
    for secret in PRIVATE_TOKENS:
        assert secret not in stream.text, f"leaked private field {secret}"
    names = [name for name, _ in events]
    assert "snapshot" in names and "delta" in names
    assert "tool_start" in names and "tool_end" in names and "proposal" in names
    assert names[-1] == "paused"
    proposal = next(data["change"] for name, data in events if name == "proposal")
    assert proposal["tool_name"] == "update_project"
    assert isinstance(proposal.get("change_key"), str) and isinstance(proposal.get("view_version"), str)
    assert proposal["turn_task_id"] == turn["id"] and proposal["turn_task_key"] == turn["task_key"]

    # 确认提案触发续跑，再次订阅生产 SSE，收到第二轮实时文本与 done
    gate2 = Gate()
    fake_llm.push_tools([gate2, ("text", {"text": "已按你的确认更新简介。"})])
    confirmed = await world.editor_client.post(
        f"{world.base}/changes/{proposal['id']}/confirm", json=change_action(proposal),
    )
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text

    async with _serve(app) as base:
        resumed = _ProdStream(base, world.editor_client, turn)
        try:
            await asyncio.wait_for(gate2.reached.wait(), 10)
            await resumed.wait_for(lambda evs: any(name == "snapshot" for name, _ in evs))
            gate2.release()
            resumed_events = await resumed.finish(within=15)
        finally:
            gate2.release()
            await resumed.aclose()

    assert resumed.status == 200
    for secret in PRIVATE_TOKENS:
        assert secret not in resumed.text
    assert resumed_events[-1][0] == "done"
    deltas = "".join(data["text"] for name, data in resumed_events if name == "delta")
    assert "已按你的确认更新简介。" in deltas
    assert (await _settled(app, turn["id"])).status == "succeeded"


async def test_editor_agent_stream_auto_execute_and_multi_round_mid_turn_reconnect(app, client, make_client, fake_llm):
    """自动执行 + 多轮跨断点：首条流收到 proposal / executed / 多轮文本，第 2 轮中途新连快照不因混合来源失败。"""
    world = await _setup_world(
        app, client, make_client, editor_own_model=True, owner_embedding=False, auto_execute=True, prefix="multi",
    )
    gate_round1 = Gate()
    gate_round2 = Gate()
    fake_llm.push_tools([
        gate_round1,
        ("text", {"text": "第一轮开始。"}),
        call("update_project", {"synopsis": "自动执行简介"}, "auto_1"),
    ])
    fake_llm.push_tools([
        ("text", {"text": "第二轮前半。"}),
        gate_round2,
        ("text", {"text": "第二轮收尾。"}),
    ])

    turn = await _send(world.editor_client, world.base, world.cid)
    async with _serve(app) as base:
        first = _ProdStream(base, world.editor_client, turn)
        second = None
        try:
            await asyncio.wait_for(gate_round1.reached.wait(), 10)
            await first.wait_for(lambda evs: any(name == "snapshot" for name, _ in evs))
            gate_round1.release()
            await asyncio.wait_for(gate_round2.reached.wait(), 10)
            await first.wait_for(
                lambda evs: any(name == "delta" and "第二轮前半。" in data.get("text", "") for name, data in evs),
            )
            # 在第 2 轮已推进多个断点且产出第 2 轮 delta 后，打开第二条订阅验证中途重连快照
            second = _ProdStream(base, world.editor_client, turn)
            await second.wait_for(lambda evs: any(name == "snapshot" for name, _ in evs))
            snap = next(data for name, data in second.events() if name == "snapshot")
            assert "第一轮开始。" in snap["text"] and "第二轮前半。" in snap["text"]
            gate_round2.release()
            first_events = await first.finish(within=15)
            second_events = await second.finish(within=15)
        finally:
            gate_round1.release()
            gate_round2.release()
            await first.aclose()
            if second is not None:
                await second.aclose()

    assert [n for n, _ in first_events if n in ("proposal", "executed")] == ["proposal", "executed"]
    assert first_events[-1][0] == "done" and second_events[-1][0] == "done"
    assert "第二轮收尾。" in "".join(d["text"] for n, d in second_events if n == "delta")


async def test_editor_agent_stream_early_subscribe_queued_transitions_to_done(
    app, client, make_client, fake_llm, monkeypatch,
):
    """协作者在 agent.turn 尚处于 queued 时订阅：经历 queued snapshot -> running -> delta -> done 不断线。"""
    world = await _setup_world(app, client, make_client, editor_own_model=True, owner_embedding=False, prefix="early")
    block = _QuotaBlock(monkeypatch, app.state.task_engine)
    fake_llm.push_tools([("text", {"text": "排队后正常完成。"})])
    try:
        turn = await _send(world.editor_client, world.base, world.cid)
        assert turn["status"] == "queued"
        async with _serve(app) as base:
            stream = _ProdStream(base, world.editor_client, turn)
            try:
                await stream.wait_for(
                    lambda evs: any(n == "snapshot" and d["task"]["status"] == "queued" for n, d in evs),
                )
                block.release()
                events = await stream.finish(within=15)
            finally:
                block.release()
                await stream.aclose()
    finally:
        block.release()

    assert events[-1][0] == "done"
    assert "排队后正常完成。" in "".join(d["text"] for n, d in events if n == "delta")


async def test_editor_agent_stream_survives_slow_model_headers_on_sqlite(
    app, client, make_client, fake_llm, monkeypatch,
):
    """SQLite 慢模型响应头延迟 > frame_timeout 时，协作者 agent.turn 流不断开、不阻塞在 _send_lease。"""
    from app.api.v1 import tasks as tasks_api

    orig_sse = tasks_api.task_sse_response
    monkeypatch.setattr(tasks_api, "task_sse_response", lambda *a, **kw: orig_sse(*a, frame_timeout=0.5, **kw))

    header_entered = asyncio.Event()
    slow_headers(monkeypatch, 1.2, reached=header_entered)
    fake_llm.push_tools([("text", {"text": "慢模型完整回答。"})])

    world = await _setup_world(app, client, make_client, editor_own_model=False, owner_embedding=False, prefix="slow")
    turn = await _send(world.editor_client, world.base, world.cid)
    await asyncio.wait_for(header_entered.wait(), 10)

    t0 = time.monotonic()
    async with _serve(app) as base:
        stream = _ProdStream(base, world.editor_client, turn)
        try:
            await stream.wait_for(lambda evs: any(n == "snapshot" for n, _ in evs), within=5)
            first_snap_dt = time.monotonic() - t0
            events = await stream.finish(within=15)
        finally:
            await stream.aclose()

    assert first_snap_dt < 0.8, f"snapshot blocked on slow _send_lease: {first_snap_dt:.2f}s"
    assert "permission_denied" not in stream.text
    assert events[-1][0] == "done"
    assert "慢模型完整回答。" in "".join(d["text"] for n, d in events if n == "delta")


@pytest.mark.parametrize(
    "change",
    [
        "revoke", "viewer", "owner_ai_http", "owner_ai_direct",
        "password", "deactivated", "reinvite", "visibility_tighten",
    ],
)
async def test_editor_agent_stream_immediate_disconnect_on_revocation_matrix(
    app, client, make_client, fake_llm, monkeypatch, change,
):
    """运行中实时撤权 / 降级 / 关作者模型授权（含直接改库）/ 改密 / 停用 / 重邀 / 收紧可见性：
    流立即断开并收到 error {code: "permission_denied"}，且零泄露后续输出。"""
    # 缩短心跳周期覆盖无通知路径（直接改库关闭 allow_owner_ai / 停用账号 / 收紧可见性）
    monkeypatch.setattr(app.state.task_engine, "heartbeat_seconds", 0.1)
    world = await _setup_world(
        app, client, make_client, editor_own_model=False, owner_embedding=False, prefix=f"rev-{change[:4]}",
    )
    gate_after_first = Gate()
    fake_llm.push_tools([
        ("text", {"text": "撤权前可见文本。"}),
        gate_after_first,
        ("text", {"text": "撤权后绝对不可泄露的秘密文本"}),
    ])

    turn = await _send(world.editor_client, world.base, world.cid)
    async with _serve(app) as base:
        stream = _ProdStream(base, world.editor_client, turn)
        try:
            await asyncio.wait_for(gate_after_first.reached.wait(), 10)
            await stream.wait_for(
                lambda evs: any(
                    (n == "delta" and "撤权前可见文本。" in d.get("text", ""))
                    or (n == "snapshot" and "撤权前可见文本。" in d.get("text", ""))
                    for n, d in evs
                ),
            )
            pid, uid = world.project["id"], world.editor["id"]
            rev = world.member["revision"]
            if change == "revoke":
                r = await client.delete(
                    f"/api/v1/projects/{pid}/collaborators/{uid}", params={"expected_revision": rev},
                )
                assert r.status_code == 204, r.text
            elif change == "viewer":
                r = await client.patch(f"/api/v1/projects/{pid}/collaborators/{uid}",
                                       json={"expected_revision": rev, "role": "viewer"})
                assert r.status_code == 200, r.text
            elif change == "owner_ai_http":
                r = await client.patch(f"/api/v1/projects/{pid}/collaborators/{uid}",
                                       json={"expected_revision": rev, "allow_owner_ai": False})
                assert r.status_code == 200, r.text
            elif change == "owner_ai_direct":
                async with app.state.sessionmaker() as db:
                    await db.execute(update(ProjectCollaborator).where(
                        ProjectCollaborator.project_id == pid, ProjectCollaborator.user_id == uid,
                    ).values(allow_owner_ai=False))
                    await db.commit()
            elif change == "password":
                r = await world.editor_client.post("/api/v1/auth/change-password",
                                                   json={"old_password": PASSWORD, "new_password": NEW_PASSWORD})
                assert r.status_code == 204, r.text
            elif change == "deactivated":
                async with app.state.sessionmaker() as db:
                    await db.execute(update(User).where(User.id == uid).values(is_active=False))
                    await db.commit()
            elif change == "reinvite":
                r = await client.delete(
                    f"/api/v1/projects/{pid}/collaborators/{uid}", params={"expected_revision": rev},
                )
                assert r.status_code == 204, r.text
                await _invite(client, world.editor_client, pid, world.editor_name, allow_owner_ai=True)
            elif change == "visibility_tighten":
                char_res = await client.post(
                    f"/api/v1/projects/{pid}/characters",
                    json={"name": "林远", "role_type": "protagonist"},
                )
                assert char_res.status_code == 201, char_res.text
                char_id = char_res.json()["id"]
                settings_res = await client.get(
                    f"/api/v1/projects/{pid}/setting-entries/character/{char_id}/ai-settings",
                )
                assert settings_res.status_code == 200, settings_res.text
                vis_rev = settings_res.json()["ai_visibility_revision"]
                r = await client.patch(
                    f"/api/v1/projects/{pid}/setting-entries/character/{char_id}/ai-settings",
                    json={"expected_ai_visibility_revision": vis_rev, "ai_mode": "never"},
                )
                assert r.status_code == 200, r.text
                # 收紧可见性不改变成员身份，放行下一帧触发帧租约内可见性戳复核立即拒绝
                gate_after_first.release()

            events = await stream.finish(within=10)
            gate_after_first.release()
        finally:
            gate_after_first.release()
            await stream.aclose()

    assert events[-1] == ("error", DENIED), (change, events)
    assert "撤权后绝对不可泄露的秘密文本" not in stream.text


async def test_agent_stream_identity_boundaries_and_change_and_guard_invariants(
    app, client, make_client, fake_llm, system_admin,
):
    """身份边界（viewer 活动 404 / 已结束 200、撤权/他人/纯管理员 404）、AgentChange 锁内复核、
    以及 publication_only 不可发送。"""
    world = await _setup_world(app, client, make_client, editor_own_model=True, owner_embedding=False, prefix="bound")
    other_client = make_client()
    await register(other_client, "bound-other-editor")
    await _invite(client, other_client, world.project["id"], "bound-other-editor")

    # 1. 先跑完一轮 finished 任务：验证本人 200、他人 / 作者 / 纯管理员 404
    fake_llm.push_tools([("text", {"text": "本人完成轮次文本。"})])
    finished = await _send(world.editor_client, world.base, world.cid)
    assert (await wait_task(world.editor_client, finished, within=8.0, statuses=STOP))["status"] == "succeeded"

    for stranger in (other_client, client, system_admin):
        res = await stranger.get(f"/api/v1/tasks/{finished['id']}/stream", params=task_params(finished))
        assert res.status_code == 404, res.text

    # 2. 启动一轮 paused（待确认）任务，再把协作者降为 viewer：
    #    - 活动/待确认轮次订阅返回 404
    #    - 已结束轮次仍可回放 200
    fake_llm.push_tools([call("update_project", {"synopsis": "待确认"}, "p1")])
    paused = await _send(world.editor_client, world.base, world.cid)
    assert (await wait_task(world.editor_client, paused, within=8.0, statuses=STOP))["status"] == "paused"
    detail = await _detail(world.editor_client, world.base, world.cid)
    change_dict = detail["changes"][0]

    # 2a. 验证 verify_stream_frame 对 proposal 帧在 AgentChange 被删或 change_key 不匹配时失败关闭
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, paused["id"])
        assert await stream_evidence._verify_agent_change_frame(db, row, "proposal", {"change": change_dict}) is True
        tampered = {**change_dict, "change_key": "00000000-0000-0000-0000-000000000000"}
        assert await stream_evidence._verify_agent_change_frame(db, row, "proposal", {"change": tampered}) is False
        await db.execute(delete(AgentChange).where(AgentChange.id == change_dict["id"]))
        await db.commit()
        assert await stream_evidence._verify_agent_change_frame(db, row, "proposal", {"change": change_dict}) is False

    # 降为 viewer（保留 paused 尚未被后台清掉前的窗口：直接改库以稳定验证入口 404）
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"],
            ProjectCollaborator.user_id == world.editor["id"],
        ).values(role="viewer"))
        await db.commit()
    paused_stream = await world.editor_client.get(f"/api/v1/tasks/{paused['id']}/stream", params=task_params(paused))
    assert paused_stream.status_code == 404, paused_stream.text
    finished_stream = await world.editor_client.get(
        f"/api/v1/tasks/{finished['id']}/stream", params=task_params(finished),
    )
    assert finished_stream.status_code == 200 and "本人完成轮次文本。" in finished_stream.text

    # 3. 撤权后已结束任务也返回 404
    rev_res = await client.delete(
        f"/api/v1/projects/{world.project['id']}/collaborators/{world.editor['id']}",
        params={"expected_revision": world.member["revision"]},
    )
    assert rev_res.status_code == 204, rev_res.text
    revoked_stream = await world.editor_client.get(
        f"/api/v1/tasks/{finished['id']}/stream", params=task_params(finished),
    )
    assert revoked_stream.status_code == 404, revoked_stream.text

    # 4. publication_only=True 的 agent.turn guard 永不可发送、预检或写库
    assert callable(getattr(get_handler("agent.turn"), "stream_guard", None))
    # 构造一轮新的运行中 guard 验证 publication_only 约束
    await _invite(client, world.editor_client, world.project["id"], world.editor_name, allow_owner_ai=True)
    cid2 = await _new_conversation(world.editor_client, world.base)
    gate = Gate()
    fake_llm.push_tools([gate, ("text", {"text": "guard 检查"})])
    t2 = await _send(world.editor_client, world.base, cid2)
    try:
        await asyncio.wait_for(gate.reached.wait(), 10)
        await wait_until(lambda: t2["id"] in app.state.task_engine.hubs, within=5)
        hub = app.state.task_engine.hubs[t2["id"]]
        await wait_until(lambda: type(hub.source) is stream_evidence.PublicationSource, within=5)
        pub_guard = hub.source.guard
        assert pub_guard._publication_only is True
        async with app.state.sessionmaker() as db:
            with pytest.raises(collaboration_ai_guard.CollaborationSendBlocked):
                await pub_guard.verify(db)
            with pytest.raises(collaboration_ai_guard.CollaborationSendBlocked):
                await pub_guard.verify_without_one_shot(db)
            with pytest.raises(collaboration_ai_guard.CollaborationSendBlocked):
                await pub_guard.require_task_write(db)
        with pytest.raises(collaboration_ai_guard.CollaborationSendBlocked):
            await pub_guard.prepare_provider(app.state.sessionmaker, fake_llm.factory, request_timeout=5.0)
    finally:
        gate.release()
    assert (await _settled(app, t2["id"])).status == "succeeded"


async def test_agent_stream_keeps_kb_and_book_import_closed(app, client, make_client):
    """范围守恒：开放 agent.turn 不影响 kb.index 与 book.import 协作者流继续返回 404。"""
    world = await _setup_world(app, client, make_client, editor_own_model=True, owner_embedding=False, prefix="scope")
    async with app.state.sessionmaker() as db:
        rows = [
            Task(user_id=world.editor["id"], project_id=world.project["id"], kind=kind, status="succeeded",
                 output_text="closed", title=kind)
            for kind in ("kb.index", "book.import")
        ]
        db.add_all(rows)
        await db.commit()
        created = [(r.id, r.statistics_key) for r in rows]
    for tid, tkey in created:
        res = await world.editor_client.get(f"/api/v1/tasks/{tid}/stream", params={"task_key": tkey})
        assert res.status_code == 404 and "closed" not in res.text


@pytest.mark.parametrize("revoke", [False, True])
async def test_guarded_subscribe_handles_task_advance_or_revoke_during_snapshot_projection(
    app, client, make_client, fake_llm, monkeypatch, revoke,
):
    """受控订阅在初始快照投影期间恰逢任务从 queued 领取推进：
    仍有效时重试并正常收流；期间被撤权则立即 permission_denied。"""
    from app.services import task_service

    world = await _setup_world(
        app, client, make_client, editor_own_model=False, owner_embedding=False, prefix=f"snap-{int(revoke)}",
    )
    engine = app.state.task_engine
    block = _QuotaBlock(monkeypatch, engine)
    gate_mid = Gate()
    fake_llm.push_tools([("text", {"text": "快照推进后可见输出。"}), gate_mid, ("text", {"text": "尾段。"})])
    turn = await _send(world.editor_client, world.base, world.cid)

    reached, proceed = asyncio.Event(), asyncio.Event()
    real_project = task_service.project_task_out
    intercepted = False

    async def delayed_project(db, user_id, out, *args, **kwargs):
        nonlocal intercepted
        result = await real_project(db, user_id, out, *args, **kwargs)
        if not intercepted and out.id == turn["id"] and out.status == "queued":
            intercepted = True
            reached.set()
            await proceed.wait()
        return result

    monkeypatch.setattr(task_service, "project_task_out", delayed_project)
    try:
        async with _serve(app) as base:
            stream = _ProdStream(base, world.editor_client, turn)
            try:
                await asyncio.wait_for(reached.wait(), 10)
                if revoke:
                    rev_res = await client.delete(
                        f"/api/v1/projects/{world.project['id']}/collaborators/{world.editor['id']}",
                        params={"expected_revision": world.member["revision"]},
                    )
                    assert rev_res.status_code == 204, rev_res.text
                    block.release()
                    proceed.set()
                    events = await stream.finish(within=10)
                    assert stream.status == 200
                    assert events and events[-1][0] == "error"
                    assert events[-1][1].get("code") == "permission_denied"
                    assert "快照推进后可见输出" not in stream.text
                else:
                    block.release()
                    await asyncio.wait_for(gate_mid.reached.wait(), 10)
                    proceed.set()
                    await stream.wait_for(
                        lambda evs: any(
                            (n == "delta" and "快照推进后可见输出。" in d.get("text", ""))
                            or (n == "snapshot" and "快照推进后可见输出。" in d.get("text", ""))
                            for n, d in evs
                        ),
                    )
                    gate_mid.release()
                    events = await stream.finish(within=15)
                    assert stream.status == 200
                    assert not any(n == "error" for n, _ in events), events
                    assert events[-1][0] == "done"
            finally:
                proceed.set()
                gate_mid.release()
                await stream.aclose()
    finally:
        block.release()
        proceed.set()
        gate_mid.release()
    settled = await _settled(app, turn["id"])
    assert settled.status == ("failed" if revoke else "succeeded")
