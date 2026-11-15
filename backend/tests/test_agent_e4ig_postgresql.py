"""E4i-g：协作者 Agent 在真实 PostgreSQL 上的并发补缺（research e4i-agent-inventory §9.2）。

本文件只补已有 PG 用例未覆盖的组合（覆盖映射见 implement.md「进度：E4i-g」）：

1. 发消息受理 vs 撤权：撤权先提交（入口写锁前停住）→ 404 / 409、零任务零消息；受理先持锁（中央 create_task 前停住）
   → 撤权在 PostgreSQL 上被阻塞，受理先提交，随后领取锁内受限收尾 failed、零模型请求。
2. 模型真实 HTTP 发送 lease（本机 SDK 服务器，OpenAI 兼容）vs 撤权 / 降为 viewer / 关闭作者模型授权 / 令牌撤销：
   发送前提交 → 零 HTTP 请求；发送中（请求已到达、响应头前）→ 撤权被 lease 阻塞，响应头后才提交，已发出的 1 次请求
   不能撤回但其输出不落库、轮次失败。主对话与会话压缩摘要（裁决 13）各一组。
3. 后台自动执行写工具 vs 撤权：撤权先提交（模型流中停住）→ 工具不执行、轮次失败；工具先持锁 → 撤权被阻塞，
   工具合法结果保留、之后零模型请求。管理员兼编辑者降为 viewer：工具不执行，且不产生任何管理审计（授权失效在进入
   工具 service 的审计作用域之前即终止轮次：引擎事件输出围栏 / 执行体锁后复核；见 implement.md 偏差说明）；
   工具先持锁时恰好一条 project.update 成功审计。
4. HTTP 确认 vs 撤权（含 start_task → execute_admitted 派生任务）：撤权先提交 → 拒绝、零派生任务；确认先持锁 →
   派生任务已提交，撤权后由领取锁内受限收尾 failed、零模型请求、章节占用释放。确认请求被客户端取消 → 整体回滚。
5. 撤销 vs 撤权（两种顺序）；撤销 vs 作者人工编辑（effects.undo 行级 after 值比对 → 409 undo_conflict，人工值保留；
   preflight_effect_versions 只校验效果格式/可见性版本，不负责此场景）。
6. 编辑者 MCP 关闭（裁决 2/15）：入口 403；残留外部提案确认只记失败、零外部调用、不建未知结果屏障，作者删除作品
   不被阻挡（作者侧未知结果屏障的 PG 竞争已由 test_agent_external_deletion_guard_postgresql 等覆盖）。
7. 摘要发送 vs 撤权：并入第 2 组 phase=summary。
8. 编辑者确认路径的真实 REPEATABLE READ 40001 → 安全 409（CONCURRENT_CHANGE），不泄露 SQL，提案保持待确认。
9. 受限收尾先持锁 vs 编辑者本人确认：确认被阻塞，放行后 409、提案已作废、轮次 failed。
10. E7 账目在 PostgreSQL（Agent 会话 REPEATABLE READ）下的对照：复用 test_agent_e2e_e4ig.check_request_ledger。

编排只用 Event 与 pg_blocking_pids，不用固定等待。
"""

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, text, update

from app.ai.providers import create_provider
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.models import (
    AdminAuditLog,
    AgentChange,
    AgentConversation,
    AgentMessage,
    AIConfig,
    Chapter,
    Project,
    ProjectCollaborator,
    Task,
    User,
)
from app.services import authorization_service as authz
from app.services import collaboration_ai_guard, project_service, task_service
from app.services.agent import access as agent_access
from app.services.agent import service, transactions
from app.services.task_engine import engine as engine_module
from tests import pg_support
from tests.helpers import create_ai_config, create_project, register, wait_task
from tests.test_agent_entries_e4id import _detail, _invite, _new_conversation, _send
from tests.test_agent_mcp_skills import change_action
from tests.test_agent_providers import openai_chunk
from tests.test_agent_revocation_e4ie import COLLAB_FAILED, STOP, TERMINAL, VOIDED, _apply, _changes, _task, _wait_db
from tests.test_agent_revocation_e4ie import _world as _e4ie_world
from tests.test_agent_runtime import call
from tests.test_ai_visibility_dispatch import _server

KINDS = ["revoke", "viewer", "model_off", "token"]
# 授权失效的受控失败文案：引擎事件 / 发送围栏（task_output_changed / permission_denied）；令牌撤销时收尾认证也失败，
# 改为系统受限收尾的固定文案。只断言 status=failed 会让“服务器内部错误”等无关故障也通过。
AUTH_STOPPED = {
    engine_module._POLICY_MESSAGES["task_output_changed"], engine_module._POLICY_MESSAGES["permission_denied"],
}
TOKEN_STOPPED = "任务执行已停止"
SECRET = "撤权后的秘密输出"


@pytest.fixture
async def pg_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="Agent E4i-g 并发测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2, start_first_engine=True,
    ) as apps:
        yield apps


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _blocked_by(app, holder_pid, work):
    """直到有会话被 holder 阻塞（pg_blocking_pids），或观察的工作已提前结束。"""
    while True:
        async with app.state.sessionmaker() as db:
            blocked = await db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE :holder = ANY(pg_blocking_pids(pid))"
            ), {"holder": holder_pid})
        if blocked or work.done():
            return
        await asyncio.sleep(0.01)


