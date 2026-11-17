"""一致性检查的协作权限（C10 / AC7）：查看者只读、非协作者 404、编辑者可发起（本人模型 / 作者模型授权），
以及受理后领取前、构建前、发送前、保存前撤权 / 降级 / 关作者模型授权时失败关闭：
零后续发送、不再保存结果、任务不停在 running、活动资源键释放，已保存的台账保留。

协作世界与撤权动作复用大纲协作测试的搭建（同一套受控受理 / 发送 lease / 写回重验模板）。
"""

import asyncio

import pytest
from sqlalchemy import func, select, update

from app.models import ConsistencyIssue, ConsistencyLedger, ConsistencyRun, Project, Task, User
from app.services.consistency import check
from app.services.task_engine.registry import get_handler
from tests.consistency_helpers import comparison, extraction
from tests.helpers import Gate, final_event, parse_sse, stream_task, stream_with_gate, streamed_text
from tests.test_collaboration_outline_entry_e4g3 import (
    EDITOR_KEY,
    MESSAGES,
    OWNER_KEY,
    STOPPED,
    _change,
    _elsewhere,
    _terminal,
    _world,
)

BROKEN = "😀 林远的佩剑在山门前断成两截。"
USED = "林远拔出佩剑，一剑逼退来敌。"
OTHER = "苏婉在药庐里守了一夜。"
CONTENTS = {1: BROKEN, 2: USED, 3: OTHER}


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def _sword(quote: str) -> str:
    return extraction({"category": "item", "subject": "佩剑", "statement": quote, "quote": quote})


def _finding(chapters: dict) -> str:
    return comparison({
        "existing_issue_id": None, "category": "item", "title": "佩剑去向存在潜在冲突", "description": "已毁仍用",
        "suggestion": "补写重铸", "severity": "high", "foreshadow_id": None,
        "evidence": [{"chapter_id": chapters[number]["id"], "fact_id": "1"} for number in (1, 2)],
    })


async def _chapters(owner, world, contents=CONTENTS) -> dict[int, dict]:
    chapters = {}
    for number, content in contents.items():
        resp = await owner.post(f"{world.base}/chapters",
                                json={"title": f"章{number}", "chapter_no": number, "content": content})
        assert resp.status_code == 201, resp.text
        chapters[number] = resp.json()
    return chapters


def _start(client, world, **body):
    return client.post(f"{world.base}/consistency/checks", json={"mode": "all", **body})


async def _state(app, world):
    async with app.state.sessionmaker() as db:
        project_id = world.project["id"]
        ledgers = set(await db.scalars(select(ConsistencyLedger.chapter_id).where(
            ConsistencyLedger.project_id == project_id)))
        issues = await db.scalar(select(func.count(ConsistencyIssue.id)).where(
            ConsistencyIssue.project_id == project_id))
        runs = list(await db.scalars(select(ConsistencyRun).where(ConsistencyRun.project_id == project_id)
                                     .order_by(ConsistencyRun.id)))
        return ledgers, issues, runs


