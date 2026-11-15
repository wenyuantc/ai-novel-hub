"""真实 PostgreSQL 中共享向量缓存物理 lease 的撤权竞争。"""

import asyncio
import json
from contextlib import suppress

import pytest
from sqlalchemy import select, text

from app.models import ProjectCollaborator
from app.services import ai_dispatch, collaboration_ai_guard, project_access, project_lifecycle
from app.services import retrieval_service as rs
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_retrieval_cache_preflight import _make_world

pytest_plugins = ("tests.test_collaboration_retrieval_postgresql",)


@pytest.fixture(autouse=True)
def _local_server_bypasses_system_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _payload():
    return json.dumps({"object": "list", "model": "embedding-model",
                       "data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()


async def _advisory_probe(sessionmaker, project_id, *, holder_pid=None):
    """独立事务探测目标独占锁，并可核对 holder 实际持有的共享锁。"""
    params = {"namespace": ai_dispatch._LOCK_NAMESPACE, "project_id": project_id}
    async with sessionmaker() as db:
        held = None
        if holder_pid is not None:
            held = await db.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :holder_pid "
                "AND locktype = 'advisory' AND mode = 'ShareLock' AND granted "
                "AND CAST(classid AS BIGINT) = :namespace AND CAST(objid AS BIGINT) = :project_id "
                "AND objsubid = 2)"
            ), {**params, "holder_pid": holder_pid})
        acquired = await db.scalar(text(
            "SELECT pg_try_advisory_xact_lock(CAST(:namespace AS INTEGER), CAST(:project_id AS INTEGER))"
        ), params)
        await db.rollback()
    return held, acquired


async def test_cache_preflight_rechecks_revoke_after_project_row_wait(postgres_retrieval_apps):
    owner_app, guest_app = postgres_retrieval_apps
    async with _server(payload=_payload(), mime="application/json") as (url, _, headers, body, requests):
        headers.set()
        body.set()
        world = await _make_world(owner_app, url)
        embedder = world["embedder"]
        await embedder.embed(["缓存查询"])
        pending = None
        try:
            async with guest_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, world["project"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == world["project"],
                    ProjectCollaborator.user_id == world["actor"],
                ))
                member.status = "revoked"
                member.revision += 1
                await holder.flush()
                pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(rs.embed_query(embedder, "缓存查询"))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, pid), 8)
                async with owner_app.state.sessionmaker() as db:
                    queries = (await db.scalars(text(
                        "SELECT query FROM pg_stat_activity WHERE datname=current_database() "
                        "AND :holder=ANY(pg_blocking_pids(pid))"
                    ), {"holder": pid})).all()
                    assert any("from projects" in query.lower() and "for share" in query.lower() for query in queries)
                await holder.commit()
            with pytest.raises(rs.VisibilityPolicyError):
                await asyncio.wait_for(pending, 8)
            assert len(requests) == 1
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


async def test_cache_preflight_holds_real_shared_lease_until_verification_finishes(
    postgres_retrieval_apps, monkeypatch,
):
    owner_app, guest_app = postgres_retrieval_apps
    async with _server(payload=_payload(), mime="application/json") as (url, _, headers, body, requests):
        headers.set()
        body.set()
        reached, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        armed = False
        send_pid = None
        attempts = []
        original = collaboration_ai_guard._lock_active_project
        original_lock = ai_dispatch._lock_postgres

        async def gated(db, project_id):
            nonlocal send_pid
            await original(db, project_id)
            if armed:
                send_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()

        monkeypatch.setattr(collaboration_ai_guard, "_lock_active_project", gated)
        world = await _make_world(owner_app, url)
        embedder = world["embedder"]
        await embedder.embed(["缓存查询"])

        async def observed_lock(db, project_id, *, write):
            if not write or project_id != world["project"]:
                return await original_lock(db, project_id, write=write)
            # 记录生产轮询的实际返回值；不消耗或替换 SQL 结果，也不改变锁行为。
            scalar = db.scalar

            async def observe(statement, *args, **kwargs):
                result = await scalar(statement, *args, **kwargs)
                if not attempted.is_set():
                    attempts.append(result)
                    attempted.set()
                return result

            db.scalar = observe
            try:
                return await original_lock(db, project_id, write=write)
            finally:
                db.scalar = scalar

        monkeypatch.setattr(ai_dispatch, "_lock_postgres", observed_lock)
        monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 8.0)
        armed = True
        cached = asyncio.create_task(rs.embed_query(embedder, "缓存查询"))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            assert send_pid is not None
            held, acquired = await _advisory_probe(
                guest_app.state.sessionmaker, world["project"], holder_pid=send_pid,
            )
            assert held is True and acquired is False

            async def change_grant():
                async with guest_app.state.sessionmaker() as db:
                    await project_access.require_project(
                        db, world["owner"], world["project"], "content.edit", for_update=True, visibility_write=True,
                    )
                    member = await db.scalar(select(ProjectCollaborator).where(
                        ProjectCollaborator.project_id == world["project"],
                        ProjectCollaborator.user_id == world["actor"],
                    ))
                    member.status = "revoked"
                    member.revision += 1
                    await db.commit()

            revoke = asyncio.create_task(change_grant())
            # pg_try_advisory_xact_lock 非阻塞，pg_blocking_pids 看不到轮询等待。
            await asyncio.wait_for(attempted.wait(), 6)
            assert attempts == [False]
            assert not revoke.done()
            async with guest_app.state.sessionmaker() as db:
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == world["project"],
                    ProjectCollaborator.user_id == world["actor"],
                ))
                assert member.status == "accepted"
                prior_revision = member.revision
            release.set()
            vector, warning = await asyncio.wait_for(cached, 8)
            assert warning == "" and vector.tolist() == [1.0, 0.0]
            await asyncio.wait_for(revoke, 8)
            async with guest_app.state.sessionmaker() as db:
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == world["project"],
                    ProjectCollaborator.user_id == world["actor"],
                ))
                assert member.status == "revoked" and member.revision == prior_revision + 1
            _, acquired = await _advisory_probe(guest_app.state.sessionmaker, world["project"])
            assert acquired is True
            with pytest.raises(rs.VisibilityPolicyError):
                await rs.embed_query(embedder, "缓存查询")
            assert len(requests) == 1
        finally:
            release.set()
            for task in (cached, revoke):
                if task is not None and not task.done():
                    task.cancel()
                if task is not None:
                    with suppress(asyncio.CancelledError, rs.VisibilityPolicyError):
                        await task