async def _settle(*works):
    for work in works:
        if work is not None and not work.done():
            work.cancel()
    await asyncio.gather(*(work for work in works if work is not None), return_exceptions=True)


async def _change(world, kind):
    """kind ∈ revoke / viewer / model_off / token；token 为编辑者原令牌撤销（不经作者）。"""
    if kind == "token":
        async with world.owner_app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.editor["id"]).values(
                token_version=User.token_version + 1,
            ))
            await db.commit()
        return
    await _apply(world, kind)


async def _member(app, world):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"],
            ProjectCollaborator.user_id == world.editor["id"],
        ))


async def _editor_tasks(app, world, kind="agent.turn"):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(Task).where(
            Task.user_id == world.editor["id"], Task.kind == kind,
        ).order_by(Task.id)))


async def _messages(app, cid):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(AgentMessage).where(
            AgentMessage.conversation_id == cid,
        ).order_by(AgentMessage.id)))


async def _pg_world(editor_app, owner_app, editor_client, owner_client):
    """作者经第二个实例（独立连接）操作；编辑者经第一个实例（运行任务引擎），本人无模型、作者授权回退。"""
    world = await _e4ie_world(editor_app, owner_client, lambda: editor_client)
    world.owner_app, world.editor_app = owner_app, editor_app
    return world


# ---------------- 1. 发消息受理 vs 撤权 ----------------


@pytest.mark.parametrize("kind", ["revoke", "viewer"])
async def test_pg_send_after_committed_revocation_is_denied(pg_apps, fake_llm, monkeypatch, kind):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.owned_conversation

        async def gated(db, user_id, project_id, conversation_id, **kwargs):
            # 锁外初检、模型预检与受理证明已完成；会话写锁之前停住，撤权在此期间提交。
            if user_id == world.editor["id"] and kwargs.get("lock") and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(db, user_id, project_id, conversation_id, **kwargs)

        monkeypatch.setattr(service, "owned_conversation", gated)
        calls = len(fake_llm.tool_calls)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/conversations/{world.cid}/messages", json={"content": "撤权竞争"},
        ))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            await _change(world, kind)
            release.set()
            response = await asyncio.wait_for(request, 20)
        finally:
            release.set()
            await _settle(request)
    if kind == "revoke":
        assert response.status_code == 404, response.text
    else:
        assert response.status_code == 409 and response.json()["code"] == "permission_denied", response.text
    assert await _editor_tasks(editor_app, world) == []
    assert await _messages(editor_app, world.cid) == []
    assert len(fake_llm.tool_calls) == calls


async def test_pg_send_holding_lock_blocks_revocation(pg_apps, fake_llm, monkeypatch):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        engine = editor_app.state.task_engine
        await engine.stop()
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = task_service.create_task

        async def held(db, engine_, user_id, kind, params, **kwargs):
            if user_id == world.editor["id"] and kind == "agent.turn" and not holding.is_set():
                # 会话写锁（门禁 → User → 发送写门禁 → 作品父行）已持有，中央受理提交之前。
                assert authz.holds_gate(db)
                pids["send"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()
            return await original(db, engine_, user_id, kind, params, **kwargs)

        monkeypatch.setattr(task_service, "create_task", held)
        calls = len(fake_llm.tool_calls)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/conversations/{world.cid}/messages", json={"content": "先受理"},
        ))
        revoke = None
        try:
            await asyncio.wait_for(holding.wait(), 15)
            revoke = asyncio.create_task(_change(world, "revoke"))
            await asyncio.wait_for(_blocked_by(editor_app, pids["send"], revoke), 15)
            assert not revoke.done()
            release.set()
            response = await asyncio.wait_for(request, 20)
            await asyncio.wait_for(revoke, 20)
        finally:
            release.set()
            await _settle(request, revoke)
            await engine.start()
        # 受理先提交（数据库事实）；提交后的复核 / 投影可能已看到撤权。
        assert response.status_code in (202, 404, 409), response.text
        assert (await _member(editor_app, world)).status == "revoked"
        (turn,) = await _editor_tasks(editor_app, world)
        assert [row.role for row in await _messages(editor_app, world.cid)] == ["user"]
        row = await _wait_db(editor_app, turn.id, TERMINAL, within=20)
        assert (row.status, row.error) == ("failed", COLLAB_FAILED)
        assert len(fake_llm.tool_calls) == calls


# ---------------- 2 / 7. 真实 HTTP 发送 lease（主对话与摘要） ----------------


def _sse_text(content):
    return (f"data: {json.dumps(openai_chunk({'content': content}, 'stop'), ensure_ascii=False)}\n\n"
            "data: [DONE]\n\n").encode()


async def _real_world(editor_app, owner_app, editor_client, owner_client, url, *, compaction=False):
    owner = await register(owner_client, "e4ig-real-owner")
    owner_cfg = await create_ai_config(owner_client, base_url=f"{url}/v1/", api_key="sk-local-e4ig", model="real-model")
    project = await create_project(owner_client)
    editor = await register(editor_client, "e4ig-real-editor")
    member = await _invite(owner_client, editor_client, project["id"], "e4ig-real-editor", allow_owner_ai=True)
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)
    if compaction:
        async with owner_app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == owner_cfg["id"]).values(context_window_k=8))
            db.add(AgentMessage(conversation_id=cid, role="user", content="旧消息" + "较早" * 3000))
            db.add(AgentMessage(conversation_id=cid, role="user", content="短的旧消息"))
            await db.commit()
    return SimpleNamespace(owner=owner, owner_client=owner_client, owner_app=owner_app, editor_app=editor_app,
                           editor=editor, editor_client=editor_client, editor_name="e4ig-real-editor",
                           project=project, member=member, base=base, cid=cid)


