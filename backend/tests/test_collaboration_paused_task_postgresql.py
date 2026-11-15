"""E5-a 真实 PostgreSQL（两实例共享库）：待审核协作任务的 finish / resume 与撤权、系统收尾与本人操作的两种先后。

每个用例让先到的一方在持有共同锁序（gate → User → [发送写门禁] → 作品父行 → Task）后停住，用 pg_blocking_pids
证明另一方确实在数据库层等待，再放行；最终结果必须与提交顺序一致，不能出现「撤权后仍结束/继续」或「已结束后又被
系统改写」。
"""

import asyncio
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text, update

from app.models import Task, User
from app.services import collaboration_service, rich_document_review
from app.services.collaboration_task_access import GRANT_KEY
from app.services.task_engine import engine as engine_module
from tests.helpers import create_project, register, task_params
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_paused_apps = _postgres_fixture
MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES


def _clients(owner_app, guest_app):
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver"),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver"),
    )


async def _world(maker, owner, guest):
    await register(owner, "pg-paused-owner")
    actor = await register(guest, "pg-paused-guest")
    project = await create_project(owner)
    invited = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "pg-paused-guest", "role": "editor",
    })
    assert invited.status_code == 201, invited.text
    invite = invited.json()
    accepted = await guest.post(f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
                                json={"expected_revision": invite["revision"]})
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    async with maker() as db:
        task = Task(user_id=actor["id"], project_id=project["id"], kind="outline.continue", status="paused",
                    result={"paused": True}, input={
                        "request": {"project_id": project["id"]}, "_task_actor_token_version": 0,
                        GRANT_KEY: {"invitation_id": member["invitation_id"], "revision": member["revision"]},
                    })
        db.add(task)
        await db.commit()
        identity = (task.id, task.statistics_key)
    return project, actor, member, identity


async def _row(maker, task_id):
    async with maker() as db:
        return await db.get(Task, task_id)


async def _cancel(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _gate_once(monkeypatch, target, name, holder, reached, release, *, pid_from=0):
    """首次调用在原锁内停住并记录持锁连接；之后的调用直接透传。"""
    original = getattr(target, name)
    calls = []

    async def gated(*args, **kwargs):
        result = await original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 1:
            holder["pid"] = await args[pid_from].scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(target, name, staticmethod(gated) if isinstance(target, type) else gated)


def _gate_revoke(monkeypatch, holder, reached, release):
    original = collaboration_service.add_success

    async def gated(db, snapshot):
        event = await original(db, snapshot)
        if snapshot.action in ("revoke", "update"):
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
        return event

    monkeypatch.setattr(collaboration_service, "add_success", gated)


def _user_action(guest, action, identity):
    return guest.post(f"/api/v1/tasks/{identity[0]}/{action}", params=task_params(identity))


# ---------------- finish / resume 与撤权 ----------------


@pytest.mark.parametrize("action", ["finish", "resume"])
async def test_postgres_revoke_commits_before_waiting_user_action(postgres_paused_apps, monkeypatch, action):
    owner_app, guest_app = postgres_paused_apps
    owner, guest = _clients(owner_app, guest_app)
    maker = owner_app.state.sessionmaker
    async with owner, guest:
        project, actor, member, identity = await _world(maker, owner, guest)
        holder, reached, release = {}, asyncio.Event(), asyncio.Event()
        _gate_revoke(monkeypatch, holder, reached, release)
        revoke = asyncio.create_task(owner.delete(
            f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}",
            params={"expected_revision": member["revision"]},
        ))
        user = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            user = asyncio.create_task(_user_action(guest, action, identity))
            # 撤权持锁未提交：本人操作在锁内等待，不能按旧授权提前提交。
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            assert (await _row(maker, identity[0])).status == "paused"
            release.set()
            assert (await asyncio.wait_for(revoke, 10)).status_code == 204
            response = await asyncio.wait_for(user, 10)
            assert response.status_code == 404, response.text
            assert (await _row(maker, identity[0])).status == "paused"
            # 系统随后按数据库事实收尾（此处由另一实例的复查执行）。
            assert await guest_app.state.task_engine.scan_collaboration_paused() == 1
            row = await _row(maker, identity[0])
            assert (row.status, row.error) == ("failed", MESSAGES["collaboration_revoked"])
        finally:
            release.set()
            await _cancel(revoke, user)


