"""E5-c3：受控任务流对开书向导（wizard.outline / characters / world / careers）运行中帧与 done 帧的发布身份证明。

真实引擎执行（fake_llm + Gate），经受控 `/_test` 路由（engine.subscribe(guarded=True) + task_sse_response）
与本机 TCP 实际发送；不替换中央帧校验器。
生产路由自 E5-f 起对协作者挂载同一受控管线（tasks.py），此处 /_test 路由便于插入持帧点，作者生产订阅路径不变。

向导只有两种发布来源（`WizardHandler.stream_guard`）：
- 未决凭据阶段（stage / delta / error）：构建时的发送 guard，原执行 = 本轮未决断点；
- 保存后的 done：内容与完成收据同事务提交，断点与可见性戳都已推进，交出按「完成断点 + 保存事务内读取的新戳」
  构造的仅发布 guard。角色/世界观整体替换时保存本身会收紧可见性（递增 revision/epoch），done 帧仍须可证。

撤权矩阵采用「引擎已产出带证明的帧、发送前改变授权」：持住一帧再改变，证明拒绝来自发送时的帧租约与
`SendGuard.require_task_read` / `require_terminal_read` 的锁内只读核对，而不是运行体自身停止。
"""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.models import Outline, Task
from app.services import project_ai_config, setting_ai_visibility, wizard_service
from app.services.ai_dispatch import read_visibility_stamp
from app.services.collaboration_ai_guard import (
    _WIZARD_RECEIPT_FIELDS,
    CollaborationSendBlocked,
    for_task,
    publication_execution_matches,
)
from app.services.task_engine import execution_identity, stream_evidence
from app.services.task_engine.handlers.wizard import WizardHandler
from tests.helpers import Gate, wait_until
from tests.test_collaboration_entity_entry_e4g3b import _change, _terminal, _world
from tests.test_collaboration_stream_replay_e5b import DENIED, _send
from tests.test_collaboration_stream_running_e5c1 import _Hold, _mount, _serve, _settled, _Stream, _task_row
from tests.test_collaboration_wizard_entry_e4g3c import ANSWERS, NEW_NAMES, _names
from tests.test_task_sse_scope import _binding

PASSWORD, NEW_PASSWORD = "secret123", "secret456new"
KINDS = ("outline", "characters", "world", "careers")
# 续写默认用大纲向导（大纲保存不改可见性）；角色续写整体替换已有角色时保存会收紧可见性（递增 epoch），
# 保存后的核验拿续写来源 epoch 与构建戳 epoch 比较（2026-11-13 修复前必然失败，结果不发布）。
# 续写：开头缓冲满 600 字符后才清洗输出，之后逐片直出；最终拼成合法 JSON。
PARTIAL = '[{"title":"续一","summary":"甲"},{"title":"续'
RESUME_HEAD = ',{"title":"续二","summary":"' + "长" * 620 + '"}'
RESUME_MARKER = ',{"title":"续三标记","summary":"丙"}'
RESUME_TAIL = "]"
RESUME_NAMES = sorted(["续一", "续二", "续三标记"])
RESUME_CASES = {
    "outline": (PARTIAL, RESUME_HEAD, RESUME_MARKER, RESUME_TAIL, RESUME_NAMES),
    "characters": ('[{"name":"续一"},{"name":"续', ',{"name":"续二","profile":"' + "长" * 620 + '"}',
                   ',{"name":"续三标记"}', "]", sorted(["续一", "续二", "续三标记"])),
}


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def _pieces(answer: str) -> tuple[str, str, str]:
    """把合法 JSON 切成三段：快照、被持住的标记段、其余。"""
    return answer[:8], answer[8:16], answer[16:]


def _capture_jobs(monkeypatch) -> dict[int, wizard_service.WizardJob]:
    jobs: dict[int, wizard_service.WizardJob] = {}
    original = WizardHandler.build_job

    async def build_job(self, ctx, task, *, execution):
        job = await original(self, ctx, task, execution=execution)
        jobs[task.id] = job
        return job

    monkeypatch.setattr(WizardHandler, "build_job", build_job)
    return jobs


async def _launch(client, world, kind, fake_llm, chunks, **body):
    fake_llm.push(chunks)
    created = await client.post(f"{world.base}/wizard/{kind}", json=body)
    assert created.status_code == 202, created.text
    return created.json()