def _real_provider(monkeypatch, app):
    monkeypatch.setattr(app.state.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


async def _assert_not_delivered(app, world, turn_id):
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, turn_id)
        conversation = await db.get(AgentConversation, world.cid)
        stored = [row.content or "" for row in await db.scalars(select(AgentMessage).where(
            AgentMessage.conversation_id == world.cid))]
    assert SECRET not in (task.output_text or "")
    assert all(SECRET not in content for content in stored)
    assert SECRET not in (conversation.summary or "") and conversation.summary_source is None
    hub = app.state.task_engine.hubs.get(turn_id)
    assert hub is None or SECRET not in hub.text


@pytest.mark.parametrize("timing", ["before_send", "during_send"])
@pytest.mark.parametrize(("phase", "kind"), [
    *(("main", kind) for kind in KINDS), ("summary", "revoke"), ("summary", "token"),
])
async def test_pg_editor_real_http_send_lease(pg_apps, monkeypatch, phase, kind, timing):
    editor_app, owner_app = pg_apps
    _real_provider(monkeypatch, editor_app)
    method = "chat_tools_stream" if phase == "main" else "generate_stream"
    paused, release = asyncio.Event(), asyncio.Event()
    send_pid = None
    if timing == "before_send":
        original_stream = getattr(OpenAICompatProvider, method)

        async def gated(self, *args, **kwargs):
            # 发送 lease 之前（尚未发出 HTTP 请求、不持任何锁）停住。
            if not paused.is_set():
                paused.set()
                await release.wait()
            async for item in original_stream(self, *args, **kwargs):
                yield item

        monkeypatch.setattr(OpenAICompatProvider, method, gated)
    else:
        original_verify = collaboration_ai_guard.SendGuard._verify_locked

        async def capture_pid(self, db, *, preflight):
            nonlocal send_pid
            await original_verify(self, db, preflight=preflight)
            if not preflight:
                send_pid = await db.scalar(text("SELECT pg_backend_pid()"))

        monkeypatch.setattr(collaboration_ai_guard.SendGuard, "_verify_locked", capture_pid)
    async with (
        _client(editor_app) as editor_client, _client(owner_app) as owner_client,
        _server(payload=_sse_text(SECRET)) as (url, received, headers, body, requests),
    ):
        world = await _real_world(editor_app, owner_app, editor_client, owner_client, url,
                                  compaction=phase == "summary")
        turn = await _send(editor_client, world.base, world.cid, "真实发送")
        changing = None
        try:
            if timing == "before_send":
                await asyncio.wait_for(paused.wait(), 15)
                await _change(world, kind)
                release.set()
            else:
                await asyncio.wait_for(received.wait(), 15)
                assert send_pid is not None and len(requests) == 1
                changing = asyncio.create_task(_change(world, kind))
                # 请求已发出、响应头未到：作者侧变更在 PostgreSQL 上被发送 lease 阻塞。
                await asyncio.wait_for(_blocked_by(owner_app, send_pid, changing), 15)
                assert not changing.done()
                headers.set()
                await asyncio.wait_for(changing, 15)
                body.set()
            row = await _wait_db(editor_app, turn["id"], TERMINAL, within=30)
        finally:
            release.set()
            headers.set()
            body.set()
            await _settle(changing)
        assert row.status == "failed", (row.status, row.error)
        assert (row.error == TOKEN_STOPPED) if kind == "token" else (row.error in AUTH_STOPPED), row.error
        # 发送前提交：零 HTTP；发送中提交：已发出的 1 次不能撤回，也不再发出第二次。
        assert len(requests) == (0 if timing == "before_send" else 1)
        await _assert_not_delivered(editor_app, world, turn["id"])


# ---------------- 3. 后台自动执行写工具 vs 撤权（含管理员兼编辑者） ----------------


async def _admin_world(editor_app, owner_app, editor_client, owner_client):
    """管理员（is_admin）以编辑者身份受邀；本人无模型、作者授权回退。"""
    from app.schemas.auth import Credentials
    from app.services import auth_service

    async with editor_app.state.sessionmaker() as db:
        await auth_service.create_user(db, Credentials(username="e4ig_manager", password="secret123"), is_admin=True)
    login = await editor_client.post("/api/v1/auth/login",
                                     json={"username": "e4ig_manager", "password": "secret123"})
    assert login.status_code == 200, login.text
    owner = await register(owner_client, "e4ig-admin-owner")
    await create_ai_config(owner_client, api_key="sk-owner-e4ig-admin1")
    project = await create_project(owner_client)
    patched = await owner_client.patch(f"/api/v1/projects/{project['id']}",
                                       json={"synopsis": "原简介", "theme": "原主题"})
    assert patched.status_code == 200, patched.text
    member = await _invite(owner_client, editor_client, project["id"], "e4ig_manager", allow_owner_ai=True)
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)
    return SimpleNamespace(owner=owner, owner_client=owner_client, owner_app=owner_app, editor_app=editor_app,
                           editor=(await editor_client.get("/api/v1/auth/me")).json(),
                           editor_client=editor_client, editor_name="e4ig_manager", project=project, member=member,
                           base=base, cid=cid)


