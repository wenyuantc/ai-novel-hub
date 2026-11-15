"""E6-b（= E4-h）：kb.index 文档/重建/作者旧记忆的受控发送与每批重验。

每批：短事务快照 → 受控 guard 在真实发送租约内重验后发送 → 新写事务锁内重验 + 快照 CAS 写入。
- 协作者任务经引擎正常执行，使用受理时冻结的作者向量，写作者旧键 provider:model:dim，任务归协作者。
- 每批之间（发送前）与发送后写入前 ×{撤权、降 viewer、关作者模型授权（HTTP / 直接改库）、作者换默认向量、
  令牌变化}×{本进程通知（HTTP 变更）、无通知（直接改库）}：已写批保留、后续批零发送、不写入，
  任务失败且文档占用释放为 failed。
- 并发修改资料：不把旧正文的向量写到新内容上；向量服务普通故障降级为关键词。
"""

import json

import pytest
from sqlalchemy import func, select, update

from app.models import KbChunk, KbDocument, ProjectCollaborator, RetrievalChunk, Task, User
from app.services import kb_index_service, retrieval_service
from tests.helpers import Gate, create_ai_config, create_project, fake_vector, register
from tests.test_collaboration_task_revocation_e4 import _wait_status
from tests.test_kb_index_admission_e6a import _admit, _params

SECTIONS = 40  # > EMBED_BATCH：至少两批
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


async def _world(app, owner, editor, *, allow_owner_ai=True, sections=SECTIONS, base_url=None):
    author = await register(owner, "e6b-owner")
    actor = await register(editor, "e6b-editor")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invited = await owner.post(f"{base}/collaborators", json={
        "username": "e6b-editor", "role": "editor", "allow_owner_ai": allow_owner_ai,
    })
    assert invited.status_code == 201, invited.text
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept",
        json={"expected_revision": invited.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    owner_cfg = await create_ai_config(owner, name="作者向量", model_type="embedding", model="owner-emb",
                                       **({"base_url": base_url} if base_url else {}))
    # 协作者本人也有默认向量配置：共享索引绝不能采用它。
    await create_ai_config(editor, name="个人向量", model_type="embedding", model="editor-emb")
    text = "".join(f"# 第{i}节\n\n第{i}节的资料正文，记录天剑宗第{i}代掌门的事迹。\n\n" for i in range(1, sections + 1))
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="资料.md", sha256="b" * 64, text=text,
                         status="ready_keyword_only")
        db.add(doc)
        await db.flush()
        assert await kb_index_service.chunk_document(db, doc) == sections
        await db.commit()
        doc_id = doc.id
    return {"author": author, "actor": actor, "project": project["id"], "base": base,
            "member": accepted.json(), "owner_cfg": owner_cfg, "doc": doc_id}


async def _embedded(app, project_id) -> list[str]:
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(RetrievalChunk.embedding_key).where(
            RetrievalChunk.project_id == project_id, RetrievalChunk.embedding.is_not(None),
        )))


async def _apply(app, owner, world, change, via):
    base, actor, member = world["base"], world["actor"]["id"], world["member"]
    if via == "http":
        if change == "revoke":
            response = await owner.delete(f"{base}/collaborators/{actor}", params={
                "expected_revision": member["revision"],
            })
            assert response.status_code == 204, response.text
        elif change == "viewer":
            response = await owner.patch(f"{base}/collaborators/{actor}", json={
                "expected_revision": member["revision"], "role": "viewer",
            })
            assert response.status_code == 200, response.text
        elif change == "model_off":
            response = await owner.patch(f"{base}/collaborators/{actor}", json={
                "expected_revision": member["revision"], "allow_owner_ai": False,
            })
            assert response.status_code == 200, response.text
        elif change == "owner_config":
            await create_ai_config(owner, name="作者新向量", model_type="embedding", model="owner-emb-2",
                                   is_default=True)
        else:
            raise AssertionError(change)
        return
    # 直接改库：不经过成员接口，不触发本进程通知（模拟跨实例变更）。
    async with app.state.sessionmaker() as db:
        condition = (ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == actor)
        if change == "revoke":
            await db.execute(update(ProjectCollaborator).where(*condition).values(
                status="revoked", revision=ProjectCollaborator.revision + 1,
            ))
        elif change == "viewer":
            await db.execute(update(ProjectCollaborator).where(*condition).values(
                role="viewer", revision=ProjectCollaborator.revision + 1,
            ))
        elif change == "model_off":
            # 不递增 revision 的授权收紧：须由作者模型授权开关的直接核对拒绝。
            await db.execute(update(ProjectCollaborator).where(*condition).values(allow_owner_ai=False))
        elif change in ("token", "owner_token"):
            target = actor if change == "token" else world["author"]["id"]
            await db.execute(update(User).where(User.id == target).values(token_version=User.token_version + 1))
        else:
            raise AssertionError(change)
        await db.commit()


