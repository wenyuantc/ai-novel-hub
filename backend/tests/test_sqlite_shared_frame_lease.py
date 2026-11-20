"""SQLite 共享读帧租约的并发兼容、撤权串行化、WAL 保守回退与防伪造回归。"""

import asyncio
import time

import pytest
from sqlalchemy import text

from app.models import Project, User
from app.services import ai_config_service, ai_dispatch, authorization_service
from app.services.task_engine import stream_evidence
from app.services.task_engine.stream_lease import (
    _task_frame_lease,
    is_sqlite_shared_frame_lease,
    task_sse_response,
)
from tests.stream_helpers import _tcp_server, mount_stream_response
from tests.test_collaboration_task_privacy import _setup, _tasks
from tests.test_task_sse_scope import _binding


async def _valid(_db, _task, _project, _event, _data, evidence):
    return evidence == "ok"


async def test_sqlite_shared_frame_lease_does_not_block_on_send_lease_or_peer_frame(
    app, client, make_client,
):
    """默认回滚日志模式下：持 RESERVED 的写锁（模拟 _send_lease）与多个 _task_frame_lease 互不阻塞。"""
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])

    # 模拟 _send_lease 长时间持有 SQLite RESERVED 写锁（不提交）
    send_db = app.state.sessionmaker()
    try:
        send_db.info["authenticated_actor"] = binding.actor
        await authorization_service.require_authenticated_for_send(send_db, binding.actor)
        await ai_dispatch._lock(send_db, project["id"], write=False)
        await ai_config_service.lock_config_owner(send_db, owner["id"], write=False)
        assert not await is_sqlite_shared_frame_lease(send_db)

        t0 = time.monotonic()
        async with _task_frame_lease(app.state.sessionmaker, binding, "delta", {"text": "a"}) as (db1, _, _):
            assert await is_sqlite_shared_frame_lease(db1)
            # 第二个并发帧租约也应立即进入（SHARED + SHARED 兼容）
            async with _task_frame_lease(app.state.sessionmaker, binding, "delta", {"text": "b"}) as (db2, _, _):
                assert await is_sqlite_shared_frame_lease(db2)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"shared frame leases blocked on send lease: {elapsed:.2f}s"
    finally:
        await send_db.close()


async def test_sqlite_shared_frame_lease_rejects_forged_cross_task_or_mutated_session(
    app, client, make_client,
):
    """普通会话、跨协程、嵌套事务、提交后或退出后的会话均不能通过 is_sqlite_shared_frame_lease。"""
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])

    async with app.state.sessionmaker() as plain:
        await plain.scalar(text("SELECT 1"))
        assert not await is_sqlite_shared_frame_lease(plain)

    saved = []
    async with _task_frame_lease(app.state.sessionmaker, binding, "delta", {"text": "a"}) as (db, _, _):
        saved.append(db)
        assert await is_sqlite_shared_frame_lease(db)
        # 跨协程不能复用
        assert not await asyncio.create_task(is_sqlite_shared_frame_lease(db))
        # 嵌套事务期间不能复用
        nested = await db.begin_nested()
        try:
            assert not await is_sqlite_shared_frame_lease(db)
        finally:
            await nested.rollback()
        assert await is_sqlite_shared_frame_lease(db)
        # 一旦提交，原根事务结束，不再有效
        await db.commit()
        assert not await is_sqlite_shared_frame_lease(db)
    assert not await is_sqlite_shared_frame_lease(saved[0])


async def test_sqlite_shared_frame_lease_releases_shared_lock_on_cancel_and_timeout(
    app, client, make_client, monkeypatch,
):
    """帧租约内超时或取消后必须立刻释放 SHARED 锁，后续写方（改密/撤权）能正常提交。"""
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])

    entered, never = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", _valid)

    async def events():
        yield "delta", {"text": "不会残留锁"}, "ok"

    async def slow_send(message):
        if b"event: delta" in message.get("body", b""):
            entered.set()
            await never.wait()

    await task_sse_response(
        events(), sessionmaker=app.state.sessionmaker, binding=binding, frame_timeout=0.1,
    ).stream_response(slow_send)
    assert entered.is_set()

    # 超时退出后写方立即提交，不被残留 SHARED 锁阻塞
    async with app.state.sessionmaker() as db:
        user = await db.get(User, editor["id"])
        user.token_version += 1
        await db.commit()