async def _synopsis(app, project_id):
    async with app.state.sessionmaker() as db:
        return (await db.get(Project, project_id)).synopsis


async def _audits(app, project_id):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.project_id == project_id).order_by(AdminAuditLog.id)))


@pytest.mark.parametrize(("actor", "order", "kind"), [
    ("editor", "revocation_first", "revoke"),
    ("editor", "tool_first", "revoke"),
    ("admin", "revocation_first", "viewer"),
    ("admin", "tool_first", "viewer"),
])
async def test_pg_auto_write_tool_vs_revocation(pg_apps, fake_llm, monkeypatch, actor, order, kind):
    from tests.helpers import Gate

    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        if actor == "admin":
            world = await _admin_world(editor_app, owner_app, editor_client, owner_client)
        else:
            world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        patched = await editor_client.patch(f"{world.base}/conversations/{world.cid}", json={"auto_execute": True})
        assert patched.status_code == 200, patched.text
        original_synopsis = await _synopsis(editor_app, world.project["id"])
        audits0 = len(await _audits(editor_app, world.project["id"]))
        gate = Gate()
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        if order == "revocation_first":
            # 模型流中停住（发送 lease 已在响应头释放，不持锁）；撤权在此期间提交。
            fake_llm.push_tools([call("update_project", {"synopsis": "自动写入"}, "a1"), gate])
        else:
            fake_llm.push_tools([call("update_project", {"synopsis": "自动写入"}, "a1")])
            original = service.execute_change

            async def held(db, task_context, change, **kwargs):
                if kwargs.get("scope") is not None and not holding.is_set():
                    # 运行器已持 _lock_execution（门禁 → User → 发送写门禁 → 作品 → Task）与提案行锁。
                    assert authz.holds_gate(db)
                    pids["tool"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    holding.set()
                    await release.wait()
                return await original(db, task_context, change, **kwargs)

            monkeypatch.setattr(service, "execute_change", held)
        turn = await _send(editor_client, world.base, world.cid, "自动修改简介")
        changing = None
        try:
            if order == "revocation_first":
                await gate.wait_reached(15)
                calls = len(fake_llm.tool_calls)
                await _change(world, kind)
                gate.release()
            else:
                await asyncio.wait_for(holding.wait(), 15)
                calls = len(fake_llm.tool_calls)
                changing = asyncio.create_task(_change(world, kind))
                await asyncio.wait_for(_blocked_by(editor_app, pids["tool"], changing), 15)
                assert not changing.done()
                release.set()
                await asyncio.wait_for(changing, 20)
            row = await _wait_db(editor_app, turn["id"], TERMINAL, within=30)
        finally:
            gate.release()
            release.set()
            await _settle(changing)
        assert row.status == "failed" and row.error in AUTH_STOPPED, (row.status, row.error)
        # 撤权之后零模型请求（工具先提交的一侧也不再续跑下一轮）。
        assert len(fake_llm.tool_calls) == calls
        changes = await _changes(editor_app, turn["id"])
        audits = (await _audits(editor_app, world.project["id"]))[audits0:]
        if order == "revocation_first":
            assert await _synopsis(editor_app, world.project["id"]) == original_synopsis
            assert all(item.status != "executed" for item in changes)
            # 锁后能力复核（lock_agent_write）先于工具内的管理审计作用域：拒绝不产生成功或失败审计。
            assert audits == []
        else:
            # 工具先持锁：合法结果先于撤权提交，保留（R3）。
            assert await _synopsis(editor_app, world.project["id"]) == "自动写入"
            assert [item.status for item in changes] == ["executed"]
            if actor == "admin":
                # 管理员兼编辑者经 service 写他人作品：恰好一条成功管理审计（裁决 6），无失败。
                assert [(item.action, item.result) for item in audits] == [("project.update", "success")]
            else:
                assert audits == []


# ---------------- 4. HTTP 确认 vs 撤权；确认请求取消 ----------------


async def _start_task_world(editor_app, owner_app, editor_client, owner_client, fake_llm):
    world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
    created = await owner_client.post(f"/api/v1/projects/{world.project['id']}/chapters",
                                      json={"title": "启程", "content": "林远推开门。"})
    assert created.status_code == 201, created.text
    world.chapter = created.json()
    fake_llm.push_tools([
        call("start_task", {"kind": "chapter.generate", "params": {"chapter_id": world.chapter["id"]}}, "s1"),
        call("update_project", {"theme": "第二项"}, "s2"),
    ])
    turn = await _send(editor_client, world.base, world.cid, "生成这一章")
    assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
    world.turn = turn
    world.view = next(item for item in (await _detail(editor_client, world.base, world.cid))["changes"]
                      if item["tool_name"] == "start_task")
    return world


async def _chapter(app, chapter_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Chapter, chapter_id)


@pytest.mark.parametrize("kind", ["revoke", "viewer"])
async def test_pg_confirm_start_task_after_committed_revocation(pg_apps, fake_llm, monkeypatch, kind):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _start_task_world(editor_app, owner_app, editor_client, owner_client, fake_llm)
        entered, release = asyncio.Event(), asyncio.Event()
        original = agent_access.lock_agent_entry

        async def gated(db, actor_id, project_id, **kwargs):
            # 锁外初检与受控受理准备（模型预检）已完成；入口写锁之前停住。
            if actor_id == world.editor["id"] and kwargs.get("lock", True) and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(db, actor_id, project_id, **kwargs)

        monkeypatch.setattr(agent_access, "lock_agent_entry", gated)
        calls = len(fake_llm.calls)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{world.view['id']}/confirm", json=change_action(world.view),
        ))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            await _change(world, kind)
            release.set()
            response = await asyncio.wait_for(request, 20)
        finally:
            release.set()
            await _settle(request)
    if kind == "revoke":
        assert response.status_code == 404, response.text
    else:
        assert response.status_code == 409 and response.json()["code"] == "permission_denied", response.text
    assert await _editor_tasks(editor_app, world, "chapter.generate") == []
    stored = await _changes(editor_app, world.turn["id"])
    assert all(item.status != "executed" for item in stored)
    assert (await _chapter(editor_app, world.chapter["id"])).status != "generating"
    assert len(fake_llm.calls) == calls


