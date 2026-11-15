"""E4i-e：真实 PostgreSQL 上协作者待确认轮次的撤权受限收尾与续跑重验。

1. 撤权与续跑并发（裁决 11）：
   - 续跑先持锁：编辑者确认最后一项后自动续跑，停在续跑锁内（revalidate_resume 之后、提交之前）；作者撤权（第二个应用
     实例）在 PostgreSQL 上被其阻塞（pg_blocking_pids 观测）。放行后续跑提交为 queued、撤权随后提交；领取锁内复核失效，
     受限收尾为 failed，零模型调用。
   - 撤权先提交：续跑停在取锁之前，作者撤权提交后放行 → 续跑失败关闭，轮次停在 paused；系统复查受限收尾为 failed。
2. 续跑时冻结向量选择的锁内重验：paused → queued 之后作者改动向量配置，领取构建锁内失败关闭，零作者向量与模型调用。
编排只用 Event 与 pg_blocking_pids，不用固定等待。
"""

import asyncio

import httpx
import pytest
from sqlalchemy import select, text

from app.models import AgentChange, ProjectCollaborator
from app.services.agent import service
from app.services.task_engine.handlers import agent as agent_handler
from tests import pg_support
from tests.helpers import wait_task
from tests.test_agent_entries_e4id import _detail
from tests.test_agent_mcp_skills import change_action
from tests.test_agent_revocation_e4ie import (
    COLLAB_FAILED,
    STOP,
    TERMINAL,
    _change_owner_embedding,
    _changes,
    _log_count,
    _task,
    _wait_db,
    _world,
)
from tests.test_agent_runtime import call


@pytest.fixture
async def pg_agent_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="Agent 撤权收尾测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2, start_first_engine=True,
    ) as apps:
        yield apps


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _paused_single(app, world, fake_llm):
    fake_llm.push_tools([call("update_project", {"synopsis": "PG 采纳"}, "g1")])
    from tests.test_agent_entries_e4id import _send

    turn = await _send(world.editor_client, world.base, world.cid)
    assert (await wait_task(world.editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
    view = (await _detail(world.editor_client, world.base, world.cid))["changes"][0]
    return turn, view


async def _blocked_by(app, holder_pid, work):
    while True:
        async with app.state.sessionmaker() as db:
            blocked = await db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE :holder = ANY(pg_blocking_pids(pid))"
            ), {"holder": holder_pid})
        if blocked or work.done():
            return


async def _member_status(app, project_id, user_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectCollaborator.status).where(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == user_id,
        ))


async def test_postgresql_resume_holding_lock_then_revocation_fails_queued_turn(
    pg_agent_apps, fake_llm, monkeypatch,
):
    editor_app, owner_app = pg_agent_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client2:
        # 作者经第二个应用实例操作（独立连接池），编辑者经第一个实例（运行任务引擎）。
        world = await _world(editor_app, owner_client2, lambda: editor_client)
        turn, view = await _paused_single(editor_app, world, fake_llm)
        engine = editor_app.state.task_engine
        await engine.stop()
        holding, release = asyncio.Event(), asyncio.Event()
        pids: dict[str, int] = {}
        original = agent_handler.AgentHandler.revalidate_resume

        async def held(self, db, ctx, task):
            await original(self, db, ctx, task)
            if task.user_id == world.editor["id"] and not holding.is_set():
                # 续跑已在 gate → User → 发送写门禁 → 作品锁内通过复核，尚未提交 queued。
                pids["resume"] = await db.scalar(text("SELECT pg_backend_pid()"))
                holding.set()
                await release.wait()

        monkeypatch.setattr(agent_handler.AgentHandler, "revalidate_resume", held)
        confirm = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{view['id']}/confirm", json=change_action(view),
        ))
        revoke = None
        try:
            await asyncio.wait_for(holding.wait(), 20)
            calls = len(fake_llm.tool_calls)
            revoke = asyncio.create_task(owner_client2.delete(
                f"/api/v1/projects/{world.project['id']}/collaborators/{world.editor['id']}",
                params={"expected_revision": world.member["revision"]},
            ))
            await asyncio.wait_for(_blocked_by(editor_app, pids["resume"], revoke), 20)
            assert not revoke.done(), revoke.result().text
            release.set()
            confirmed = await asyncio.wait_for(confirm, 30)
            revoked = await asyncio.wait_for(revoke, 30)
        finally:
            release.set()
            for work in (confirm, revoke):
                if work is not None and not work.done():
                    work.cancel()
            await asyncio.gather(*(work for work in (confirm, revoke) if work is not None), return_exceptions=True)
            await engine.start()
        # 确认与续跑先提交（数据库事实）；响应投影可能已看到撤权。
        assert confirmed.status_code in (200, 404, 409), confirmed.text
        assert revoked.status_code == 204, revoked.text
        assert await _member_status(editor_app, world.project["id"], world.editor["id"]) == "revoked"
        row = await _wait_db(editor_app, turn["id"], TERMINAL, within=30)
        assert (row.status, row.error) == ("failed", COLLAB_FAILED)
        assert [item.status for item in await _changes(editor_app, turn["id"])] == ["executed"]
        assert len(fake_llm.tool_calls) == calls