async def _resume_source(app, client, world, fake_llm, kind="outline"):
    """本人失败的向导任务（含可续写的部分输出），用于续写；默认大纲。"""
    from app.ai.providers import AIProviderError

    partial = RESUME_CASES[kind][0]
    fake_llm.push([partial, AIProviderError("模型调用失败：上游中断")])
    created = await client.post(f"{world.base}/wizard/{kind}", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.output_text == partial
    return row


async def _launch_resume(guest, world, fake_llm, source, chunks, kind="outline"):
    return await _launch(guest, world, kind, fake_llm, chunks,
                         continue_from_task_id=source.id, continue_from_task_key=source.statistics_key)


def _text(events) -> str:
    snapshot = next(data["text"] for event, data in events if event == "snapshot")
    return snapshot + "".join(data["text"] for event, data in events if event == "delta")


def _caught_up(events, hub, full: str) -> bool:
    """订阅者已实际收到运行体至今发布的全部帧（正文与最新进度/阶段）；之后才放行保存。"""
    if not events or events[0][0] != "snapshot" or _text(events) != full:
        return False
    progress = [data for event, data in events if event == "progress"]
    seen = progress[-1] if progress else events[0][1]["task"]
    return (seen["progress"], seen["stage"]) == (hub.progress, hub.stage)


async def _apply(app, owner, guest, world, change):
    if change == "password":
        response = await guest.post("/api/v1/auth/change-password", json={
            "old_password": PASSWORD, "new_password": NEW_PASSWORD,
        })
        assert response.status_code == 204, response.text
        return
    await _change(app, owner, world, {"revoke": "revoked", "viewer": "viewer", "owner_ai": "owner_ai",
                                      "reinvite": "reinvite"}[change])


# ---------------------------------------------------------------- 正例：各类帧均可证明


@pytest.mark.parametrize("actor", ["editor", "owner"])
@pytest.mark.parametrize("kind", KINDS)
async def test_wizard_running_and_done_frames_pass_proof_over_tcp(
    app, client, make_client, fake_llm, monkeypatch, kind, actor,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    jobs = _capture_jobs(monkeypatch)
    _mount(app)
    # 尾部空白让输出越过进度刻度（每 50 字符），使运行中 progress 帧可观察；向导解析忽略首尾空白。
    answer = ANSWERS[kind] + " " * 120
    head, middle, rest = _pieces(answer)
    gate = Gate()
    client_for = guest if actor == "editor" else client
    async with app.state.sessionmaker() as db:
        before = await read_visibility_stamp(db, world.project["id"])
    end = Gate()
    # 末尾 Gate：运行体在保存前停住，直到全部增量已实际发出（保存收紧可见性时的在途帧见已知窗口用例）。
    task = await _launch(client_for, world, kind, fake_llm, [head, middle, gate, rest, end])
    engine = app.state.task_engine
    await gate.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text == head + middle)
    async with _serve(app) as base:
        stream = _Stream(base, client_for, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await end.wait_reached()
            await stream.wait_for(lambda events: _caught_up(events, engine.hubs[task["id"]], answer))
            end.release()
            events = await stream.finish()
        finally:
            gate.release()
            end.release()
            await stream.aclose()
    assert stream.status == 200, stream.text
    names = [event for event, _ in events]
    assert names[0] == "snapshot" and events[0][1]["text"] == head + middle
    assert {"delta", "progress"} <= set(names), names
    assert names[-1] == "done", events[-1]
    done = events[-1][1]
    assert done["task"]["status"] == "succeeded" and done["task"]["task_key"] == task["task_key"]
    assert done["result"]["kind"] == kind
    row = await _task_row(app, task["id"])
    assert _text(events) == row.output_text == answer
    assert await _names(app, world, kind) == sorted(NEW_NAMES[kind])
    assert "permission_denied" not in stream.text
    job = jobs[task["id"]]
    async with app.state.sessionmaker() as db:
        after = await read_visibility_stamp(db, world.project["id"])
    # done 帧来源是保存后另构造的仅发布 guard：断点为本轮完成收据、可见性戳为保存事务内的新戳。
    assert job.publication_guard is not None and job.publication_guard[1]._publication_only
    assert job.publication_guard[1]._stamp is job.visibility_stamp
    if kind == "characters":
        # 整体替换已有角色会收紧可见性：done 帧不能沿用构建时的戳（承重：沿用构建 guard 即被拒）。
        assert job.guard._stamp == before and job.visibility_stamp == after != before


@pytest.mark.parametrize("kind", ["outline", "characters"])
@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_wizard_resume_frames_pass_proof_over_tcp(app, client, make_client, fake_llm, monkeypatch, actor, kind):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    client_for = guest if actor == "editor" else client
    partial, head, marker, tail, expected_names = RESUME_CASES[kind]
    source = await _resume_source(app, client_for, world, fake_llm, kind)
    jobs = _capture_jobs(monkeypatch)
    _mount(app)
    gate, end = Gate(), Gate()
    task = await _launch_resume(client_for, world, fake_llm, source, [gate, head, marker, tail, end], kind)
    engine = app.state.task_engine
    await gate.wait_reached()
    prefix = wizard_service.trim_to_resume_point(partial)
    full = prefix + head + marker + tail
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text == prefix)
    async with _serve(app) as base:
        stream = _Stream(base, client_for, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await end.wait_reached()
            await stream.wait_for(lambda events: _caught_up(events, engine.hubs[task["id"]], full))
            end.release()
            events = await stream.finish()
        finally:
            gate.release()
            end.release()
            await stream.aclose()
    names = [event for event, _ in events]
    # 续写前缀（stage「从中断处继续」之后的首个 delta）由快照证明；续写正文逐帧证明。
    assert names[0] == "snapshot" and events[0][1]["text"] == prefix
    assert events[0][1]["task"]["stage"] == "从中断处继续"
    assert names[-1] == "done", events[-1]
    row = await _task_row(app, task["id"])
    assert _text(events) == row.output_text == full
    assert "permission_denied" not in stream.text
    assert row.status == "succeeded" and not row.error
    assert events[-1][1]["result"]["kind"] == kind
    assert await _names(app, world, kind) == expected_names
    job = jobs[task["id"]]
    assert job.resume_epoch == job.guard._stamp.epoch
    assert job.publication_guard[1]._resume_epoch == job.resume_epoch
    if kind == "characters":
        # 承重：整体替换已有角色（甲、乙）使保存推进 epoch，来源 epoch 已不等于当前 epoch，done 帧仍须发布。
        assert job.visibility_stamp.epoch > job.resume_epoch == source.input["ai_history_epoch"]


async def test_failed_wizard_error_frame_is_proven_for_editor(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    task = await _launch(guest, world, "characters", fake_llm, ['[{"name":"半截', gate, RuntimeError("模型中断")])
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
    assert "permission_denied" not in stream.text
    assert await _names(app, world, "characters") == ["乙", "甲"]


# ---------------------------------------------------------------- 撤权矩阵


@pytest.mark.parametrize("notify", ["notify", "silent"])
@pytest.mark.parametrize("change", ["revoke", "viewer", "owner_ai", "password", "reinvite"])
@pytest.mark.parametrize("point", ["delta", "resume_delta", "done"])
async def test_wizard_frame_produced_before_change_is_denied_at_send(
    app, client, make_client, fake_llm, monkeypatch, point, change, notify,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    gate1, gate2 = Gate(), Gate()
    if point == "resume_delta":
        kind, original_names = "outline", []
        source = await _resume_source(app, guest, world, fake_llm)
        marker = RESUME_MARKER
        hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == marker)
        _mount(app, [hold])
        task = await _launch_resume(guest, world, fake_llm, source,
                                    [gate1, RESUME_HEAD, marker, gate2, RESUME_TAIL])
        sent_before = wizard_service.trim_to_resume_point(PARTIAL) + RESUME_HEAD
    else:
        kind, original_names = "characters", ["乙", "甲"]
        head, marker, rest = _pieces(ANSWERS["characters"])
        sent_before = head
        if point == "delta":
            hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == marker)
            chunks = [head, gate1, marker, gate2, rest]
        else:
            # 末尾 gate2：全部增量实际发出后才放行保存，done 是唯一被持住的帧。
            hold = _Hold(lambda item: item[0] == "done")
            chunks = [head, gate1, marker, rest, gate2]
            sent_before = ANSWERS["characters"]
        _mount(app, [hold])
        task = await _launch(guest, world, "characters", fake_llm, chunks)
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
            if point == "done":
                await gate2.wait_reached()
                await stream.wait_for(lambda events: _caught_up(events, engine.hubs[task["id"]], ANSWERS["characters"]))
                gate2.release()
            # 运行体已核验并发布该帧，订阅侧已取得、尚未发送。
            await asyncio.wait_for(hold.reached.wait(), 15)
            if point == "done":
                # 内容与完成收据已提交、终态已写入，done 帧持在订阅侧。
                assert (await _settled(app, task["id"])).status == "succeeded"
            else:
                await gate2.wait_reached()
            await _apply(app, client, guest, world, change)
            if notify == "notify" and change == "password":
                engine.notify_collaboration_change(world.project["id"], world.guest["id"])
            if notify == "silent" and point != "done":
                row = await _task_row(app, task["id"])
                assert row.status == "running" and row.execution_key is not None
            hold.proceed.set()
            events = await stream.finish()
            if notify == "silent" and point != "done":
                # 拒绝来自发送侧：运行体仍阻塞，任务仍为 running。
                assert (await _task_row(app, task["id"])).status == "running"
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            await stream.aclose()
    assert stream.status == 200
    assert events[0][0] == "snapshot" and events[0][1]["text"] == snapshot_text
    assert events[-1] == ("error", DENIED), events
    assert "done" not in [event for event, _ in events]
    assert '"result"' not in stream.text
    # 被持住帧之前已发出的只有快照与其后的增量/进度（续写开头）；被持住帧及之后零泄漏。
    assert {event for event, _ in events[1:-1]} <= {"delta", "progress"}, events
    assert _text(events[:-1]) == sent_before
    if point == "done":
        # done 本身与结果不发出，已提交内容保留（任务仍 succeeded）。
        assert (await _task_row(app, task["id"])).status == "succeeded"
        assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])
        return
    assert marker not in stream.text
    row = await _settled(app, task["id"])
    assert row.status != "succeeded", (row.status, row.error)
    # 撤权后内容从未提交：本步骤内容不变。
    assert await _names(app, world, kind) == original_names