async def test_collaborator_index_runs_with_owner_vector_and_actor_task(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor)
    out = await _admit(app, world["actor"]["id"], _params(world, "document"))
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    # 并发额度归真实触发者；外部费用来自作者配置（只调用作者模型，协作者个人模型零调用）。
    assert row.user_id == world["actor"]["id"]
    assert [len(call["texts"]) for call in fake_llm.embed_calls] == [32, SECTIONS - 32]
    assert all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)
    keys = await _embedded(app, world["project"])
    assert len(keys) == SECTIONS and set(keys) == {"openai_compat:owner-emb:64"}
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready" and doc.index_task_key is None and doc.index_claim_key is None


@pytest.mark.parametrize("timing", ["between", "after_send"])
@pytest.mark.parametrize(("change", "via"), [
    ("revoke", "http"), ("revoke", "db"), ("viewer", "http"), ("viewer", "db"),
    ("model_off", "http"), ("model_off", "db"), ("token", "db"), ("owner_config", "http"),
])
async def test_collaborator_batches_fail_closed_after_change(
    app, client, make_client, fake_llm, change, via, timing,
):
    editor = make_client()
    world = await _world(app, client, editor)
    gate = Gate()
    if timing == "between":
        # 第二批：已快照并通过 prepare 预检，但尚未进入真实发送门禁。
        fake_llm.embed_before_gates = [None, gate]
    else:
        # 第二批：已通过真实发送门禁并“到达供应商”，写入前变更。
        fake_llm.embed_after_gates = [None, gate]
    out = await _admit(app, world["actor"]["id"], _params(world, "document"))
    await gate.wait_reached(10)
    assert len(await _embedded(app, world["project"])) == 32
    await _apply(app, client, world, change, via)
    gate.release()
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "failed", (row.status, row.error)
    # 已写批保留；后续批零发送（between）或已发送但不写入（after_send）。
    assert len(fake_llm.embed_calls) == (1 if timing == "between" else 2)
    keys = await _embedded(app, world["project"])
    assert len(keys) == 32 and set(keys) == {"openai_compat:owner-emb:64"}
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "failed"
        assert doc.index_task_key is None and doc.index_task_queued_at is None and doc.index_claim_key is None
        assert (await db.get(Task, out.id)).execution_key is None
    assert all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)


@pytest.mark.parametrize("timing", ["between", "after_send"])
@pytest.mark.parametrize("change", ["owner_config", "owner_token"])
async def test_author_batches_fail_closed_after_change(app, client, make_client, fake_llm, change, timing):
    """裁决 6：作者索引同样经受控路径，配置/令牌变化后续批零发送、不写入。"""
    editor = make_client()
    world = await _world(app, client, editor)
    gate = Gate()
    if timing == "between":
        fake_llm.embed_before_gates = [None, gate]
    else:
        fake_llm.embed_after_gates = [None, gate]
    out = await _admit(app, world["author"]["id"], _params(world, "document"))
    await gate.wait_reached(10)
    await _apply(app, client, world, change, "http" if change == "owner_config" else "db")
    gate.release()
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "failed", (row.status, row.error)
    assert len(fake_llm.embed_calls) == (1 if timing == "between" else 2)
    assert len(await _embedded(app, world["project"])) == 32
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "failed" and doc.index_claim_key is None


