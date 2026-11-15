"""E4i-d：真实 PostgreSQL 上的协作者 Agent 入口。

1. 编辑者端到端：真实 HTTP 建会话 → 发送 → 写工具提案 → 确认（自动续跑成功）→ 撤销，作品回到原值。
2. 拒绝与撤权并发（裁决 15：拒绝不经 execute_change，入口锁内自行判定 ai.use + 原轮次）：
   - 撤权 / 降级先提交：拒绝请求在锁外初检之后、入口写锁之前停住，作者提交后放行 → 404 / 409，提案保持 proposed。
   - 拒绝先持锁：拒绝在锁内判定通过后、提交前停住；作者撤权（第二个应用实例）在 PostgreSQL 上被其阻塞
     （pg_blocking_pids 观测），放行后拒绝 200 成功、撤权随后完成。
编排只用 Event 与 pg_blocking_pids，不用固定等待。
"""

import asyncio

import httpx
import pytest
from sqlalchemy import select, text

from app.models import AgentChange, Project, ProjectCollaborator
from app.services import authorization_service as authz
from app.services.agent import access as agent_access
from app.services.agent import service
from tests import pg_support
from tests.helpers import create_ai_config, create_project, register, wait_task
from tests.test_agent_entries_e4id import STOP, _detail, _invite, _new_conversation, _send
from tests.test_agent_mcp_skills import change_action
from tests.test_agent_runtime import call

OWNER_KEY = "sk-owner-e4id-pg-001"
EDITOR_KEY = "sk-editor-e4id-pg-01"


@pytest.fixture
async def pg_agent_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="Agent 入口测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2, start_first_engine=True,
    ) as apps:
        yield apps


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _world(app, owner_client, editor_client):
    owner = await register(owner_client, "e4id-pg-owner")
    await create_ai_config(owner_client, api_key=OWNER_KEY)
    project = await create_project(owner_client)
    editor = await register(editor_client, "e4id-pg-editor")
    await create_ai_config(editor_client, api_key=EDITOR_KEY)
    member = await _invite(owner_client, editor_client, project["id"], "e4id-pg-editor")
    return owner, editor, project, member


async def _synopsis(app, project_id):
    async with app.state.sessionmaker() as db:
        return (await db.get(Project, project_id)).synopsis


async def test_postgresql_editor_send_tool_confirm_undo(pg_agent_apps, fake_llm):
    app = pg_agent_apps[0]
    async with _client(app) as owner_client, _client(app) as editor_client:
        _owner, editor, project, member = await _world(app, owner_client, editor_client)
        base = f"/api/v1/projects/{project['id']}/agent"
        original = await _synopsis(app, project["id"])
        cid = await _new_conversation(editor_client, base)
        fake_llm.push_tools([call("update_project", {"synopsis": "PG 编辑者 Agent"}, "pg1")])
        turn = await _send(editor_client, base, cid)
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
        change = (await _detail(editor_client, base, cid))["changes"][0]
        confirmed = await editor_client.post(f"{base}/changes/{change['id']}/confirm", json=change_action(change))
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
        assert await _synopsis(app, project["id"]) == "PG 编辑者 Agent"
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "succeeded"
        view = next(item for item in (await _detail(editor_client, base, cid))["changes"] if item["id"] == change["id"])
        undone = await editor_client.post(f"{base}/changes/{change['id']}/undo", json=change_action(view))
        assert undone.status_code == 200 and undone.json()["status"] == "undone", undone.text
    assert await _synopsis(app, project["id"]) == original
    assert {item["api_key"] for item in fake_llm.tool_calls} == {EDITOR_KEY}
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(AgentChange).where(AgentChange.id == change["id"]))
        assert row.status == "undone"
        from app.models import Task

        task = await db.get(Task, turn["id"])
        assert task.user_id == editor["id"]
        assert task.input["_collaboration_grant"] == {
            "invitation_id": member["invitation_id"], "revision": member["revision"],
        }


async def _paused_proposals(app, owner_client, editor_client, fake_llm):
    _owner, editor, project, member = await _world(app, owner_client, editor_client)
    base = f"/api/v1/projects/{project['id']}/agent"
    cid = await _new_conversation(editor_client, base)
    fake_llm.push_tools([
        call("update_project", {"synopsis": "并发一"}, "c1"), call("update_project", {"theme": "并发二"}, "c2"),
    ])
    turn = await _send(editor_client, base, cid)
    assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
    change = (await _detail(editor_client, base, cid))["changes"][0]
    return editor, project, member, base, change