@pytest.mark.parametrize("point", ["delta", "resume_delta", "done"])
async def test_held_wizard_frame_is_sent_when_nothing_changes(
    app, client, make_client, fake_llm, monkeypatch, point,
):
    """正对照：同样持住已产出帧再发送，未改变授权时该帧与后续帧照常发出。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate1, gate2, gate3 = Gate(), Gate(), Gate()
    if point == "resume_delta":
        source = await _resume_source(app, guest, world, fake_llm)
        marker = RESUME_MARKER
        hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == marker)
        _mount(app, [hold])
        task = await _launch_resume(guest, world, fake_llm, source,
                                    [gate1, RESUME_HEAD, marker, gate2, RESUME_TAIL, gate3])
        full = wizard_service.trim_to_resume_point(PARTIAL) + RESUME_HEAD + marker + RESUME_TAIL
    else:
        head, marker, rest = _pieces(ANSWERS["characters"])
        full = ANSWERS["characters"]
        if point == "delta":
            hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == marker)
            # gate3：其余增量实际发出后才放行保存（保存收紧可见性时的在途帧见已知窗口用例）。
            chunks = [head, gate1, marker, gate2, rest, gate3]
        else:
            # 末尾 gate2：全部增量实际发出后才放行保存，done 是唯一被持住的帧。
            hold = _Hold(lambda item: item[0] == "done")
            chunks = [head, gate1, marker, rest, gate2]
        _mount(app, [hold])
        task = await _launch(guest, world, "characters", fake_llm, chunks)
    await gate1.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate1.release()
            if point == "done":
                await gate2.wait_reached()
                await stream.wait_for(lambda events: _caught_up(events, engine.hubs[task["id"]], full))
                gate2.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            if point == "done":
                assert (await _settled(app, task["id"])).status == "succeeded"
            else:
                await gate2.wait_reached()
                assert (await _task_row(app, task["id"])).status == "running"
            hold.proceed.set()
            if point != "done":
                await stream.wait_for(lambda events: any(
                    event == "delta" and data["text"] == marker for event, data in events))
            gate2.release()
            if point != "done":
                await gate3.wait_reached()
                await stream.wait_for(lambda events: _caught_up(events, engine.hubs[task["id"]], full))
                gate3.release()
            events = await stream.finish()
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            gate3.release()
            await stream.aclose()
    assert events[-1][0] == "done", events
    assert events[-1][1]["result"]["kind"] == ("outline" if point == "resume_delta" else "characters")
    assert "permission_denied" not in stream.text
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_notification_closes_idle_wizard_stream_before_next_frame(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    head, middle, rest = _pieces(ANSWERS["characters"])
    task = await _launch(guest, world, "characters", fake_llm, [head, gate, middle, rest])
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
    assert middle not in stream.text
    await _settled(app, task["id"])
    assert await _names(app, world, "characters") == ["乙", "甲"]


# ---------------------------------------------------------------- 只读核对是承重的


async def test_resume_source_changed_after_frame_produced_is_denied(app, client, make_client, fake_llm, monkeypatch):
    """只改续写来源任务（授权、本任务行均不变）：唯有帧租约内的续写来源只读核对能拦截。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    source = await _resume_source(app, guest, world, fake_llm)
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == RESUME_MARKER)
    _mount(app, [hold])
    gate1, gate2 = Gate(), Gate()
    task = await _launch_resume(guest, world, fake_llm, source, [gate1, RESUME_HEAD, RESUME_MARKER, gate2, RESUME_TAIL])
    await gate1.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate1.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            await gate2.wait_reached()
            before = await _task_row(app, task["id"])
            async with app.state.sessionmaker() as db:
                changed = await db.execute(update(Task).where(Task.id == source.id).values(
                    output_text=PARTIAL + "被改写",
                ))
                assert changed.rowcount == 1
                await db.commit()
            after = await _task_row(app, task["id"])
            assert (after.checkpoint, after.status, after.input) == (before.checkpoint, "running", before.input)
            hold.proceed.set()
            events = await stream.finish()
            assert (await _task_row(app, task["id"])).status == "running"
        finally:
            hold.proceed.set()
            gate1.release()
            gate2.release()
            await stream.aclose()
    assert events[-1] == ("error", DENIED), events
    assert {event for event, _ in events[1:-1]} <= {"delta", "progress"}, events
    assert _text(events[:-1]) == wizard_service.trim_to_resume_point(PARTIAL) + RESUME_HEAD
    assert RESUME_MARKER not in stream.text
    await _settled(app, task["id"])