@pytest.mark.parametrize("who", ["author", "actor"])
async def test_concurrent_source_edit_never_stores_stale_vector(app, client, make_client, fake_llm, who):
    """发送后写入前资料被修改：旧正文的向量不写入；重新快照后按新内容建向量。"""
    editor = make_client()
    world = await _world(app, client, editor)
    gate = Gate()
    fake_llm.embed_after_gates = [gate]
    out = await _admit(app, world[who]["id"], _params(world, "document"))
    await gate.wait_reached(10)
    sent = fake_llm.embed_calls[0]["texts"][0]
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.project_id == world["project"])
                              .order_by(RetrievalChunk.id).limit(1))
        chunk = await db.get(KbChunk, row.ref_id)
        assert sent == f"{chunk.heading_path}\n{chunk.content}"
        chunk.content = "第1节的资料已被修改为新内容。"
        row.text = f"{chunk.heading_path}\n{chunk.content}"
        row.version += 1
        await db.commit()
        row_id, new_text = row.id, row.text
    gate.release()
    task = await _wait_status(app, out.id, TERMINAL)
    assert task.status == "succeeded", task.error
    async with app.state.sessionmaker() as db:
        stored = await db.get(RetrievalChunk, row_id)
        expected = retrieval_service.normalize_vector(fake_vector(new_text, fake_llm.embed_dim))
        stale = retrieval_service.normalize_vector(fake_vector(sent, fake_llm.embed_dim))
        vector = retrieval_service.decode_vector(stored.embedding)
        assert (vector == expected).all() and not (vector == stale).all()
        assert await db.scalar(select(func.count(RetrievalChunk.id)).where(
            RetrievalChunk.project_id == world["project"], RetrievalChunk.embedding.is_(None),
        )) == 0
    assert any(new_text in call["texts"] for call in fake_llm.embed_calls[1:])


@pytest.mark.parametrize("who", ["author", "actor"])
@pytest.mark.parametrize("failing_call", [0, 1])
async def test_ordinary_vector_failure_degrades_to_keyword(app, client, make_client, fake_llm, who, failing_call):
    """向量服务普通故障（裁决 6、11）：不重发不确定请求，已写批保留，文档降级为仅关键词，任务成功。"""
    from app.ai.providers import AIProviderError

    editor = make_client()
    world = await _world(app, client, editor)
    gate = Gate()
    fake_llm.embed_before_gates = [gate] if failing_call == 0 else [None, gate]
    out = await _admit(app, world[who]["id"], _params(world, "document"))
    await gate.wait_reached(10)
    fake_llm.embed_error = AIProviderError("模型调用失败：额度不足")
    gate.release()
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    assert len(fake_llm.embed_calls) == failing_call + 1
    assert len(await _embedded(app, world["project"])) == 32 * failing_call
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready_keyword_only" and doc.index_claim_key is None
        assert any("向量生成失败" in item for item in doc.warnings)