async def _assert_released(app, owner, world, fake_llm, responses):
    """活动资源键已释放：作者可立即对同一作品发起检查并成功。"""
    for response in responses:
        fake_llm.push(response)
    author = await _start(owner, world)
    assert author.status_code == 202, author.text
    row = await _terminal(app, author.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.owner["id"] and "_collaboration_grant" not in row.input


async def _add_issue(app, world, chapters) -> int:
    async with app.state.sessionmaker() as db:
        issue = ConsistencyIssue(
            project_id=world.project["id"], category="item", title="旧问题", severity="medium", status="open",
            identity_key="a" * 64,
            evidence=[{"chapter_id": chapters[1]["id"], "fact_id": "1", "quote": "佩剑", "start": 5, "end": 7,
                       "text_hash": "0" * 64}],
        )
        db.add(issue)
        await db.commit()
        return issue.id


# ---------------- 读写面 ----------------


@pytest.mark.parametrize("case", ["viewer", "pending"])
async def test_viewer_is_read_only_and_non_members_get_404(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(client, guest, role="viewer", accept=case == "viewer", allow_owner_ai=True)
    chapters = await _chapters(client, world)
    issue_id = await _add_issue(app, world, chapters)
    base = f"{world.base}/consistency"
    reads = [f"{base}/summary", f"{base}/issues", f"{base}/issues/{issue_id}"]
    writes = [
        ("patch", f"{base}/issues/{issue_id}", {"revision": 1, "status": "ignored"}),
        ("post", f"{base}/checks/preview", {"mode": "all"}),
        ("post", f"{base}/checks", {"mode": "all"}),
        ("post", f"{base}/issues/{issue_id}/recheck", None),
    ]
    for path in reads:
        resp = await guest.get(path)
        assert resp.status_code == (200 if case == "viewer" else 404), (path, resp.text)
    for method, path, body in writes:
        resp = await getattr(guest, method)(path, **({"json": body} if body is not None else {}))
        if case == "viewer":
            assert resp.status_code == 403 and resp.json()["code"] == "project_readonly", (path, resp.text)
        else:
            assert resp.status_code == 404, (path, resp.text)
        assert "佩剑" not in resp.text
    if case == "viewer":
        detail = (await guest.get(f"{base}/issues/{issue_id}")).json()
        assert detail["evidence"][0]["quote"] == "佩剑" and detail["status"] == "open"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        assert (await db.get(ConsistencyIssue, issue_id)).revision == 1
    assert fake_llm.calls == []


async def test_admin_manual_access_does_not_extend_to_ai_entries(app, client, make_client, fake_llm):
    """管理员的人工内容权限沿用 require_project 的既有口径（可读清单）；发起检查属 ai.use，不对其开放。"""
    guest = make_client()
    world = await _world(client, guest, accept=False, allow_owner_ai=True)
    chapters = await _chapters(client, world)
    issue_id = await _add_issue(app, world, chapters)
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == world.guest["id"]).values(is_admin=True))
        await db.commit()
    base = f"{world.base}/consistency"
    assert (await guest.get(f"{base}/issues")).status_code == 200
    for path, body in ((f"{base}/checks/preview", {"mode": "all"}), (f"{base}/checks", {"mode": "all"}),
                       (f"{base}/issues/{issue_id}/recheck", None)):
        resp = await guest.post(path, **({"json": body} if body is not None else {}))
        assert resp.status_code == 404, (path, resp.text)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
    assert fake_llm.calls == []


async def test_editor_runs_check_with_own_model_and_can_triage(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    preview = await guest.post(f"{world.base}/consistency/checks/preview", json={"mode": "all"})
    assert preview.status_code == 200 and preview.json()["to_extract"] == 2
    fake_llm.push(_sword("佩剑在山门前断成两截"))
    fake_llm.push(_sword("拔出佩剑"))
    fake_llm.push(_finding(chapters))
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"] and row.input["_collaboration_grant"]["invitation_id"]
    # 本人有文本配置：即使作者授权开启也只用本人密钥
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY] * 3
    assert (row.input["_project_model"]["source"], row.input["_project_model"]["config_owner_id"]) == (
        "actor", world.guest["id"],
    )
    ledgers, issues, runs = await _state(app, world)
    assert ledgers == {chapters[1]["id"], chapters[2]["id"]} and issues == 1
    assert (runs[0].status, runs[0].actor_id) == ("succeeded", world.guest["id"])
    # 发起者本人可见 task_key；作者只看到有检查在进行 / 已完成的概要，不拿他人任务的 task_key
    listed = (await guest.get(f"{world.base}/consistency/issues")).json()
    issue = listed["items"][0]
    saved = await guest.patch(f"{world.base}/consistency/issues/{issue['id']}",
                              json={"revision": issue["revision"], "status": "resolved", "note": "已补写"})
    assert saved.status_code == 200 and saved.json()["status_changed_by"] == world.guest["id"]
    assert (await client.get(f"{world.base}/consistency/summary")).json()["counts_by_status"]["resolved"] == 1
    # 已完成的结果不因随后撤权回滚
    await _change(app, client, world, "revoked")
    assert (await _state(app, world))[:2] == (ledgers, 1)
    assert (await guest.get(f"{world.base}/consistency/issues")).status_code == 404


async def test_editor_owner_model_fallback_records_cost_source(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    gate = Gate()
    fake_llm.push([gate, _sword("佩剑在山门前断成两截")])
    fake_llm.push(_sword("拔出佩剑"))
    fake_llm.push(_finding(chapters))
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    # 进行中：作者的概要只有任务 id，task_key 只交给发起者本人
    owner_view = (await client.get(f"{world.base}/consistency/summary")).json()
    guest_view = (await guest.get(f"{world.base}/consistency/summary")).json()
    assert owner_view["running_task_id"] == created.json()["id"] and owner_view["running_task_key"] is None
    assert guest_view["running_task_key"] == created.json()["task_key"]
    blocked = await _start(client, world)
    assert blocked.status_code == 409 and blocked.json()["detail"] == check.OPEN_CHECK
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY] * 3
    model = row.input["_project_model"]
    assert (model["source"], model["config_owner_id"], model["config_id"]) == (
        "owner", world.owner["id"], world.owner_config["id"],
    )
    detail = await guest.get(f"/api/v1/tasks/{row.id}", params={"task_key": row.statistics_key})
    assert detail.status_code == 200 and "_project_model" not in detail.text and OWNER_KEY not in detail.text


