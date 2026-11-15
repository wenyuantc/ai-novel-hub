"""E6-d PostgreSQL：同步检索（d1）与实体任务检索（d2）在双实例、跨连接撤权下的两种提交顺序。

- d1 撤权先提交（持锁）：授权编辑者的知识库检索在锁上阻塞（pg_blocking_pids），放行后被拒，零向量请求。
- d1 发送租约先持锁：真实发送前的租约内重验已通过、仍持锁时，另一实例的撤权被阻塞；放行后撤权提交，
  检索返回前按原轮次复核失败，不返回任何资料。
- d1 / d2 无通知直接改库关作者模型授权（另一实例）：发送租约内重验拦截，零向量请求。
"""

import asyncio
from contextlib import suppress

import httpx
from sqlalchemy import select, text, update

from app.models import KbChunk, KbDocument, ProjectCollaborator, RetrievalChunk
from app.services import collaboration_ai_guard, collaboration_service, retrieval_service
from tests.helpers import Gate, create_ai_config, fake_vector
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_kb_editor_entries_e6c_postgresql import _setup

pytest_plugins = ("tests.test_collaboration_access_postgresql",)

OWNER_SPACE = "openai_compat:owner-emb:64"
QUERY = "天剑宗位于何处"


def _clients(owner_app, guest_app):
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver"),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver"),
    )


async def _author_document(
    app, project_id: int, *, key: str = OWNER_SPACE, content: str = "天剑宗位于北境雪山之巅",
) -> None:
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project_id, filename="原有.txt", sha256="d" * 64, text=content,
                         status="ready", chunk_count=1)
        db.add(doc)
        await db.flush()
        chunk = KbChunk(document_id=doc.id, seq=1, content=content)
        db.add(chunk)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, project_id, "kb", [
            retrieval_service.ChunkInput(ref_id=chunk.id, text=chunk.content),
        ])
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.project_id == project_id).values(
            embedding=retrieval_service.encode_vector(retrieval_service.normalize_vector(fake_vector(content))),
            embedding_key=key,
        ))
        await db.commit()
    retrieval_service.invalidate(project_id)


async def _cancel(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


async def test_revoke_commits_before_authorized_editor_search(postgres_collaboration_apps, monkeypatch, fake_llm):
    owner_app, guest_app = postgres_collaboration_apps
    owner_client, guest_client = _clients(owner_app, guest_app)
    async with owner_client as owner, guest_client as guest:
        project_id, guest_id, base, member = await _setup(owner, guest)
        await _author_document(owner_app, project_id)
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
        search = None
        try:
            await asyncio.wait_for(reached.wait(), 8)
            search = asyncio.create_task(guest.post(f"{base}/kb/search", json={"query": QUERY, "min_score": 0}))
            await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder["pid"]), 8)
            assert not search.done()
            release.set()
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
            response = await asyncio.wait_for(search, 8)
            assert response.status_code in (403, 404), response.text
            assert "天剑宗位于北境" not in response.text
            assert fake_llm.embed_calls == []
        finally:
            release.set()
            await _cancel(revoke, search)


async def test_send_lease_holds_revoke_then_result_rechecked(postgres_collaboration_apps, monkeypatch, fake_llm):
    owner_app, guest_app = postgres_collaboration_apps
    owner_client, guest_client = _clients(owner_app, guest_app)
    async with owner_client as owner, guest_client as guest:
        project_id, guest_id, base, member = await _setup(owner, guest)
        await _author_document(owner_app, project_id)
        verified, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = collaboration_ai_guard.SendGuard.verify

        async def gated(self, db):
            await original(self, db)
            if not verified.is_set():
                # 真实发送租约内的重验已通过、锁仍持有：另一实例的撤权必须等待。
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                verified.set()
                await release.wait()

        monkeypatch.setattr(collaboration_ai_guard.SendGuard, "verify", gated)
        search = asyncio.create_task(guest.post(f"{base}/kb/search", json={"query": QUERY, "min_score": 0}))
        revoke = None
        try:
            await asyncio.wait_for(verified.wait(), 8)
            revoke = asyncio.create_task(owner.delete(f"{base}/collaborators/{guest_id}", params={
                "expected_revision": member["revision"],
            }))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 8)
            assert not revoke.done()
            release.set()
            assert (await asyncio.wait_for(revoke, 8)).status_code == 204
            response = await asyncio.wait_for(search, 8)
            # 查询向量在撤权提交前已合法发出（只用作者模型）；之后结果预检（409 permission_denied）
            # 或返回前按原轮次复核（403/404）失败关闭，不返回资料。
            assert [call["model"] for call in fake_llm.embed_calls] == ["owner-emb"]
            assert response.status_code in (403, 404, 409), response.text
            assert "天剑宗位于北境" not in response.text
        finally:
            release.set()
            await _cancel(search, revoke)