async def test_wizard_pending_not_from_guard_is_denied_by_central_read(app, client, make_client, fake_llm, monkeypatch):
    """伪造未决凭据：guard 不能签发；模拟发布层失守后，中央锁内只读核对仍按 guard 原执行拒绝。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    gate = Gate()
    head, middle, rest = _pieces(ANSWERS["characters"])
    task = await _launch(guest, world, "characters", fake_llm, [head, gate, middle, rest])
    await gate.wait_reached()
    await wait_until(lambda: (hub := engine.hubs.get(task["id"])) is not None and hub.text)
    hub = engine.hubs[task["id"]]
    source = hub.source
    assert type(source) is stream_evidence.PublicationSource
    guard, execution = source.guard, source.execution
    assert publication_execution_matches(guard, execution) and guard._wizard_stream_shape() == "pending"
    checkpoint = execution.checkpoint
    forged_checkpoint = {**checkpoint, "_wizard_pending": {**checkpoint["_wizard_pending"], "nonce": str(uuid4())}}
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
        monkeypatch.setattr("app.services.collaboration_ai_guard.publication_execution_matches",
                            lambda _guard, _execution: True)
        forged_source = stream_evidence.issue_publication(forged, guard, hub=hub)
        data = {"text": "伪造来源的增量"}
        evidence = stream_evidence.frame_evidence("delta", data, forged_source, stamp)
        assert await _send(app, binding, [("delta", data, evidence)]) == [("error", DENIED)]
        # 正对照：恢复原断点后原来源可通过。
        async with app.state.sessionmaker() as db:
            await db.execute(update(Task).where(Task.id == task["id"]).values(checkpoint=checkpoint))
            await db.commit()
        monkeypatch.undo()
        original = stream_evidence.frame_evidence("delta", data, source, stamp)
        assert await _send(app, binding, [("delta", data, original)]) == [("delta", data)]
    finally:
        gate.release()
    assert (await _settled(app, task["id"])).status == "succeeded"


async def test_done_frame_denied_when_visibility_changes_after_save(app, client, make_client, fake_llm, monkeypatch):
    """done 帧持在订阅侧时，作者再改可见性（递增 revision）：仅发布 guard 的新戳不再成立 → 拒绝。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    hold = _Hold(lambda item: item[0] == "done")
    _mount(app, [hold])
    gate = Gate()
    head, middle, rest = _pieces(ANSWERS["outline"])
    task = await _launch(guest, world, "outline", fake_llm, [head, gate, middle, rest])
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            assert (await _settled(app, task["id"])).status == "succeeded"
            async with app.state.sessionmaker() as db:
                await setting_ai_visibility.bump_visibility_revision(db, world.project["id"], tighten=False)
                await db.commit()
            hold.proceed.set()
            events = await stream.finish()
        finally:
            hold.proceed.set()
            gate.release()
            await stream.aclose()
    assert events[-1] == ("error", DENIED), events
    assert '"result"' not in stream.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Outline.title).where(Outline.project_id == world.project["id"])) == "新章纲"