@pytest.mark.parametrize("case", ["no_model_no_grant", "explicit_owner_config"])
async def test_editor_model_errors_never_fall_back(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=case != "no_model_no_grant", editor_model=False)
    await _chapters(client, world, {1: BROKEN, 2: USED})
    body = {"ai_config_id": world.owner_config["id"]} if case == "explicit_owner_config" else {}
    resp = await _start(guest, world, **body)
    assert resp.status_code in (400, 404), resp.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
    assert fake_llm.calls == []


async def test_editor_stream_uses_guarded_pipeline(app, client, make_client, fake_llm):
    """协作者订阅走受控任务流：每帧按原绑定证明来源；输出只有进度日志与概要。撤权后的重订阅 404。"""
    guest = make_client()
    world = await _world(client, guest)
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    gate = Gate()
    raw = _sword("佩剑在山门前断成两截")
    # 阻塞点放在首个分片之后：发送 lease 已结束，订阅帧不与之争用 SQLite 库锁
    fake_llm.push([raw[:1], gate, raw[1:]])
    fake_llm.push(_sword("拔出佩剑"))
    fake_llm.push(_finding(chapters))
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    resp = await stream_with_gate(app, guest, created.json(), gate)
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    name, final = events[-1]
    assert name == "done" and final["task"]["status"] == "succeeded", events[-3:]
    assert final["result"]["created"] == 1
    assert streamed_text(events) == "第 1/2 章：抽取 1 条事实\n第 2/2 章：抽取 1 条事实\n比对 1/1 批：完成\n"
    assert "_project_model" not in str(events) and "_collaboration_grant" not in str(events)
    # 已结束任务的回放
    replay = await stream_task(guest, created.json())
    assert final_event(replay)[0] == "done"
    # 作者不能订阅协作者的任务
    foreign = await client.get(f"/api/v1/tasks/{created.json()['id']}/stream",
                               params={"task_key": created.json()["task_key"]})
    assert foreign.status_code == 404
    await _change(app, client, world, "revoked")
    gone = await guest.get(f"/api/v1/tasks/{created.json()['id']}/stream",
                           params={"task_key": created.json()["task_key"]})
    assert gone.status_code == 404