async def test_pg_confirm_start_task_holding_lock_then_revocation(pg_apps, fake_llm, monkeypatch):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _start_task_world(editor_app, owner_app, editor_client, owner_client, fake_llm)
        engine = editor_app.state.task_engine
        await engine.stop()
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = service.execute_change

        async def held(db, task_context, change, **kwargs):
            if change.tool_name == "start_task" and not holding.is_set():
                # 确认请求已持入口写锁（门禁 → User → 发送写门禁 → 作品 → 提案），execute_admitted 之前。
                assert authz.holds_gate(db)
                pids["confirm"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()
            return await original(db, task_context, change, **kwargs)

        monkeypatch.setattr(service, "execute_change", held)
        calls = len(fake_llm.calls)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{world.view['id']}/confirm", json=change_action(world.view),
        ))
        revoke = None
        try:
            await asyncio.wait_for(holding.wait(), 15)
            revoke = asyncio.create_task(_change(world, "revoke"))
            await asyncio.wait_for(_blocked_by(editor_app, pids["confirm"], revoke), 15)
            assert not revoke.done()
            release.set()
            response = await asyncio.wait_for(request, 20)
            await asyncio.wait_for(revoke, 20)
        finally:
            release.set()
            await _settle(request, revoke)
            await engine.start()
        assert response.status_code in (200, 404, 409), response.text
        assert (await _member(editor_app, world)).status == "revoked"
        (derived,) = await _editor_tasks(editor_app, world, "chapter.generate")
        # 派生任务先于撤权提交并绑定原轮次；撤权后领取锁内受限收尾，零模型请求，章节占用释放。
        assert derived.input["_collaboration_grant"] == {
            "invitation_id": world.member["invitation_id"], "revision": world.member["revision"],
        }
        row = await _wait_db(editor_app, derived.id, TERMINAL, within=30)
        assert (row.status, row.error) == ("failed", COLLAB_FAILED)
        await engine.scan_collaboration_paused()
        turn = await _wait_db(editor_app, world.turn["id"], TERMINAL, within=30)
        assert (turn.status, turn.error) == ("failed", COLLAB_FAILED)
        chapter = await _chapter(editor_app, world.chapter["id"])
        assert chapter.status != "generating" and chapter.content == "林远推开门。"
        assert len(fake_llm.calls) == calls


async def test_pg_cancelled_confirm_request_rolls_back(pg_apps, fake_llm, monkeypatch):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        fake_llm.push_tools([call("update_project", {"synopsis": "取消的确认"}, "x1"),
                             call("update_project", {"theme": "第二项"}, "x2")])
        turn = await _send(editor_client, world.base, world.cid, "两项修改")
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
        view = (await _detail(editor_client, world.base, world.cid))["changes"][0]
        original_synopsis = await _synopsis(editor_app, world.project["id"])
        reached = asyncio.Event()
        original = service.execute_change

        async def stuck(db, task_context, change, **kwargs):
            result = await original(db, task_context, change, **kwargs)
            if not reached.is_set():
                # 工具已在事务内写入、尚未提交：客户端在此断开。
                reached.set()
                await asyncio.Event().wait()
            return result

        monkeypatch.setattr(service, "execute_change", stuck)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{view['id']}/confirm", json=change_action(view),
        ))
        try:
            await asyncio.wait_for(reached.wait(), 15)
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request
        finally:
            await _settle(request)
        monkeypatch.setattr(service, "execute_change", original)
        assert await _synopsis(editor_app, world.project["id"]) == original_synopsis
        stored = {item.id: item for item in await _changes(editor_app, turn["id"])}
        assert stored[view["id"]].status == "proposed"
        # 取消释放了全部锁：作者随即可改成员，编辑者之后的确认也照常执行。
        view = next(item for item in (await _detail(editor_client, world.base, world.cid))["changes"]
                    if item["id"] == view["id"])
        confirmed = await editor_client.post(f"{world.base}/changes/{view['id']}/confirm", json=change_action(view))
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
        assert await _synopsis(editor_app, world.project["id"]) == "取消的确认"


# ---------------- 5. 撤销 vs 撤权；撤销 vs 人工编辑 ----------------


async def _executed_world(editor_app, owner_app, editor_client, owner_client, fake_llm):
    world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
    fake_llm.push_tools([call("update_project", {"synopsis": "Agent 简介"}, "u1")])
    turn = await _send(editor_client, world.base, world.cid, "改简介")
    assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
    view = (await _detail(editor_client, world.base, world.cid))["changes"][0]
    confirmed = await editor_client.post(f"{world.base}/changes/{view['id']}/confirm", json=change_action(view))
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
    assert (await wait_task(editor_client, turn, within=20))["status"] == "succeeded"
    world.turn = turn
    world.view = next(item for item in (await _detail(editor_client, world.base, world.cid))["changes"]
                      if item["id"] == view["id"])
    return world