async def test_direct_owner_ai_off_on_other_instance_blocks_search_send(
    postgres_collaboration_apps, monkeypatch, fake_llm,
):
    owner_app, guest_app = postgres_collaboration_apps
    owner_client, guest_client = _clients(owner_app, guest_app)
    async with owner_client as owner, guest_client as guest:
        project_id, guest_id, base, _ = await _setup(owner, guest)
        await _author_document(owner_app, project_id)
        original = collaboration_ai_guard.SendGuard.prepare_provider

        async def switch_off_after_prepare(guard, *args, **kwargs):
            bound = await original(guard, *args, **kwargs)
            async with owner_app.state.sessionmaker() as db:
                await db.execute(update(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == guest_id,
                ).values(allow_owner_ai=False))
                await db.commit()
            return bound

        monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", switch_off_after_prepare)
        response = await guest.post(f"{base}/kb/search", json={"query": QUERY, "min_score": 0})
        assert response.status_code == 409, response.text
        assert "天剑宗位于北境" not in response.text
        assert fake_llm.embed_calls == []
        # 授权已关闭：之后的检索在锁内按关键词处理，不再构造向量 provider，仍零请求。
        response = await guest.post(f"{base}/kb/search", json={"query": QUERY, "min_score": 0})
        assert response.status_code == 200 and response.json()["mode"] == "keyword", response.text
        assert fake_llm.embed_calls == []


async def test_entity_task_author_vector_and_direct_owner_ai_off(postgres_collaboration_apps, monkeypatch, fake_llm):
    """d2：实体任务在第二个实例执行；授权时只调用作者向量模型；另一实例无通知直接改库关授权后，
    下一次查询向量在发送租约内被拦截，零请求、零文本调用、无结果。"""
    from tests.test_collaboration_entity_entry_e4g3b import _answer, _start, _terminal, _world

    first_app, second_app = postgres_collaboration_apps
    for app in (first_app, second_app):
        app.state.settings.allow_private_ai_endpoints = True
        app.state.settings.auto_analyze_enabled = False
    owner_client, guest_client = _clients(first_app, first_app)
    try:
        async with owner_client as owner, guest_client as guest:
            world = await _world(owner, guest, allow_owner_ai=True)
            await create_ai_config(owner, name="作者向量", model="owner-embed", model_type="embedding")
            await create_ai_config(guest, name="本人向量", model="editor-embed", model_type="embedding")
            await _author_document(first_app, world.project["id"], key="openai_compat:owner-embed:64",
                                   content="北斗剑阁天剑宗师承")
            fake_llm.push(_answer("character.generate"))
            created = await _start(guest, world, "character.generate", instruction="天剑宗师承")
            assert created.status_code == 202, created.text
            await second_app.state.task_engine.start()
            row = await _terminal(first_app, created.json()["id"])
            assert row.status == "succeeded", row.error
            assert [call["model"] for call in fake_llm.embed_calls] == ["owner-embed"]
            assert "北斗剑阁" in str(fake_llm.calls[-1]["messages"])

            fake_llm.embed_calls.clear()
            calls_before = len(fake_llm.calls)
            gate = Gate()
            fake_llm.embed_before_gates = [gate]
            fake_llm.push(_answer("character.generate"))
            created = await _start(guest, world, "character.generate", instruction="天剑宗师承")
            assert created.status_code == 202, created.text
            await gate.wait_reached(15)
            async with first_app.state.sessionmaker() as db:
                await db.execute(update(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == world.project["id"],
                    ProjectCollaborator.user_id == world.guest["id"],
                ).values(allow_owner_ai=False))
                await db.commit()
            gate.release()
            row = await _terminal(first_app, created.json()["id"])
            assert row.status == "failed" and row.result is None, row.status
            assert fake_llm.embed_calls == [] and len(fake_llm.calls) == calls_before
            async with first_app.state.sessionmaker() as db:
                assert (await db.scalar(select(ProjectCollaborator.allow_owner_ai).where(
                    ProjectCollaborator.project_id == world.project["id"],
                    ProjectCollaborator.user_id == world.guest["id"],
                ))) is False
    finally:
        await second_app.state.task_engine.stop()