# ---------------- 四个时点的撤权 ----------------


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct", "disabled"])
async def test_change_after_admission_before_claim_fails_closed(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    await _change(app, client, world, change)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    expected = {"disabled": "collaboration_account_unavailable", "owner_ai_direct": "collaboration_model_revoked"}
    assert row.status == "failed" and row.error == MESSAGES[expected.get(change, "collaboration_revoked")]
    assert row.result is None and fake_llm.calls == []
    assert await _state(app, world) == (set(), 0, [])
    await _assert_released(app, client, world, fake_llm,
                           [_sword("佩剑在山门前断成两截"), _sword("拔出佩剑"), _finding(chapters)])


@pytest.mark.parametrize("stage", ["build", "send"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_change_after_claim_before_first_send_never_sends(app, client, make_client, fake_llm, monkeypatch,
                                                                stage, change):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    handler = get_handler("consistency.check")
    original_build = type(handler).build_job
    original_complete = check.complete
    fired, changes = asyncio.Event(), []
    revoke = _elsewhere(app, client, world, change, changes)

    async def before_build(self, ctx, task, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_build(self, ctx, task, **kwargs)

    async def before_send(target, messages):
        # 首次发送前（job 已构建、消息已组装，尚未进入发送 lease）
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_complete(target, messages)

    if stage == "build":
        monkeypatch.setattr(type(handler), "build_job", before_build)
    else:
        monkeypatch.setattr(check, "complete", before_send)
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired.is_set()
    assert row.status in ("failed", "cancelled") and row.result is None, (row.status, row.error)
    if row.status == "failed" and stage == "send":
        # 构建前的锁内重验沿用执行身份屏障的既有文案（同大纲任务），这里只核对发送门禁的固定文案
        assert row.error in STOPPED, row.error
    assert row.output_text == "" and fake_llm.calls == []
    ledgers, issues, runs = await _state(app, world)
    assert (ledgers, issues) == (set(), 0) and all(run.status != "running" for run in runs)
    await _assert_released(app, client, world, fake_llm,
                           [_sword("佩剑在山门前断成两截"), _sword("拔出佩剑"), _finding(chapters)])


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "owner_ai_direct", "reinvite"])
async def test_change_during_send_stops_and_saves_nothing_more(app, client, make_client, fake_llm, change):
    """第 1 章台账已保存、第 2 章发送中撤权：第 2 章结果不落库、不再发送后续章节与比对，已保存的台账保留。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not change.startswith("owner_ai"))
    chapters = await _chapters(client, world)
    gate = Gate()
    fake_llm.push(_sword("佩剑在山门前断成两截"))
    fake_llm.push([gate, _sword("拔出佩剑")])
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    changes: list = []
    await _elsewhere(app, client, world, change, changes)()
    gate.release()
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert row.status in ("failed", "cancelled") and row.result is None, (row.status, row.error)
    if row.status == "failed":
        assert row.error in STOPPED, row.error
    assert len(fake_llm.calls) == 2
    ledgers, issues, runs = await _state(app, world)
    assert ledgers == {chapters[1]["id"]} and issues == 0
    assert len(runs) == 1 and runs[0].status in ("failed", "cancelled") and runs[0].stats["extracted"] == 1
    last_run = (await client.get(f"{world.base}/consistency/summary")).json()["last_run"]
    assert last_run["status"] == row.status
    # 作者重新检查：第 1 章台账直接复用
    await _assert_released(app, client, world, fake_llm,
                           [_sword("拔出佩剑"), extraction(), _finding(chapters)])
    assert len(fake_llm.calls) == 5


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_change_before_merge_saves_no_issues(app, client, make_client, fake_llm, monkeypatch, change):
    """比对已返回、合并事务开始前撤权：锁内重验失败，不写任何问题；台账保留。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    original_complete = check.complete
    changes: list = []
    revoke = _elsewhere(app, client, world, change, changes)
    sent = 0

    async def change_after_compare(target, messages):
        nonlocal sent
        text = await original_complete(target, messages)
        sent += 1
        if sent == 3:
            # 两章抽取与一次比对都已返回，合并事务尚未开始
            await revoke()
        return text

    monkeypatch.setattr(check, "complete", change_after_compare)
    fake_llm.push(_sword("佩剑在山门前断成两截"))
    fake_llm.push(_sword("拔出佩剑"))
    fake_llm.push(_finding(chapters))
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert sent == 3 and changes
    assert row.status in ("failed", "cancelled") and row.result is None, (row.status, row.error)
    assert len(fake_llm.calls) == 3
    ledgers, issues, runs = await _state(app, world)
    assert ledgers == {chapters[1]["id"], chapters[2]["id"]} and issues == 0
    assert runs[0].status in ("failed", "cancelled")
    await _assert_released(app, client, world, fake_llm, [_finding(chapters)])


@pytest.mark.parametrize("who", ["editor", "author"])
async def test_change_after_merge_before_terminal_releases_the_task(app, client, make_client, fake_llm,
                                                                    monkeypatch, who):
    """完成事件已发布、终态事务前授权或设置变化：任务改记无结果失败并释放 consistency:{project}，不停在 running；
    合并事务已提交的问题与台账保留，运行记录仍为已完成。"""
    guest = make_client()
    world = await _world(client, guest)
    chapters = await _chapters(client, world, {1: BROKEN, 2: USED})
    engine = app.state.task_engine
    engine_cls = type(engine)
    original_complete = engine_cls.complete_state
    fired = []

    async def change():
        if who == "editor":
            await _change(app, client, world, "viewer")
        else:
            async with app.state.sessionmaker() as db:
                await db.execute(update(Project).where(Project.id == world.project["id"]).values(
                    ai_visibility_revision=Project.ai_visibility_revision + 1,
                ))
                await db.commit()

    async def before_final(self, task, **kwargs):
        if not fired and task is not None and task.kind == "consistency.check" and kwargs.get("status") == "succeeded":
            fired.append(who)
            await asyncio.shield(asyncio.ensure_future(change()))
        return await original_complete(self, task, **kwargs)

    monkeypatch.setattr(engine_cls, "complete_state", before_final)
    fake_llm.push(_sword("佩剑在山门前断成两截"))
    fake_llm.push(_sword("拔出佩剑"))
    fake_llm.push(_finding(chapters))
    created = await _start(guest if who == "editor" else client, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == [who]
    assert row.status == "failed" and row.result is None, (row.status, row.error)
    assert row.error in STOPPED
    assert not engine.has_cleanup_failure(world.project["id"])
    ledgers, issues, runs = await _state(app, world)
    assert ledgers == {chapters[1]["id"], chapters[2]["id"]} and issues == 1
    assert runs[0].status == "succeeded"
    last_run = (await client.get(f"{world.base}/consistency/summary")).json()
    assert last_run["last_run"]["status"] == "succeeded" and last_run["running_task_id"] is None
    await _assert_released(app, client, world, fake_llm, [_finding(chapters)])