async def test_sqlite_wal_mode_falls_back_to_write_lock_and_keeps_revocation_serialized(
    app, client, make_client, monkeypatch,
):
    """若 SQLite 手工开启 WAL 模式，SHARED 读锁不阻塞写方提交；帧租约必须自动回退写锁以维持撤权串行化。"""
    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])

    async with app.state.engine.connect() as conn:
        mode = (await conn.exec_driver_sql("PRAGMA journal_mode=WAL")).scalar()
        assert str(mode).lower() == "wal"

    shared_seen: list[bool] = []

    async def verify(db, _task, _project, _event, _data, evidence):
        shared_seen.append(await is_sqlite_shared_frame_lease(db))
        return evidence == "ok"

    entered, release, next_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def events():
        yield "snapshot", {"text": "WAL先发"}, "ok"
        await next_frame.wait()
        yield "delta", {"text": "WAL撤权后"}, "ok"

    async def endpoint():
        return task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding)

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)
    mount_stream_response(app, "/_test/task-frame", endpoint)
    async with _tcp_server(app, entered, release) as tcp:
        pending = asyncio.create_task(tcp.get("/_test/task-frame"))
        revoke = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert shared_seen == [False], "WAL mode must not use SQLite SHARED frame lease"
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
            assert "WAL先发" in response.text
            assert "WAL撤权后" not in response.text and "permission_denied" in response.text
        finally:
            release.set()
            next_frame.set()
            for item in (pending, revoke):
                if item is not None and not item.done():
                    item.cancel()
            await asyncio.gather(*(item for item in (pending, revoke) if item is not None), return_exceptions=True)


async def test_sqlite_shared_frame_lease_verifies_missing_project_and_user(app, client, make_client):
    """共享读路径下 _lock 与 lock_config_owner 对不存在目标仍按原异常语义抛 NotFoundError。"""
    from app.core.errors import NotFoundError

    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])

    async with _task_frame_lease(app.state.sessionmaker, binding, "delta", {"text": "a"}) as (db, _, _):
        assert await is_sqlite_shared_frame_lease(db)
        with pytest.raises(NotFoundError):
            await ai_dispatch._lock(db, project["id"] + 999999, write=False)
        with pytest.raises(NotFoundError):
            await ai_config_service.lock_config_owner(db, owner["id"] + 999999, write=False)
        # 仍可正常查到存在对象
        assert await db.scalar(text(f"SELECT id FROM {Project.__tablename__} WHERE id = :pid"), {"pid": project["id"]})


async def test_sqlite_shared_frame_lease_final_visibility_avoids_pending_commit_deadlock(app, client, make_client):
    """写方在 COMMIT（持 SQLite PENDING 锁）等待帧租约释放 SHARED 时，personal_projection._final_visibility
    必须复用当前帧租约连接而非再开第二条连接，避免 SHARED→新连接→PENDING→SHARED 三方死锁。"""
    from app.services.personal_projection import project_payload

    guest = make_client()
    owner, editor, project, member = await _setup(app, client, guest)
    tasks = await _tasks(app, owner["id"], editor["id"], project["id"], member["invitation_id"], member["revision"])
    binding = await _binding(app, editor["id"], *tasks["valid"])

    writer = None
    try:
        async with _task_frame_lease(app.state.sessionmaker, binding, "snapshot", {"text": ""}) as (db, _, _):
            assert await is_sqlite_shared_frame_lease(db)
            # 启动一个并发写方：先 UPDATE 再 COMMIT（进入 PENDING 等本连接释放 SHARED）。
            writer_started = asyncio.Event()

            async def pending_writer():
                async with app.state.sessionmaker() as writer_db:
                    await writer_db.execute(
                        text("UPDATE projects SET updated_at = updated_at WHERE id = :pid"),
                        {"pid": project["id"]},
                    )
                    writer_started.set()
                    await writer_db.commit()

            writer = asyncio.create_task(pending_writer())
            await asyncio.wait_for(writer_started.wait(), 5)
            await asyncio.sleep(0.05)  # 让 writer_db.commit() 进入 SQLite PENDING
            projected = await asyncio.wait_for(
                project_payload(db, editor["id"], project["id"], {"task_id": tasks["valid"][0], "note": "ok"}),
                2.0,
            )
            assert projected["note"] == "ok"
        # 退出帧租约释放 SHARED 后，writer 的 COMMIT 应立刻成功。
        await asyncio.wait_for(writer, 5)
    finally:
        if writer is not None and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
