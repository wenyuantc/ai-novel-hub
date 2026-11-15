"""E6-b：kb.index 每批写入与撤权在真实 PostgreSQL 跨连接的两种提交顺序（pg_blocking_pids 证明阻塞）。

撤权由另一应用实例（owner_app）提交，执行体所在实例（guest_app）收不到本进程通知，
只能依靠每批受控发送与写入屏障失败关闭。
"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import select, text

from app.core.errors import AppError
from app.models import KbDocument, RetrievalChunk, Task, User
from app.services import collaboration_service, kb_index_service, retrieval_service, task_service
from app.services.authorization_service import AuthenticatedActor
from tests.helpers import Gate, create_ai_config, create_project, register
from tests.test_collaboration_access_postgresql import _assert_project_row_locked, _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)

SECTIONS = 40


async def _setup(owner_app, owner, guest):
    await register(owner, "pg-e6b-owner")
    guest_user = await register(guest, "pg-e6b-guest")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invited = await owner.post(f"{base}/collaborators", json={
        "username": "pg-e6b-guest", "role": "editor", "allow_owner_ai": True,
    })
    assert invited.status_code == 201, invited.text
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept",
        json={"expected_revision": invited.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    await create_ai_config(owner, name="作者向量", model_type="embedding", model="owner-emb")
    await create_ai_config(guest, name="个人向量", model_type="embedding", model="guest-emb")
    body = "".join(f"# 第{i}节\n\n第{i}节的资料正文。\n\n" for i in range(1, SECTIONS + 1))
    async with owner_app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="资料.md", sha256="c" * 64, text=body,
                         status="ready_keyword_only")
        db.add(doc)
        await db.flush()
        assert await kb_index_service.chunk_document(db, doc) == SECTIONS
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


async def _embedded(app, project_id) -> int:
    async with app.state.sessionmaker() as db:
        return len(list(await db.scalars(select(RetrievalChunk.id).where(
            RetrievalChunk.project_id == project_id, RetrievalChunk.embedding.is_not(None),
        ))))


async def _assert_failed_and_released(app, task_id, doc_id):
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert row.status == "failed", (row.status, row.error)
        doc = await db.get(KbDocument, doc_id)
        assert doc.status == "failed" and doc.index_task_key is None and doc.index_claim_key is None


async def _cancel(*tasks):
    for item in tasks:
        if item is not None and not item.done():
            item.cancel()
            with suppress(asyncio.CancelledError, AppError):
                await item


async def test_batch_write_commits_before_revoke_then_next_batch_never_sends(
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
        original = retrieval_service.store_index_legacy_embedding_snapshots

        async def gated(db, *args, **kwargs):
            stored = await original(db, *args, **kwargs)
            if "pid" not in holder:
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return stored

        monkeypatch.setattr(retrieval_service, "store_index_legacy_embedding_snapshots", gated)
        out = await _admit(guest_app, guest_id, project_id, doc_id)
        run = asyncio.create_task(guest_app.state.task_engine._claim_and_run(out.id, out.task_key))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 10)
            # 写批事务持有作品父行锁直至提交：撤权必须等待。
            await _assert_project_row_locked(owner_app.state.sessionmaker, project_id)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
                "expected_revision": member["revision"],
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 10)
            assert len(fake_llm.embed_calls) == 1
            release.set()
            assert (await asyncio.wait_for(revoke, 10)).status_code == 204
            await asyncio.wait_for(run, 15)
        finally:
            release.set()
            await _cancel(run, revoke)
        # 先提交的合法批次保留；撤权后的下一批零发送、不写入。
        assert len(fake_llm.embed_calls) == 1
        assert await _embedded(owner_app, project_id) == 32
        await _assert_failed_and_released(owner_app, out.id, doc_id)


async def test_revoke_commits_before_batch_write_then_write_fails_closed(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project_id, guest_id, base, member, doc_id = await _setup(owner_app, owner, guest)
        sent = Gate()
        fake_llm.embed_after_gates = [sent]
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
        out = await _admit(guest_app, guest_id, project_id, doc_id)
        run = asyncio.create_task(guest_app.state.task_engine._claim_and_run(out.id, out.task_key))
        revoke = None
        try:
            # 第一批已越过真实发送门禁（响应头后）并停在返回前。
            await sent.wait_reached(10)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
                "expected_revision": member["revision"],
            }))
            await asyncio.wait_for(reached.wait(), 10)
            await _assert_project_row_locked(guest_app.state.sessionmaker, project_id)
            sent.release()
            # 写批的锁内重验被撤权事务阻塞。
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 10)
            release.set()
            assert (await asyncio.wait_for(revoke, 10)).status_code == 204
            await asyncio.wait_for(run, 15)
        finally:
            sent.release()
            release.set()
            await _cancel(run, revoke)
        assert len(fake_llm.embed_calls) == 1
        assert await _embedded(owner_app, project_id) == 0
        await _assert_failed_and_released(owner_app, out.id, doc_id)


async def test_postgresql_collaborator_index_positive_control(postgres_collaboration_apps, fake_llm):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project_id, guest_id, _base, _member, doc_id = await _setup(owner_app, owner, guest)
        out = await _admit(guest_app, guest_id, project_id, doc_id)
        await guest_app.state.task_engine._claim_and_run(out.id, out.task_key)
        async with owner_app.state.sessionmaker() as db:
            row = await db.get(Task, out.id)
            assert row.status == "succeeded" and row.user_id == guest_id, row.error
            keys = set(await db.scalars(select(RetrievalChunk.embedding_key).where(
                RetrievalChunk.project_id == project_id,
            )))
            assert keys == {"openai_compat:owner-emb:64"}
            assert (await db.get(KbDocument, doc_id)).status == "ready"
        assert [call["model"] for call in fake_llm.embed_calls] == ["owner-emb", "owner-emb"]