async def test_postgresql_revocation_before_resume_lock_leaves_paused_then_restricted_finish(
    pg_agent_apps, fake_llm, monkeypatch,
):
    editor_app, owner_app = pg_agent_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client2:
        # 作者经第二个应用实例操作（独立连接池），编辑者经第一个实例（运行任务引擎）。
        world = await _world(editor_app, owner_client2, lambda: editor_client)
        fake_llm.push_tools([call("update_project", {"synopsis": "PG 一"}, "h1"),
                             call("update_project", {"theme": "PG 二"}, "h2")])
        from tests.test_agent_entries_e4id import _send

        turn = await _send(world.editor_client, world.base, world.cid)
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
        first, second = (await _detail(editor_client, world.base, world.cid))["changes"]
        # 先拒绝第二项（仍有待确认项，不续跑），再确认第一项触发续跑，停在续跑取锁之前。
        rejected = await editor_client.post(f"{world.base}/changes/{second['id']}/reject",
                                            json=change_action(second))
        assert rejected.status_code == 200, rejected.text
        entered, release = asyncio.Event(), asyncio.Event()
        original = service.maybe_resume

        async def gated(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(service, "maybe_resume", gated)
        confirm = asyncio.create_task(editor_client.post(
            f"{world.base}/changes/{first['id']}/confirm", json=change_action(first),
        ))
        try:
            await asyncio.wait_for(entered.wait(), 20)
            calls = len(fake_llm.tool_calls)
            revoked = await owner_client2.delete(
                f"/api/v1/projects/{world.project['id']}/collaborators/{world.editor['id']}",
                params={"expected_revision": world.member["revision"]},
            )
            assert revoked.status_code == 204, revoked.text
            release.set()
            confirmed = await asyncio.wait_for(confirm, 30)
        finally:
            release.set()
            if not confirm.done():
                confirm.cancel()
            await asyncio.gather(confirm, return_exceptions=True)
        # 确认已提交；续跑在锁内按原 grant 复核失败关闭（不排队、不发送）。
        assert confirmed.status_code in (404, 409), confirmed.text
        assert (await _task(editor_app, turn["id"])).status == "paused"
        assert await editor_app.state.task_engine.scan_collaboration_paused() >= 1
        row = await _task(editor_app, turn["id"])
        assert (row.status, row.error) == ("failed", COLLAB_FAILED)
        assert [item.status for item in await _changes(editor_app, turn["id"])] == ["executed", "rejected"]
        assert len(fake_llm.tool_calls) == calls


async def test_postgresql_resumed_turn_rechecks_frozen_embedding_inside_lock(pg_agent_apps, fake_llm):
    from tests.test_agent_handler_e4ib import _send as admit
    from tests.test_agent_tools_e4ic import _retrieval_world

    app = pg_agent_apps[0]
    async with _client(app) as owner_client, _client(app) as editor_client:
        world = await _retrieval_world(app, owner_client, editor_client, allow_owner_ai=True)
        world.owner_client = owner_client
        base = f"/api/v1/projects/{world.project['id']}/agent"
        fake_llm.push_tools([call("update_project", {"synopsis": "PG 续跑前"}, "v1")])
        turn = await admit(app, world.editor["id"], world.project["id"], world.conversation_id)
        assert (await wait_task(editor_client, turn, within=20, statuses=STOP))["status"] == "paused"
        assert (await _task(app, turn["id"])).input["_shared_embedding"]["mode"] == "vector"
        view = (await _detail(editor_client, base, world.conversation_id))["changes"][0]
        embeds, calls, logs = len(fake_llm.embed_calls), len(fake_llm.tool_calls), await _log_count(app, turn["id"])
        engine = app.state.task_engine
        await engine.stop()
        try:
            confirmed = await editor_client.post(f"{base}/changes/{view['id']}/confirm", json=change_action(view))
            assert confirmed.status_code == 200 and confirmed.json()["status"] == "executed", confirmed.text
            assert (await _task(app, turn["id"])).status == "queued"
            await _change_owner_embedding(app, world)
        finally:
            await engine.start()
        row = await _wait_db(app, turn["id"], TERMINAL, within=30)
        assert (row.status, row.error) == ("failed", "模型配置已失效，请重新选择")
        assert len(fake_llm.embed_calls) == embeds
        assert len(fake_llm.tool_calls) == calls
        assert await _log_count(app, turn["id"]) == logs
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(AgentChange.status).where(AgentChange.id == view["id"])) == "executed"