@pytest.mark.parametrize("who", ["author", "editor"])
async def test_document_mode_replaces_old_model_vectors_only_in_this_document(app, client, make_client, fake_llm, who):
    """裁决 12：文档模式把本文档内旧模型向量替换为作者当前模型向量（作者与授权编辑者均适用），
    其他文档与记忆的旧向量保持不变；替换完成后文档为 ready。"""
    editor = make_client()
    world = await _world(app, client, editor)
    original = retrieval_service.encode_vector(retrieval_service.normalize_vector([1.0] + [0.0] * 7))
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(RetrievalChunk).where(RetrievalChunk.project_id == world["project"])
                                     .order_by(RetrievalChunk.id).limit(3)))
        for row in rows:
            row.embedding, row.embedding_key = original, "openai_compat:other-emb:8"
        other = KbDocument(project_id=world["project"], filename="其他.md", sha256="c" * 64,
                           text="# 别处\n\n别处资料。\n", status="ready")
        db.add(other)
        await db.flush()
        assert await kb_index_service.chunk_document(db, other) == 1
        other_row = await db.scalar(select(RetrievalChunk).join(KbChunk, KbChunk.id == RetrievalChunk.ref_id).where(
            RetrievalChunk.source == "kb", KbChunk.document_id == other.id,
        ))
        other_row.embedding, other_row.embedding_key = original, "openai_compat:other-emb:8"
        # 本作品记忆的旧模型向量同样不在文档模式范围内（裁决 12 只限本文档）。
        from app.models import StoryMemory
        from app.services import memory_service

        memory = StoryMemory(project_id=world["project"], chapter_no=1, kind="manual", origin="manual",
                             content="别处记忆")
        db.add(memory)
        await db.flush()
        await retrieval_service.upsert_project_sources(db, world["project"], "memory", [memory_service._chunk(memory)])
        memory_row = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory.id,
        ))
        memory_row.embedding, memory_row.embedding_key = original, "openai_compat:other-emb:8"
        await db.commit()
        replaced_ids, other_id, memory_chunk_id = [row.id for row in rows], other_row.id, memory_row.id
    actor = world["author"]["id"] if who == "author" else world["actor"]["id"]
    out = await _admit(app, actor, _params(world, "document"))
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    assert row.user_id == actor
    async with app.state.sessionmaker() as db:
        for chunk_id in replaced_ids:
            replaced = await db.get(RetrievalChunk, chunk_id)
            assert replaced.embedding_key == "openai_compat:owner-emb:64" and bytes(replaced.embedding) != original
        for kept_id in (other_id, memory_chunk_id):
            kept = await db.get(RetrievalChunk, kept_id)
            assert kept.embedding_key == "openai_compat:other-emb:8" and bytes(kept.embedding) == original
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready" and doc.index_claim_key is None
        assert kb_index_service._STILL_STALE not in doc.warnings
        assert (await db.get(KbDocument, other.id)).status == "ready"
    # 只发送本文档的分块（含被替换的 3 块），全部使用作者模型。
    assert sum(len(call["texts"]) for call in fake_llm.embed_calls) == SECTIONS
    assert all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)
    sent = [text for call in fake_llm.embed_calls for text in call["texts"]]
    assert not any("别处资料" in text or "别处记忆" in text for text in sent)


async def test_document_status_guard_still_reports_missing_vectors(app, client, make_client, fake_llm, monkeypatch):
    """收尾复核仍保护真正缺失：替换后若本文档仍有分块缺当前模型向量，不标为 ready。"""
    editor = make_client()
    world = await _world(app, client, editor, sections=3)
    real = kb_index_service._embed_stale

    async def embed_then_clear(sessionmaker, job, **kwargs):
        async for event in real(sessionmaker, job, **kwargs):
            yield event
        # 全部批次写完之后、收尾之前，本文档某分块又失去当前模型向量（例如被并发清空）。
        async with app.state.sessionmaker() as db:
            first = await db.scalar(select(RetrievalChunk.id).where(RetrievalChunk.project_id == world["project"])
                                    .order_by(RetrievalChunk.id).limit(1))
            await db.execute(update(RetrievalChunk).where(RetrievalChunk.id == first).values(
                embedding=None, embedding_key=None,
            ))
            await db.commit()

    monkeypatch.setattr(kb_index_service, "_embed_stale", embed_then_clear)
    out = await _admit(app, world["actor"]["id"], _params(world, "document"))
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready_keyword_only" and kb_index_service._STILL_STALE in doc.warnings


async def test_collaborator_rebuild_replaces_with_owner_legacy_key(app, client, make_client, fake_llm):
    """授权编辑者重建（裁决 3）：按作者旧键替换其他空间并刷新文档状态，仍只用作者模型。"""
    editor = make_client()
    world = await _world(app, client, editor)
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.project_id == world["project"]).values(
            embedding=retrieval_service.encode_vector(retrieval_service.normalize_vector([1.0, 0.0])),
            embedding_key="openai_compat:other-emb:2",
        ))
        await db.commit()
    out = await _admit(app, world["actor"]["id"], _params(world, "rebuild"))
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    keys = await _embedded(app, world["project"])
    assert len(keys) == SECTIONS and set(keys) == {"openai_compat:owner-emb:64"}
    assert all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)
    async with app.state.sessionmaker() as db:
        assert (await db.get(KbDocument, world["doc"])).status == "ready"