async def _change_status(app, change_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(AgentChange.status).where(AgentChange.id == change_id))


@pytest.mark.parametrize("revocation", ["revoke", "viewer"])
async def test_postgresql_reject_after_concurrent_revocation_is_denied(
    pg_agent_apps, fake_llm, monkeypatch, revocation,
):
    app = pg_agent_apps[0]
    async with _client(app) as owner_client, _client(app) as editor_client:
        editor, project, member, base, change = await _paused_proposals(app, owner_client, editor_client, fake_llm)
        entered, release = asyncio.Event(), asyncio.Event()
        original = agent_access.lock_agent_entry

        async def gated(db, actor_id, project_id, **kwargs):
            # 锁外初检（lock=False）已通过；入口写锁之前停住，撤权在此期间提交。
            if actor_id == editor["id"] and kwargs.get("lock", True) and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(db, actor_id, project_id, **kwargs)

        monkeypatch.setattr(agent_access, "lock_agent_entry", gated)
        request = asyncio.create_task(editor_client.post(
            f"{base}/changes/{change['id']}/reject", json=change_action(change),
        ))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            path = f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}"
            if revocation == "revoke":
                response = await owner_client.delete(path, params={"expected_revision": member["revision"]})
                assert response.status_code == 204, response.text
            else:
                response = await owner_client.patch(path, json={"expected_revision": member["revision"],
                                                                "role": "viewer"})
                assert response.status_code == 200, response.text
            release.set()
            result = await asyncio.wait_for(request, 20)
        finally:
            release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    if revocation == "revoke":
        # 已撤权：私人作用域不可见，404。
        assert result.status_code == 404, result.text
    else:
        assert result.status_code == 409 and result.json()["code"] == "permission_denied", result.text
    assert await _change_status(app, change["id"]) == "proposed"
    assert await _synopsis(app, project["id"]) == project["synopsis"]


async def test_postgresql_reject_holding_lock_blocks_concurrent_revocation(pg_agent_apps, fake_llm, monkeypatch):
    editor_app, owner_app = pg_agent_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client:
        editor, project, member, base, change = await _paused_proposals(
            editor_app, owner_client, editor_client, fake_llm,
        )
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = service._require_change_write

        async def held(db, entry, user_id, row):
            grant = await original(db, entry, user_id, row)
            if user_id == editor["id"] and authz.holds_gate(db) and not holding.is_set():
                # 拒绝已在锁内判定通过、尚未提交：作者撤权须在 PostgreSQL 上等待本事务。
                pids["reject"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()
            return grant

        monkeypatch.setattr(service, "_require_change_write", held)
        reject = asyncio.create_task(editor_client.post(
            f"{base}/changes/{change['id']}/reject", json=change_action(change),
        ))
        revoke = None
        try:
            await asyncio.wait_for(holding.wait(), 15)
            revoke = asyncio.create_task(owner_client.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
                params={"expected_revision": member["revision"]},
            ))

            async def revoke_blocked():
                while True:
                    async with editor_app.state.sessionmaker() as db:
                        blocked = await db.scalar(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE :holder = ANY(pg_blocking_pids(pid))"
                        ), {"holder": pids["reject"]})
                    if blocked or revoke.done():
                        return

            await asyncio.wait_for(revoke_blocked(), 15)
            assert not revoke.done(), revoke.result().text
            release.set()
            rejected = await asyncio.wait_for(reject, 20)
            revoked = await asyncio.wait_for(revoke, 20)
        finally:
            release.set()
            for work in (reject, revoke):
                if work is not None and not work.done():
                    work.cancel()
            await asyncio.gather(*(work for work in (reject, revoke) if work is not None), return_exceptions=True)
    # 拒绝先于撤权提交（数据库事实）；其提交后的续跑复核 / 最终投影可能已看到撤权，响应为 200 或撤权后的
    # 404 / 409——都不回滚已提交的合法结果（R3）。
    assert rejected.status_code in (200, 404, 409), rejected.text
    if rejected.status_code == 200:
        assert rejected.json()["status"] == "rejected"
    assert revoked.status_code == 204, revoked.text
    assert await _change_status(editor_app, change["id"]) == "rejected"
    async with editor_app.state.sessionmaker() as db:
        member_row = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == editor["id"],
        ))
        assert member_row.status == "revoked"