# ---------------------------------------------------------------- 形状与 stream_guard（承重）


def _completion_identity(job, *, drop=None, extra_key=None, keep_pending=False, result_kind=None):
    """按 save_wizard_result 的形状，从原未决凭据派生完成收据身份。"""
    pending = job.pending
    base = dict(pending.active.checkpoint or {})
    marker = base.pop("_wizard_pending")
    result = {"kind": result_kind or job.kind, "items": []}
    receipt = {**marker, "result": result, "result_hash": "r" * 64, "stream_hash": "s" * 64}
    if drop is not None:
        receipt.pop(drop)
    if extra_key is not None:
        receipt[extra_key] = 1
    checkpoint = {**base, "_wizard_completion": receipt}
    if keep_pending:
        checkpoint["_wizard_pending"] = marker
    return execution_identity.advance_checkpoint(pending.active, checkpoint)


@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_wizard_publication_shapes_and_stream_guard(app, client, make_client, fake_llm, monkeypatch, actor):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    jobs = _capture_jobs(monkeypatch)
    client_for = guest if actor == "editor" else client
    task = await _launch(client_for, world, "characters", fake_llm, [ANSWERS["characters"]])
    assert (await _settled(app, task["id"])).status == "succeeded"
    job = jobs[task["id"]]
    handler = WizardHandler()
    pending, completed = job.pending.active, job.execution
    g1 = job.guard
    g2 = job.publication_guard[1]
    assert g1._task is pending and g2._task == completed and g2._publication_only and not g1._publication_only
    assert (g1._wizard_stream_shape(), g2._wizard_stream_shape()) == ("pending", "completed")
    # E6-d：协作执行受理时冻结共享向量选择，两种 guard 都携带同一份。E6-f（裁决 13）翻转：作者任务同样冻结
    # （额外绑定无 grant）；额外绑定的 grant 须与原执行一致，否则不是向导形状。
    assert len(g1._extra_bindings) == 1 and g1._extra_bindings == g2._extra_bindings
    extra = g1._extra_bindings[0]
    assert type(extra) is project_ai_config.ResolvedSharedEmbedding
    assert extra.grant_identity == pending.grant_identity
    assert (extra.grant_identity is None) == (actor == "owner")
    # 任务输入带选择时，缺少该额外绑定的 guard 不能构造。
    with pytest.raises(CollaborationSendBlocked):
        for_task(pending, g1._binding, g1._stamp, cipher=g1._cipher, allow_private=g1._allow_private,
                 resume=g1._resume)
    forged_grant = None if actor == "editor" else (str(uuid4()), 1)
    assert replace(g1, _extra_bindings=(replace(extra, grant_identity=forged_grant),))._wizard_stream_shape() is None
    assert g1._stream_readable() and g2._stream_readable()
    # 只认逐字相等：未决 guard 不能签发完成断点，完成 guard 也不能签发未决断点。
    assert publication_execution_matches(g1, pending) and publication_execution_matches(g2, completed)
    assert not publication_execution_matches(g1, completed) and not publication_execution_matches(g2, pending)
    assert not publication_execution_matches(g1, job.pending.original)
    # 未决凭据被换掉（nonce / execution_key / input_hash）：按该断点构造的 guard 不可读、不可发布。
    marker = pending.checkpoint["_wizard_pending"]
    for key, value in (("execution_key", str(uuid4())), ("task_key", str(uuid4())), ("input_hash", "0" * 64),
                       ("actor_id", 999999), ("kind", "wizard.world"), ("version", 2)):
        forged = execution_identity.advance_checkpoint(pending, {"_wizard_pending": {**marker, key: value}})
        guard = for_task(forged, g1._binding, g1._stamp, cipher=g1._cipher, allow_private=g1._allow_private,
                         resume=g1._resume, extra_bindings=g1._extra_bindings)
        assert guard._wizard_stream_shape() is None and not guard._stream_readable(), key
        assert not publication_execution_matches(guard, forged), key
    # 无凭据的原领取断点也不是向导发布形状。
    plain = for_task(job.pending.original, g1._binding, g1._stamp, cipher=g1._cipher,
                     allow_private=g1._allow_private, resume=g1._resume, extra_bindings=g1._extra_bindings)
    assert not plain._stream_readable() and not publication_execution_matches(plain, job.pending.original)
    # 仅发布 guard 只能绑定带完成收据的断点；收据缺键、夹带键、仍带未决凭据、结果类型不符都拒绝。
    ok = _completion_identity(job)
    assert for_task(ok, g2._binding, g2._stamp, cipher=g2._cipher, allow_private=g2._allow_private,
                    resume=g2._resume, publication_only=True,
                    extra_bindings=g2._extra_bindings)._wizard_stream_shape() == "completed"
    bad = {"pending_only": pending, "plain": job.pending.original,
           **{f"drop_{key}": _completion_identity(job, drop=key) for key in sorted(_WIZARD_RECEIPT_FIELDS)},
           "extra": _completion_identity(job, extra_key="grant"),
           "both": _completion_identity(job, keep_pending=True),
           "kind": _completion_identity(job, result_kind="world")}
    for name, identity in bad.items():
        with pytest.raises(CollaborationSendBlocked):
            for_task(identity, g2._binding, g2._stamp, cipher=g2._cipher, allow_private=g2._allow_private,
                     resume=g2._resume, publication_only=True, extra_bindings=g2._extra_bindings)
        assert name
    # 未决 guard 夹带其他来源即不再是向导形状。
    sentinel = object()
    for field in ("_chapter", "_document_claim", "_entity_sources", "_analysis_execution", "_scene"):
        assert replace(g1, **{field: sentinel})._wizard_stream_shape() is None, field
    assert replace(g1, _embedding_inputs=())._wizard_stream_shape() is None
    assert replace(g1, _extra_bindings=(g1._binding,))._wizard_stream_shape() is None
    assert replace(g1, _publication_only=True)._wizard_stream_shape() is None
    # 仅发布 guard 不能发送、预检或写入。
    with pytest.raises(CollaborationSendBlocked):
        await g2.prepare_provider(app.state.sessionmaker, app.state.task_engine.ctx.factory, request_timeout=5)
    async with app.state.sessionmaker() as db:
        with pytest.raises(CollaborationSendBlocked):
            await g2.require_task_write(db)
        with pytest.raises(CollaborationSendBlocked):
            await g2.verify(db)
    # stream_guard：done 交出缓存的仅发布 guard；非 done、结果不符、guard 被替换、戳被替换均拒绝。
    done = completed.checkpoint["_wizard_completion"]["result"]
    assert handler.stream_guard(job, execution=job.pending.original, current_execution=completed,
                                event="done", data=done) is g2
    with pytest.raises(CollaborationSendBlocked):
        handler.stream_guard(job, execution=job.pending.original, current_execution=completed,
                             event="delta", data={"text": "x"})
    with pytest.raises(ConflictError) as mismatch:
        handler.stream_guard(job, execution=job.pending.original, current_execution=completed,
                             event="done", data={**done, "items": []})
    assert mismatch.value.extra["code"] == "wizard_result_uncertain"
    # 完成收据与原未决凭据逐键核对：真实收据缺任一键、夹带键或改值，stream_guard 都不交出 guard。
    real = completed.checkpoint["_wizard_completion"]
    variants = {**{f"drop_{key}": {k: v for k, v in real.items() if k != key} for key in sorted(real)},
                "extra": {**real, "grant": None}, "nonce": {**real, "nonce": str(uuid4())},
                "actor_type": {**real, "actor_id": str(real["actor_id"])}}
    for name, receipt in variants.items():
        job.execution = execution_identity.advance_checkpoint(
            completed, {**completed.checkpoint, "_wizard_completion": receipt},
        )
        with pytest.raises(ConflictError) as rejected:
            handler.stream_guard(job, execution=job.pending.original, current_execution=job.execution,
                                 event="done", data=done)
        # 由 verify_wizard_completion 自身拒绝（不只靠随后 for_task 的形状校验兜底）。
        assert rejected.value.extra["code"] == "wizard_result_uncertain", name
    job.execution = completed
    stamp = job.visibility_stamp
    job.visibility_stamp = replace(stamp)  # 等值但不同对象：按绑定变化重建，而不是交出旧缓存
    rebuilt = handler.stream_guard(job, execution=job.pending.original, current_execution=completed,
                                   event="done", data=done)
    assert rebuilt is not g2 and rebuilt._stamp is job.visibility_stamp
    job.visibility_stamp = stamp
    original_guard = job.guard
    job.guard = g2
    with pytest.raises(CollaborationSendBlocked):
        handler.stream_guard(job, execution=job.pending.original, current_execution=completed,
                             event="done", data=done)
    job.guard = original_guard
    # 未决阶段：只交出构建 guard 本身；可见性戳被替换为等值新对象即拒绝。
    job.execution, job.visibility_stamp = pending, g1._stamp
    assert handler.stream_guard(job, execution=job.pending.original, current_execution=pending,
                                event="delta", data={"text": "x"}) is g1
    job.visibility_stamp = replace(g1._stamp)
    with pytest.raises(CollaborationSendBlocked):
        handler.stream_guard(job, execution=job.pending.original, current_execution=pending,
                             event="delta", data={"text": "x"})