@pytest.mark.parametrize("change", ["revoke", "model_off"])
async def test_rebuild_between_batches_stops_without_status_refresh(app, client, make_client, fake_llm, change):
    """重建：撤权后后续批零发送，文档状态刷新（写入）同样不执行。"""
    editor = make_client()
    world = await _world(app, client, editor)
    gate = Gate()
    fake_llm.embed_before_gates = [None, gate]
    out = await _admit(app, world["actor"]["id"], _params(world, "rebuild"))
    await gate.wait_reached(10)
    await _apply(app, client, world, change, "db")
    gate.release()
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "failed", row.error
    assert len(fake_llm.embed_calls) == 1 and len(await _embedded(app, world["project"])) == 32
    async with app.state.sessionmaker() as db:
        assert (await db.get(KbDocument, world["doc"])).status == "ready_keyword_only"


def _conflict_once(monkeypatch, *, times: int):
    """快照读取（allow_orphans=True，仅 _embed_stale 使用）前 times 次模拟读已提交下的来源不一致。"""
    original = retrieval_service.snapshot_embedding_inputs
    calls = {"n": 0}

    async def flaky(db, project_id, ids, *, allow_orphans=False):
        if allow_orphans and calls["n"] < times:
            calls["n"] += 1
            raise retrieval_service._invalid_retrieval_reference()
        return await original(db, project_id, ids, allow_orphans=allow_orphans)

    monkeypatch.setattr(retrieval_service, "snapshot_embedding_inputs", flaky)
    return calls


@pytest.mark.parametrize("who", ["author", "actor"])
async def test_transient_snapshot_conflict_resnapshots_instead_of_failing(
    app, client, make_client, fake_llm, monkeypatch, who,
):
    """PostgreSQL 读已提交下快照的两次读取可能看到不同提交：重新快照，不把作者索引误判为授权失效。"""
    editor = make_client()
    world = await _world(app, client, editor)
    calls = _conflict_once(monkeypatch, times=1)
    out = await _admit(app, world[who]["id"], _params(world, "document"))
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    assert calls["n"] == 1
    assert len(await _embedded(app, world["project"])) == SECTIONS
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready" and kb_index_service._SOURCE_CHANGED not in doc.warnings


async def test_persistent_snapshot_conflict_degrades_without_sending(app, client, make_client, fake_llm, monkeypatch):
    editor = make_client()
    world = await _world(app, client, editor)
    _conflict_once(monkeypatch, times=1000)
    out = await _admit(app, world["actor"]["id"], _params(world, "document"))
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    assert not fake_llm.embed_calls and not await _embedded(app, world["project"])
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready_keyword_only" and kb_index_service._SOURCE_CHANGED in doc.warnings


async def test_snapshot_conflict_with_revoked_grant_fails_closed(app, client, make_client, fake_llm, monkeypatch):
    """快照冲突的重试仍先重验授权：已撤权不能降级为“仅关键词成功”。"""
    editor = make_client()
    world = await _world(app, client, editor)
    gate = Gate()
    fake_llm.embed_before_gates = [gate]
    out = await _admit(app, world["actor"]["id"], _params(world, "document"))
    await gate.wait_reached(10)
    fake_llm.embed_error = AssertionError("撤权后不应再次发送")
    _conflict_once(monkeypatch, times=1000)
    await _apply(app, client, world, "revoke", "db")
    gate.release()
    row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "failed", (row.status, row.error)
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "failed" and doc.index_claim_key is None
    assert not await _embedded(app, world["project"])


async def test_index_alive_treats_busy_gate_as_retry_and_other_errors_as_policy(monkeypatch):
    from app.core.errors import ForbiddenError
    from app.services import ai_dispatch

    job = kb_index_service.IndexJob("rebuild", 1, None, None)

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def rollback(self):
            return None

    errors = iter([ai_dispatch.VisibilityBusyError(), ForbiddenError("x"), RuntimeError("boom")])

    async def failing(db, job):
        raise next(errors)

    monkeypatch.setattr(kb_index_service, "_require_index_write", failing)
    await kb_index_service._require_index_alive(_Maker(), job)
    for _ in range(2):
        with pytest.raises(retrieval_service.EmbeddingPolicyError):
            await kb_index_service._require_index_alive(_Maker(), job)