@pytest.mark.parametrize("action", ["finish", "resume"])
async def test_postgres_user_action_commits_before_waiting_revoke(postgres_paused_apps, monkeypatch, action):
    owner_app, guest_app = postgres_paused_apps
    owner, guest = _clients(owner_app, guest_app)
    maker = owner_app.state.sessionmaker
    async with owner, guest:
        project, actor, member, identity = await _world(maker, owner, guest)
        holder, reached, release = {}, asyncio.Event(), asyncio.Event()
        # finish/resume 都在 _lock_user_task_project 之后读取待审核提案：在此停住即证明已持有原锁。
        _gate_once(monkeypatch, rich_document_review, "pending_for_task", holder, reached, release)
        # 撤权在取得锁、写完业务后再停住：本人响应在撤权提交前完成投影，使两次提交的先后与响应一一对应。
        revoke_holder, revoke_reached, revoke_release = {}, asyncio.Event(), asyncio.Event()
        _gate_revoke(monkeypatch, revoke_holder, revoke_reached, revoke_release)
        user = asyncio.create_task(_user_action(guest, action, identity))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            revoke = asyncio.create_task(owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{actor['id']}",
                params={"expected_revision": member["revision"]},
            ))
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            assert not revoke_reached.is_set()
            release.set()
            response = await asyncio.wait_for(user, 10)
            assert response.status_code == (200 if action == "finish" else 202), response.text
            await asyncio.wait_for(revoke_reached.wait(), 10)
            revoke_release.set()
            assert (await asyncio.wait_for(revoke, 10)).status_code == 204
            row = await _row(maker, identity[0])
            if action == "finish":
                # 结束先提交即有效：撤权与系统复查都不能事后改写。
                assert (row.status, row.error) == ("succeeded", "")
                assert await guest_app.state.task_engine.scan_collaboration_paused() == 0
                assert (await _row(maker, identity[0])).status == "succeeded"
            else:
                # 继续先提交后任务回到排队：不属于待审核收尾，由领取锁内复核（E4-a）按原轮次失败关闭。
                assert row.status == "queued"
                assert await guest_app.state.task_engine.scan_collaboration_paused() == 0
                await guest_app.state.task_engine.start()
                try:
                    async with asyncio.timeout(20):
                        while True:
                            row = await _row(maker, identity[0])
                            if row.status != "queued":
                                break
                            await asyncio.sleep(0.05)
                finally:
                    await guest_app.state.task_engine.stop()
                assert (row.status, row.error) == ("failed", MESSAGES["collaboration_revoked"])
        finally:
            release.set()
            revoke_release.set()
            await _cancel(user, revoke)


# ---------------- 系统收尾与本人 finish / resume 争用 ----------------


async def _stale_token_world(maker, owner, guest):
    """改密后原受理令牌失效：本人用新会话仍能看到并操作待审核任务，系统复查则按原令牌判定失效。"""
    project, actor, member, identity = await _world(maker, owner, guest)
    async with maker() as db:
        await db.execute(update(User).where(User.id == actor["id"]).values(token_version=User.token_version + 1))
        await db.commit()
    login = await guest.post("/api/v1/auth/login", json={"username": "pg-paused-guest", "password": "secret123"})
    assert login.status_code == 200, login.text
    assert (await guest.get(f"/api/v1/tasks/{identity[0]}", params=task_params(identity))).status_code == 200
    return project, actor, member, identity