# ---------------------------------------------------------------- 同执行戳后继（2026-11-13，原已知窗口已翻转）


async def _existing_entries(client, world, kind):
    """作品已有该类条目（_world 自带角色甲、乙；世界观/职业另建），向导整体替换会收紧可见性。"""
    if kind == "world":
        created = await client.post(f"{world.base}/world", json={"title": "旧山门", "content": "云海"})
        assert created.status_code == 201, created.text
    elif kind == "careers":
        created = await client.post(f"{world.base}/careers", json={"name": "旧修", "stages": [{"name": "一"}]})
        assert created.status_code == 201, created.text


def _spy_successor(monkeypatch) -> list[bool]:
    """记录中央校验器是否经「同执行戳后继」分支（仍调用真实实现）。"""
    calls: list[bool] = []
    original = stream_evidence._verify_wizard_successor

    async def spy(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(stream_evidence, "_verify_wizard_successor", spy)
    return calls


@pytest.mark.parametrize(("kind", "actor", "existing"), [
    ("characters", "editor", True), ("world", "editor", True), ("careers", "editor", True),
    ("characters", "owner", True), ("outline", "editor", True), ("world", "editor", False),
])
async def test_running_frame_in_flight_across_own_tightening_save_is_sent(
    app, client, make_client, fake_llm, monkeypatch, kind, actor, existing,
):
    """未决阶段的帧在本任务保存提交后才发送（原 E5-c3 已知窗口，2026-11-13 翻转）。

    设定类向导整体替换已有条目时保存本身收紧可见性（递增 revision/epoch）：帧投影戳与构建 guard 的戳都不再成立，
    改由同一 hub 登记的本执行完成仅发布 guard（保存事务内的新戳）在帧租约锁内复核，帧照常发出、最终 done。
    大纲及无已有条目的首次设定保存不改可见性，不进入后继分支，照旧经终态证明发出。
    """
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    if existing:
        await _existing_entries(client, world, kind)
    else:
        assert kind == "world"
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *_args, **_kwargs: None)
    calls = _spy_successor(monkeypatch)
    head, marker, rest = _pieces(ANSWERS[kind])
    hold = _Hold(lambda item: item[0] == "delta" and item[1].get("text") == marker)
    _mount(app, [hold])
    gate, gate2 = Gate(), Gate()
    client_for = guest if actor == "editor" else client
    async with app.state.sessionmaker() as db:
        before = await read_visibility_stamp(db, world.project["id"])
    task = await _launch(client_for, world, kind, fake_llm, [head, gate, marker, gate2, rest])
    await gate.wait_reached()
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.text)
    async with _serve(app) as base:
        stream = _Stream(base, client_for, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            gate.release()
            await asyncio.wait_for(hold.reached.wait(), 15)
            gate2.release()
            # 运行体不等订阅：保存、完成收据与终态都已提交后才放行被持住的帧。
            assert (await _settled(app, task["id"])).status == "succeeded"
            hold.proceed.set()
            events = await stream.finish()
        finally:
            hold.proceed.set()
            gate.release()
            gate2.release()
            await stream.aclose()
    async with app.state.sessionmaker() as db:
        after = await read_visibility_stamp(db, world.project["id"])
    assert events[-1][0] == "done", events
    assert events[-1][1]["result"]["kind"] == kind
    assert "permission_denied" not in stream.text
    assert _text(events) == ANSWERS[kind]
    assert await _names(app, world, kind) == sorted(NEW_NAMES[kind])
    if kind != "outline" and existing:
        # 保存确实收紧，且帧经后继分支证明（承重：去掉后继分支即 permission_denied）。
        assert after != before and after.epoch > before.epoch
        assert calls and all(calls), calls
    else:
        assert after == before
        assert calls == []


# ---------------------------------------------------------------- 首个事件前订阅（E5-e，原已知窗口翻转）


async def test_subscription_before_first_verified_wizard_event_gets_empty_snapshot(
    app, client, make_client, fake_llm,
):
    """E5-e：领取后、首个事件核验前（等待模型首个 token）开始的受控订阅先得到无正文快照，之后照常到 done。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    _mount(app)
    gate = Gate()
    task = await _launch(guest, world, "characters", fake_llm, [gate, ANSWERS["characters"]])
    await gate.wait_reached()
    hub = app.state.task_engine.hubs[task["id"]]
    assert type(hub.source) is stream_evidence.ClaimSource and hub.text == ""
    async with _serve(app) as base:
        stream = _Stream(base, guest, task)
        try:
            await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
            assert stream.events()[0][1]["text"] == "" and "新角色" not in stream.text
            gate.release()
            events = await stream.finish()
        finally:
            gate.release()
            await stream.aclose()
    assert events[0][0] == "snapshot" and events[-1][0] == "done", events
    assert "permission_denied" not in stream.text
    assert _text(events) == ANSWERS["characters"]
    assert (await _settled(app, task["id"])).status == "succeeded"


def test_answers_are_valid_json():
    for answer in ANSWERS.values():
        json.loads(answer)
    for partial, head, marker, tail, _expected in RESUME_CASES.values():
        json.loads(wizard_service.trim_to_resume_point(partial) + head + marker + tail)