async def test_author_default_switch_away_and_back_never_writes_other_model(app, client, make_client, fake_llm):
    """作者在批次之间 A→B→A 切换默认向量：不得按 B 发送或写入；发送时 A 仍是同一实例的当前默认，继续完成。"""
    editor = make_client()
    world = await _world(app, client, editor)
    gate = Gate()
    fake_llm.embed_before_gates = [None, gate]
    out = await _admit(app, world["author"]["id"], _params(world, "document"))
    await gate.wait_reached(10)
    await create_ai_config(client, name="作者新向量", model_type="embedding", model="owner-emb-2", is_default=True)
    back = await client.post(f"/api/v1/ai-configs/{world['owner_cfg']['id']}/default")
    assert back.status_code == 200, back.text
    gate.release()
    row = await _wait_status(app, out.id, TERMINAL)
    assert all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)
    keys = await _embedded(app, world["project"])
    assert set(keys) <= {"openai_compat:owner-emb:64"}
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, world["doc"])
    # 发送前 A 已恢复为默认且实例未变：按 A 继续是合法的当前作者选择，B 从未被使用。
    assert row.status == "succeeded", row.error
    assert len(keys) == SECTIONS and doc.status == "ready"


async def test_bound_memory_ordinary_failure_degrades_with_warning(app, client):
    """_run_bound_memory 普通故障按 _policy_cause 判定（修复前引用不存在的函数，被记为内部错误）。"""
    from tests.test_ai_visibility_dispatch import _server
    from tests.test_bound_memory_index_e6 import _owner_job

    payload = b'{"error":{"message":"quota exceeded","type":"invalid_request_error"}}'
    async with _server(status="400 Bad Request", mime="application/json", payload=payload) as (
        url, _, headers, body, requests,
    ):
        headers.set()
        body.set()
        _, project, memory_ids, _, job = await _owner_job(app, client, url)
        events = [event async for event in kb_index_service.run_index_job(job, app.state.sessionmaker)]
    assert len(requests) == 1
    name, data = events[-1]
    assert name == "done", events
    assert any("向量生成失败" in item for item in data["warnings"])
    async with app.state.sessionmaker() as db:
        row = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == memory_ids[0],
        ))
        assert row.embedding is None


async def test_author_document_index_over_real_http_transport(app, client, make_client):
    """真实 openai_compat SDK + 本机 HTTP：受控发送门禁、同批重复分块去重后逐行写入作者旧键。"""
    from app.ai.providers import create_provider
    from app.models import AIConfig
    from tests.test_ai_visibility_dispatch import _server

    payload = (b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]},'
               b'{"object":"embedding","index":1,"embedding":[0.0,1.0]}]}')
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        engine = app.state.task_engine
        engine.ctx.factory = create_provider
        engine.ctx.settings.allow_private_ai_endpoints = True
        editor = make_client()
        world = await _world(app, client, editor, sections=2)
        async with app.state.sessionmaker() as db:
            cfg = await db.get(AIConfig, world["owner_cfg"]["id"])
            cfg.base_url = f"{url}/v1"
            # 第三个分块与第一个正文相同：同批只发送两段文本，两行各自按快照写入同一向量。
            first = await db.scalar(select(KbChunk).where(KbChunk.document_id == world["doc"]).order_by(KbChunk.id))
            await db.commit()
            doc = await db.get(KbDocument, world["doc"])
            extra = KbChunk(document_id=doc.id, seq=99, heading_path=first.heading_path, content=first.content,
                            start_offset=0, end_offset=len(first.content))
            db.add(extra)
            await db.flush()
            await retrieval_service.upsert_project_sources(db, world["project"], "kb", [retrieval_service.ChunkInput(
                ref_id=extra.id, text=f"{first.heading_path}\n{first.content}", enabled=True,
            )])
            doc.chunk_count = 3
            await db.commit()
        out = await _admit(app, world["author"]["id"], _params(world, "document"))
        row = await _wait_status(app, out.id, TERMINAL)
    assert row.status == "succeeded", row.error
    assert len(requests) == 1 and len(json.loads(requests[0])["input"]) == 2
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(RetrievalChunk).where(RetrievalChunk.project_id == world["project"])
                                     .order_by(RetrievalChunk.id)))
        assert {r.embedding_key for r in rows} == {"openai_compat:owner-emb:2"}
        vectors = [retrieval_service.decode_vector(r.embedding).tolist() for r in rows]
        assert vectors[0] == vectors[2] == [1.0, 0.0] and vectors[1] == [0.0, 1.0]
        assert (await db.get(KbDocument, world["doc"])).status == "ready"