@pytest.mark.parametrize("action", ["finish", "resume"])
async def test_postgres_system_cleanup_commits_before_waiting_user_action(postgres_paused_apps, monkeypatch, action):
    owner_app, guest_app = postgres_paused_apps
    owner, guest = _clients(owner_app, guest_app)
    maker = owner_app.state.sessionmaker
    async with owner, guest:
        _project, _actor, _member, identity = await _stale_token_world(maker, owner, guest)
        holder, reached, release = {}, asyncio.Event(), asyncio.Event()
        _gate_once(monkeypatch, engine_module.TaskEngine, "_collaboration_claim_failure", holder, reached, release)
        cleanup = asyncio.create_task(owner_app.state.task_engine.scan_collaboration_paused())
        user = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            user = asyncio.create_task(_user_action(guest, action, identity))
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            release.set()
            assert await asyncio.wait_for(cleanup, 10) == 1
            response = await asyncio.wait_for(user, 10)
            # 系统收尾先提交：本人看到的已是已结束记录，结束/继续被状态冲突拒绝，不能复活任务。
            assert response.status_code == 409, response.text
            row = await _row(maker, identity[0])
            assert (row.status, row.error) == ("failed", MESSAGES["collaboration_account_unavailable"])
        finally:
            release.set()
            await _cancel(cleanup, user)


@pytest.mark.parametrize("action", ["finish", "resume"])
async def test_postgres_user_action_commits_before_waiting_system_cleanup(postgres_paused_apps, monkeypatch, action):
    owner_app, guest_app = postgres_paused_apps
    owner, guest = _clients(owner_app, guest_app)
    maker = owner_app.state.sessionmaker
    async with owner, guest:
        _project, _actor, _member, identity = await _stale_token_world(maker, owner, guest)
        holder, reached, release = {}, asyncio.Event(), asyncio.Event()
        _gate_once(monkeypatch, rich_document_review, "pending_for_task", holder, reached, release)
        user = asyncio.create_task(_user_action(guest, action, identity))
        cleanup = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            # 预筛在锁外已判定失效；锁内收尾须等待本人事务结束后再按原轮次 CAS。
            cleanup = asyncio.create_task(owner_app.state.task_engine.scan_collaboration_paused())
            await asyncio.wait_for(_wait_for_blocker(maker, holder["pid"]), 10)
            release.set()
            response = await asyncio.wait_for(user, 10)
            assert response.status_code == (200 if action == "finish" else 202), response.text
            assert await asyncio.wait_for(cleanup, 10) == 0
            row = await _row(maker, identity[0])
            assert row.status == ("succeeded" if action == "finish" else "queued") and row.error == ""
        finally:
            release.set()
            await _cancel(user, cleanup)


async def test_postgres_cleanup_skips_round_replaced_after_precheck(postgres_paused_apps):
    """预筛之后任务换了入队轮次（继续后再次暂停）或 UUID：锁内按原身份 CAS，不误伤新一轮。"""
    owner_app, guest_app = postgres_paused_apps
    owner, guest = _clients(owner_app, guest_app)
    maker = owner_app.state.sessionmaker
    async with owner, guest:
        _project, actor, _member, identity = await _stale_token_world(maker, owner, guest)
        async with maker() as db:
            row = await db.get(Task, identity[0])
            grant = (row.input[GRANT_KEY]["invitation_id"], row.input[GRANT_KEY]["revision"])
            old_round, project_id = row.queued_at, row.project_id
            row.queued_at = old_round + timedelta(seconds=1)
            await db.commit()
        engine = owner_app.state.task_engine
        assert not await engine._fail_collaboration_paused(
            identity[0], identity[1], old_round, actor["id"], project_id, grant,
        )
        assert not await engine._fail_collaboration_paused(
            identity[0], str(uuid4()), old_round + timedelta(seconds=1), actor["id"], project_id, grant,
        )
        assert (await _row(maker, identity[0])).status == "paused"
        # 正对照：按当前轮次即可收尾。
        assert await engine._fail_collaboration_paused(
            identity[0], identity[1], old_round + timedelta(seconds=1), actor["id"], project_id, grant,
        )
        row = await _row(maker, identity[0])
        assert (row.status, row.error) == ("failed", MESSAGES["collaboration_account_unavailable"])