async def test_postgresql_revocation_before_paused_commit_fails_running_turn(pg_agent_apps, fake_llm, monkeypatch):
    """断点已提交、paused 终态核验之前由第二个实例撤权（跨实例、无通知）。

    受限收尾为 failed、提案作废，不停在 running。
    """
    editor_app, owner_app = pg_agent_apps
    async with _client(editor_app) as editor_client, _client(owner_app) as owner_client2:
        world = await _world(editor_app, owner_client2, lambda: editor_client)
        engine = editor_app.state.task_engine
        reached, release = asyncio.Event(), asyncio.Event()
        original = engine.complete_state

        async def held(task, **kwargs):
            if (kwargs.get("status") == "paused" and task is not None and task.user_id == world.editor["id"]
                    and not reached.is_set()):
                reached.set()
                await release.wait()
            return await original(task, **kwargs)

        monkeypatch.setattr(engine, "complete_state", held)
        fake_llm.push_tools([call("update_project", {"synopsis": "PG 一"}, "k1"),
                             call("update_project", {"theme": "PG 二"}, "k2")])
        from tests.test_agent_entries_e4id import _send

        turn = await _send(world.editor_client, world.base, world.cid)
        try:
            await asyncio.wait_for(reached.wait(), 20)
            calls = len(fake_llm.tool_calls)
            revoked = await owner_client2.delete(
                f"/api/v1/projects/{world.project['id']}/collaborators/{world.editor['id']}",
                params={"expected_revision": world.member["revision"]},
            )
            assert revoked.status_code == 204, revoked.text
        finally:
            release.set()
        row = await _wait_db(editor_app, turn["id"], TERMINAL, within=30)
        assert (row.status, row.error, row.execution_key) == ("failed", COLLAB_FAILED, None)
        assert [(item.status, item.error) for item in await _changes(editor_app, turn["id"])] == [
            ("failed", service.COLLABORATION_CHANGE_VOIDED),
        ] * 2
        assert len(fake_llm.tool_calls) == calls
        async with editor_app.state.sessionmaker() as db:
            assert await service.active_turn(db, world.cid) is None