async def _change_status(app, change_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(AgentChange.status).where(AgentChange.id == change_id))


@pytest.mark.parametrize("kind", ["revoke", "viewer"])
async def test_pg_undo_after_committed_revocation_is_denied(pg_apps, fake_llm, monkeypatch, kind):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _executed_world(editor_app, owner_app, editor_client, owner_client, fake_llm)
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        original = agent_access.lock_agent_entry

        async def gated(db, actor_id, project_id, **kwargs):
            # 撤销第一次锁内初检后已回滚；第二次（进程锁内）入口写锁之前停住。
            if actor_id == world.editor["id"] and kwargs.get("lock", True):
                seen.append(True)
                if len(seen) == 2 and not entered.is_set():
                    entered.set()
                    await release.wait()
            return await original(db, actor_id, project_id, **kwargs)

        monkeypatch.setattr(agent_access, "lock_agent_entry", gated)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{world.view['id']}/undo", json=change_action(world.view),
        ))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            await _change(world, kind)
            release.set()
            response = await asyncio.wait_for(request, 20)
        finally:
            release.set()
            await _settle(request)
    if kind == "revoke":
        assert response.status_code == 404, response.text
    else:
        assert response.status_code == 409 and response.json()["code"] == "permission_denied", response.text
    assert await _change_status(editor_app, world.view["id"]) == "executed"
    assert await _synopsis(editor_app, world.project["id"]) == "Agent 简介"


async def test_pg_undo_holding_lock_blocks_revocation(pg_apps, fake_llm, monkeypatch):
    from app.services.agent import effects

    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _executed_world(editor_app, owner_app, editor_client, owner_client, fake_llm)
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = effects.preflight_effect_versions

        async def held(db, project_id, rows):
            if not holding.is_set():
                # 撤销已在锁后通过 Agent 写能力复核（原轮次 grant），效果预检之前。
                assert authz.holds_gate(db)
                pids["undo"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()
            return await original(db, project_id, rows)

        monkeypatch.setattr(effects, "preflight_effect_versions", held)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{world.view['id']}/undo", json=change_action(world.view),
        ))
        revoke = None
        try:
            await asyncio.wait_for(holding.wait(), 15)
            revoke = asyncio.create_task(_change(world, "revoke"))
            await asyncio.wait_for(_blocked_by(editor_app, pids["undo"], revoke), 15)
            assert not revoke.done()
            release.set()
            response = await asyncio.wait_for(request, 20)
            await asyncio.wait_for(revoke, 20)
        finally:
            release.set()
            await _settle(request, revoke)
    # 撤销先提交（合法）；撤权随后完成。响应投影可能已看到撤权。
    assert response.status_code in (200, 404, 409), response.text
    assert await _change_status(editor_app, world.view["id"]) == "undone"
    assert await _synopsis(editor_app, world.project["id"]) == "原简介"
    assert (await _member(editor_app, world)).status == "revoked"


@pytest.mark.parametrize("order", ["manual_first", "undo_first"])
async def test_pg_undo_vs_author_manual_edit(pg_apps, fake_llm, monkeypatch, order):
    from app.services.agent import effects

    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _executed_world(editor_app, owner_app, editor_client, owner_client, fake_llm)
        path = f"/api/v1/projects/{world.project['id']}"
        if order == "manual_first":
            edited = await owner_client.patch(path, json={"synopsis": "作者人工"})
            assert edited.status_code == 200, edited.text
            response = await editor_client.post(
                f"{world.base}/changes/{world.view['id']}/undo", json=change_action(world.view),
            )
            # effects.undo 行级比对（当前值 != 执行后 after）：人工修改之后的旧效果不能覆盖人工值。
            assert response.status_code == 409 and response.json().get("code") == "undo_conflict", response.text
            assert await _change_status(editor_app, world.view["id"]) == "executed"
            assert await _synopsis(editor_app, world.project["id"]) == "作者人工"
            return
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = effects.preflight_effect_versions

        async def held(db, project_id, rows):
            result = await original(db, project_id, rows)
            if not holding.is_set():
                pids["undo"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()
            return result

        monkeypatch.setattr(effects, "preflight_effect_versions", held)
        request = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{world.view['id']}/undo", json=change_action(world.view),
        ))
        manual = None
        try:
            await asyncio.wait_for(holding.wait(), 15)
            manual = asyncio.create_task(owner_client.patch(path, json={"synopsis": "作者人工"}))
            await asyncio.wait_for(_blocked_by(editor_app, pids["undo"], manual), 15)
            assert not manual.done()
            release.set()
            response = await asyncio.wait_for(request, 20)
            edited = await asyncio.wait_for(manual, 20)
        finally:
            release.set()
            await _settle(request, manual)
        # 撤销先持锁并提交；人工编辑被阻塞，随后在撤销结果之上提交，二者都不丢。
        assert response.status_code == 200 and response.json()["status"] == "undone", response.text
        assert edited.status_code == 200, edited.text
        assert await _change_status(editor_app, world.view["id"]) == "undone"
        assert await _synopsis(editor_app, world.project["id"]) == "作者人工"


