"""E6-a：kb.index 协作受理与撤权在真实 PostgreSQL 跨连接的两种提交顺序。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.core.errors import AppError, NotFoundError
from app.models import KbDocument, Task, User
from app.services import collaboration_service, kb_index_service, project_ai_config, task_service
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_access_postgresql import _assert_project_row_locked, _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def _setup(owner_app, owner, guest):
    await register(owner, "pg-e6a-owner")
    guest_user = await register(guest, "pg-e6a-guest")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invited = await owner.post(f"{base}/collaborators", json={
        "username": "pg-e6a-guest", "role": "editor", "allow_owner_ai": True,
    })
    assert invited.status_code == 201, invited.text
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept",
        json={"expected_revision": invited.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    await create_ai_config(owner, name="作者向量", model_type="embedding", model="owner-emb")
    await create_ai_config(guest, name="个人向量", model_type="embedding", model="guest-emb")
    async with owner_app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="资料.txt", sha256="a" * 64,
                         text="天剑宗位于北境。", status="ready_keyword_only")
        db.add(doc)
        await db.flush()
        await kb_index_service.chunk_document(db, doc)
        await db.commit()
        doc_id = doc.id
    return project["id"], guest_user["id"], base, accepted.json(), doc_id


async def _admit(app, user_id, project_id, doc_id):
    async with app.state.sessionmaker() as db:
        user = await db.get(User, user_id)
        db.info["authenticated_actor"] = AuthenticatedActor(user.id, user.token_version)
        return await task_service.create_task(db, app.state.task_engine, user_id, "kb.index", {
            "project_id": project_id, "mode": "document", "document_id": doc_id,
        })


async def test_revoke_commits_before_collaborator_index_admission(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project_id, guest_id, base, member, doc_id = await _setup(owner_app, owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = collaboration_service.add_success

        async def gated(db, snapshot):
            event = await original(db, snapshot)
            if snapshot.action == "revoke":
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return event

        monkeypatch.setattr(collaboration_service, "add_success", gated)
        revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
            "expected_revision": member["revision"],
        }))
        admit = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            await _assert_project_row_locked(guest_app.state.sessionmaker, project_id)
            admit = asyncio.create_task(_admit(guest_app, guest_id, project_id, doc_id))
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
            with pytest.raises(AppError) as rejected:
                await asyncio.wait_for(admit, 8)
            assert rejected.value.status_code in (403, 404)
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(func.count(Task.id))) == 0
                doc = await db.get(KbDocument, doc_id)
                assert doc.status == "ready_keyword_only" and doc.index_task_key is None
            assert not fake_llm.embed_calls
        finally:
            release.set()
            for task in (revoke, admit):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, AppError):
                        await task


async def test_admission_commits_before_revoke_then_build_and_claim_fail_closed(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project_id, guest_id, base, member, doc_id = await _setup(owner_app, owner, guest)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = project_ai_config.resolve_shared_embedding

        async def gated(db, *args, **kwargs):
            selection = await original(db, *args, **kwargs)
            holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            await release.wait()
            return selection

        monkeypatch.setattr(project_ai_config, "resolve_shared_embedding", gated)
        admit = asyncio.create_task(_admit(guest_app, guest_id, project_id, doc_id))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project_id)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
                "expected_revision": member["revision"],
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 8)
            release.set()
            try:
                out_id = (await asyncio.wait_for(admit, 8)).id
            except NotFoundError:
                # 受理已提交；撤权在返回前的任务投影复核之前提交时，任务对其隐藏（合法赢家，E6-b 复核发现的时序）。
                out_id = None
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
        finally:
            release.set()
            for task in (admit, revoke):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, AppError):
                        await task
        monkeypatch.setattr(project_ai_config, "resolve_shared_embedding", original)
        async with owner_app.state.sessionmaker() as db:
            row = await db.scalar(select(Task).where(Task.user_id == guest_id, Task.kind == "kb.index"))
            assert out_id in (None, row.id)
            out_id = row.id
            assert row.user_id == guest_id and row.status == "queued"
            assert row.input["_collaboration_grant"]["invitation_id"] == member["invitation_id"]
            assert (await db.get(KbDocument, doc_id)).index_task_key == row.statistics_key
        # 受理已提交而授权随后撤销：模拟领取后的 build 失败关闭，零发送。
        async with guest_app.state.sessionmaker() as db:
            task = await db.get(Task, out_id)
            queued = await kb_index_service.queued_document_claim(db, task)
            execution = from_queued_task(task, str(uuid4()))
            await db.execute(update(Task).where(Task.id == task.id).values(
                status="running", execution_key=execution.execution_key,
            ))
            await kb_index_service.activate_document_claim(db, queued, execution_key=execution.execution_key)
            await db.commit()
        async with guest_app.state.sessionmaker() as db:
            task = await db.get(Task, out_id)
            with pytest.raises(AppError):
                await kb_index_service.build_index_job(
                    db, task=task, execution=execution, cipher=guest_app.state.cipher,
                    factory=guest_app.state.provider_factory, ai_timeout=5, allow_private=True,
                )
        assert not fake_llm.embed_calls


async def test_admission_commits_before_revoke_then_engine_claim_releases_document(
    postgres_collaboration_apps, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project_id, guest_id, base, member, doc_id = await _setup(owner_app, owner, guest)
        out = await _admit(guest_app, guest_id, project_id, doc_id)
        revoked = await owner.delete(f"{base}/collaborators/{guest_id}", params={
            "expected_revision": member["revision"],
        })
        assert revoked.status_code == 204, revoked.text
        await guest_app.state.task_engine._claim_and_run(out.id, out.task_key)
        async with owner_app.state.sessionmaker() as db:
            row = await db.get(Task, out.id)
            assert row.status == "failed" and row.error == "协作授权已失效，任务已停止"
            doc = await db.get(KbDocument, doc_id)
            assert doc.index_task_key is None and doc.index_claim_key is None
        assert not fake_llm.embed_calls