# ---------------- 6. 编辑者 MCP 关闭 ----------------


async def test_pg_editor_mcp_closed_and_residual_external_never_blocks_author_delete(
    pg_apps, fake_llm, monkeypatch,
):
    from app.services.agent import mcp_tools
    from tests.mcp_fakes import FakeMcpConnector, tool

    editor_app, owner_app = pg_apps
    connector = FakeMcpConnector()
    editor_app.state.task_engine.ctx.mcp_connector = connector
    owner_app.state.task_engine.ctx.mcp_connector = connector
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        # 编辑者本人确有一个可用插件（插件属账号级资源）：只有会话挂载被拒绝。
        url = "https://dict.example.com/mcp"
        connector.add(url, [tool("send", {"to": {"type": "string"}}, ["to"])])
        plugin = await editor_client.post("/api/v1/mcp-plugins", json={"name": "dict", "url": url})
        assert plugin.status_code == 201, plugin.text
        plugin = plugin.json()
        refreshed = await editor_client.post(f"/api/v1/mcp-plugins/{plugin['id']}/refresh")
        assert refreshed.status_code == 200, refreshed.text
        attached = await editor_client.patch(f"{world.base}/conversations/{world.cid}",
                                             json={"mcp_plugin_ids": [plugin["id"]]})
        assert attached.status_code == 403 and attached.json()["code"] == "collaborator_mcp_unavailable", attached.text
        created = await editor_client.post(f"{world.base}/conversations", json={"mcp_plugin_ids": [plugin["id"]]})
        # 新建会话不接受挂载插件（字段被忽略），不能借新建绕过。
        assert created.status_code == 201 and created.json()["mcp_plugin_ids"] == [], created.text
        fake_llm.push_tools([call("update_project", {"synopsis": "将被改成外部"}, "m1"),
                             call("update_project", {"theme": "第二项"}, "m2")])
        turn = await _send(editor_client, world.base, world.cid, "外部")
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
        first = (await _detail(editor_client, world.base, world.cid))["changes"][0]
        async with editor_app.state.sessionmaker() as db:
            # 编辑者轮次不可能产生外部提案（裁决 2）；这里人为植入残留行，验证确认路径仍不发网、不建屏障。
            # 残留挂载（绕过入口 403）+ 本轮映射表 + 与当前插件一致的预览：除“协作轮次”外完全可执行，
            # 若确认路径不按协作轮次拦截，就会进入外部执行（认领、连接插件）。
            await db.execute(update(AgentConversation).where(AgentConversation.id == world.cid).values(
                mcp_plugin_ids=[plugin["id"]],
            ))
            await db.flush()
            settings = editor_app.state.task_engine.ctx.settings
            conversation = await db.get(AgentConversation, world.cid)
            tool_map = await mcp_tools.build_tool_map(db, settings, world.editor["id"], conversation)
            assert len(tool_map) == 1, tool_map
            (exposed,) = tool_map
            arguments = {"to": "编辑部"}
            resolved = await mcp_tools.resolve(db, settings, world.editor["id"], world.cid, tool_map, exposed)
            assert resolved is not None
            task_row = await db.get(Task, turn["id"])
            task_row.checkpoint = {**(task_row.checkpoint or {}), "mcp_tool_map": tool_map}
            await db.execute(update(AgentChange).where(AgentChange.id == first["id"]).values(
                kind="external", tool_name=exposed, arguments=arguments,
                preview=mcp_tools.preview(tool_map[exposed], resolved, arguments),
            ))
            await db.commit()
        connects0 = len(connector.targets)
        view = next(item for item in (await _detail(editor_client, world.base, world.cid))["changes"]
                    if item["id"] == first["id"])
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = service.execute_change

        async def held(db, task_context, change, **kwargs):
            if change.id == view["id"] and not holding.is_set():
                pids["confirm"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()
            return await original(db, task_context, change, **kwargs)

        monkeypatch.setattr(service, "execute_change", held)
        confirm = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{view['id']}/confirm", json=change_action(view),
        ))
        delete = None
        try:
            await asyncio.wait_for(holding.wait(), 15)
            delete = asyncio.create_task(owner_client.delete(f"/api/v1/projects/{world.project['id']}"))
            await asyncio.wait_for(_blocked_by(editor_app, pids["confirm"], delete), 15)
            assert not delete.done()
            release.set()
            confirmed = await asyncio.wait_for(confirm, 20)
            deleted = await asyncio.wait_for(delete, 30)
        finally:
            release.set()
            await _settle(confirm, delete)
        assert confirmed.status_code in (200, 404, 409), confirmed.text
        assert deleted.status_code == 204, deleted.text
        async with editor_app.state.sessionmaker() as db:
            row = await db.get(AgentChange, view["id"])
            assert (row.status, row.error) == ("failed", mcp_tools.CHANGED)
            assert not await project_service.has_unsettled_external_change(db, project_id=world.project["id"])
        assert connector.calls == [] and len(connector.targets) == connects0


# ---------------- 8. 编辑者确认路径的真实 40001 ----------------


async def test_pg_editor_confirm_serialization_conflict_is_safe_409(pg_apps, fake_llm, monkeypatch):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        fake_llm.push_tools([call("update_project", {"synopsis": "冲突后重试"}, "r1"),
                             call("update_project", {"theme": "第二项"}, "r2")])
        turn = await _send(editor_client, world.base, world.cid, "两项")
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
        view = (await _detail(editor_client, world.base, world.cid))["changes"][0]
        original_synopsis = await _synopsis(editor_app, world.project["id"])
        touched = asyncio.Event()
        original = service._require_change_write

        async def touch(db, entry, user_id, row):
            grant = await original(db, entry, user_id, row)
            if row.id == view["id"] and authz.holds_gate(db) and not touched.is_set():
                # 确认的 REPEATABLE READ 快照已在门禁 UPDATE 冻结；另一真实连接在此之后提交对同一提案行的更新，
                # 随后确认对该行的条件 UPDATE 得到真实 40001。
                async with owner_app.state.sessionmaker() as other:
                    # 原值写回（不触发 ORM onupdate）：投影事实不变，只产生新的行版本。
                    await other.execute(text("UPDATE agent_changes SET preview = preview WHERE id = :id"),
                                        {"id": view["id"]})
                    await other.commit()
                touched.set()
            return grant

        monkeypatch.setattr(service, "_require_change_write", touch)
        response = await editor_client.post(f"{world.base}/changes/{view['id']}/confirm", json=change_action(view))
        assert touched.is_set()
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == transactions.CONCURRENT_CHANGE
        assert "agent_changes" not in response.text and "UPDATE" not in response.text
        assert await _change_status(editor_app, view["id"]) == "proposed"
        assert await _synopsis(editor_app, world.project["id"]) == original_synopsis
        # 冲突是安全可重试的：再次确认照常执行。
        view = next(item for item in (await _detail(editor_client, world.base, world.cid))["changes"]
                    if item["id"] == view["id"])
        retried = await editor_client.post(f"{world.base}/changes/{view['id']}/confirm", json=change_action(view))
        assert retried.status_code == 200 and retried.json()["status"] == "executed", retried.text


# ---------------- 9. 受限收尾先持锁 vs 编辑者本人确认 ----------------


async def test_pg_restricted_finish_holding_lock_blocks_own_confirm(pg_apps, fake_llm, monkeypatch):
    editor_app, owner_app = pg_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        world = await _pg_world(editor_app, owner_app, editor_client, owner_client)
        fake_llm.push_tools([call("update_project", {"synopsis": "收尾竞争一"}, "f1"),
                             call("update_project", {"theme": "收尾竞争二"}, "f2")])
        turn = await _send(editor_client, world.base, world.cid, "两项")
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
        view = (await _detail(editor_client, world.base, world.cid))["changes"][0]
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = service.void_revoked_turn_changes

        async def held(db, **kwargs):
            result = await original(db, **kwargs)
            if not holding.is_set():
                # 受限收尾已在锁内（门禁 → User → 发送写门禁 → 作品 → Task）作废提案，提交之前。
                pids["finish"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()
            return result

        monkeypatch.setattr(service, "void_revoked_turn_changes", held)
        entered, go = asyncio.Event(), asyncio.Event()
        original_entry = agent_access.lock_agent_entry

        async def gated(db, actor_id, project_id, **kwargs):
            # 确认已通过锁外初检（此时仍是 editor）；入口写锁之前停住。
            if actor_id == world.editor["id"] and kwargs.get("lock", True) and not entered.is_set():
                entered.set()
                await go.wait()
            return await original_entry(db, actor_id, project_id, **kwargs)

        monkeypatch.setattr(agent_access, "lock_agent_entry", gated)
        confirm = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{view['id']}/confirm", json=change_action(view),
        ))
        scan = None
        try:
            await asyncio.wait_for(entered.wait(), 15)
            # 降为 viewer 后，受限收尾（第二个实例：授权运行时与连接都独立，阻塞只能发生在 PostgreSQL 上）
            # 先取锁并作废提案；确认随后在入口写锁上等待它。编辑者实例的引擎暂停，避免它抢先复查。
            await editor_app.state.task_engine.stop()
            await _change(world, "viewer")
            scan = asyncio.create_task(owner_app.state.task_engine.scan_collaboration_paused())
            await asyncio.wait_for(holding.wait(), 15)
            go.set()
            await asyncio.wait_for(_blocked_by(editor_app, pids["finish"], confirm), 15)
            assert not confirm.done()
            release.set()
            response = await asyncio.wait_for(confirm, 20)
            await asyncio.wait_for(scan, 20)
        finally:
            go.set()
            release.set()
            await _settle(scan, confirm)
            await editor_app.state.task_engine.start()
        assert response.status_code == 409 and response.json()["code"] == "permission_denied", response.text
        row = await _task(editor_app, turn["id"])
        assert (row.status, row.error) == ("failed", COLLAB_FAILED)
        stored = await _changes(editor_app, turn["id"])
        assert [(item.status, item.error) for item in stored] == [("failed", VOIDED)] * 2
        assert await _synopsis(editor_app, world.project["id"]) == "原简介"


# ---------------- E7：编辑者请求账目与配置来源（PostgreSQL REPEATABLE READ 对照） ----------------


@pytest.mark.parametrize("mode", ["fallback", "own_ungranted"])
async def test_pg_editor_request_ledger_and_config_source(pg_apps, fake_llm, mode):
    from tests.test_agent_e2e_e4ig import check_request_ledger

    app = pg_apps[0]
    async with _client(app) as owner_client, _client(app) as editor_client:
        await check_request_ledger(app, owner_client, editor_client, fake_llm, mode)
