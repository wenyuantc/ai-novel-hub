"""E6-c：授权编辑者的知识库 / 记忆向量入口（HTTP）。

- 已授权编辑者上传 / 重新索引 / 重建：任务归编辑者，向量只用作者配置（费用来源=作者），写作者旧键。
- 作者无向量配置：编辑者上传同步关键词并提示作者未配置（裁决 2），零任务零调用。
- 人工记忆：授权编辑者只向量化本次写入的条目（裁决 4）；未授权编辑者只维护关键词。
- 无授权编辑者：重新索引 / 重建 403 owner_embedding_not_allowed（裁决 10）；viewer 403；
  待接受 / 非成员 / 已撤权 / 仅管理员 404；全部零任务零调用。
- 受理后领取前撤权 / 关授权：领取失败关闭，零调用，文档占用释放。
- 协作者重试：同轮次可重试；关授权后拒绝；撤权重邀后旧任务不可重试。
"""

import pytest
from sqlalchemy import func, select, update

from app.models import KbChunk, KbDocument, RetrievalChunk, StoryMemory, Task, User
from app.services import kb_index_service, kb_service, retrieval_service
from tests.helpers import create_ai_config, create_project, register
from tests.test_collaboration_task_revocation_e4 import _wait_status

TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
OWNER_KEY = "openai_compat:owner-emb:64"
OLD = retrieval_service.encode_vector(retrieval_service.normalize_vector([1.0] + [0.0] * 7))


async def _invite(owner, guest, base, username, *, role="editor", allow_owner_ai=True, accept=True):
    invited = await owner.post(f"{base}/collaborators", json={
        "username": username, "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert invited.status_code == 201, invited.text
    if not accept:
        return invited.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept",
        json={"expected_revision": invited.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def _world(app, owner, editor, *, allow_owner_ai=True, owner_embedding=True, role="editor"):
    author = await register(owner, "e6c-owner")
    actor = await register(editor, "e6c-editor")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    member = await _invite(owner, editor, base, "e6c-editor", role=role, allow_owner_ai=allow_owner_ai)
    if owner_embedding:
        await create_ai_config(owner, name="作者向量", model_type="embedding", model="owner-emb")
    # 编辑者本人也有默认向量配置：共享索引绝不能采用它。
    await create_ai_config(editor, name="个人向量", model_type="embedding", model="editor-emb")
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="原有.md", sha256="a" * 64,
                         text="# 门派\n\n天剑宗位于北境雪山之巅。\n\n# 人物\n\n掌门姓萧。\n", status="ready")
        db.add(doc)
        await db.flush()
        await kb_index_service.chunk_document(db, doc)
        rows = list(await db.scalars(select(RetrievalChunk).where(RetrievalChunk.project_id == project["id"])))
        for row in rows:
            row.embedding, row.embedding_key = OLD, "openai_compat:other-emb:8"
        await db.commit()
        doc_id = doc.id
    return {"author": author, "actor": actor, "project": project["id"], "base": base, "member": member,
            "doc": doc_id}


async def _tasks(app) -> list[Task]:
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(Task).where(Task.kind == "kb.index").order_by(Task.id)))


def _models(fake_llm) -> set[str]:
    return {call["model"] for call in fake_llm.embed_calls}


async def _embedded_keys(app, project_id) -> list[str | None]:
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(RetrievalChunk.embedding_key).where(
            RetrievalChunk.project_id == project_id,
        ).order_by(RetrievalChunk.id)))


def _assert_owner_cost_source(task: Task, world) -> None:
    selection = task.input["_shared_embedding"]
    model = selection["model"]
    assert selection["mode"] == "vector" and model["source"] == "owner"
    assert model["config_owner_id"] == world["author"]["id"] and model["actor_id"] == world["actor"]["id"]


async def test_authorized_editor_upload_runs_owner_vectors_as_editor(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor)
    response = await editor.post(f"{world['base']}/kb/documents", files={
        "file": ("新资料.md", "# 剑法\n\n天剑十三式。\n".encode()),
    })
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["task"] is not None
    for secret in ("_shared_embedding", "_collaboration_grant", "owner-emb", "作者向量", "config_owner_id"):
        assert secret not in response.text
    row = await _wait_status(app, body["task"]["id"], TERMINAL)
    assert row.status == "succeeded", row.error
    # 并发额度归编辑者；外部费用来自作者配置（只调用作者模型）。
    assert row.user_id == world["actor"]["id"]
    _assert_owner_cost_source(row, world)
    assert _models(fake_llm) == {"owner-emb"}
    async with app.state.sessionmaker() as db:
        doc = await db.get(KbDocument, body["document"]["id"])
        assert doc.status == "ready" and doc.index_claim_key is None
        chunk_ids = list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == doc.id)))
        keys = list(await db.scalars(select(RetrievalChunk.embedding_key).where(
            RetrievalChunk.source == "kb", RetrievalChunk.ref_id.in_(chunk_ids),
        )))
        assert keys and set(keys) == {OWNER_KEY}
        # 原有文档的其他空间向量不被上传任务改写（只处理本文档）。
        old_doc_chunks = list(await db.scalars(select(KbChunk.id).where(KbChunk.document_id == world["doc"])))
        kept = list(await db.scalars(select(RetrievalChunk.embedding_key).where(
            RetrievalChunk.source == "kb", RetrievalChunk.ref_id.in_(old_doc_chunks),
        )))
        assert set(kept) == {"openai_compat:other-emb:8"}


async def test_authorized_editor_without_owner_embedding_uploads_keyword_only(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor, owner_embedding=False)
    response = await editor.post(f"{world['base']}/kb/documents", files={"file": ("新资料.txt", "天剑十三式".encode())})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["task"] is None and body["document"]["status"] == "ready_keyword_only"
    assert kb_service.KEYWORD_ONLY_OWNER_UNCONFIGURED in body["document"]["warnings"]
    assert await _tasks(app) == [] and fake_llm.embed_calls == []


@pytest.mark.parametrize("who", ["editor", "author"])
async def test_owner_embedding_unusable_upload_degrades_to_keyword(app, client, make_client, fake_llm, who):
    """作者配置存在但无法使用（密钥无法解密）：编辑者上传与作者本人一样降级为关键词，不阻断人工上传，
    零向量调用；编辑者的重新索引（显式向量操作）仍 400，不回退到其他配置。"""
    from app.models import AIConfig

    editor = make_client()
    world = await _world(app, client, editor)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(
            AIConfig.user_id == world["author"]["id"], AIConfig.model_type == "embedding",
        ).values(api_key_enc="not-a-valid-token"))
        await db.commit()
    uploader = editor if who == "editor" else client
    response = await uploader.post(f"{world['base']}/kb/documents", files={"file": ("新.txt", "天剑十三式".encode())})
    assert response.status_code == 202, response.text
    body = response.json()
    assert "not-a-valid-token" not in response.text
    if who == "editor":
        assert body["task"] is None and body["document"]["status"] == "ready_keyword_only"
        assert kb_service.KEYWORD_ONLY_OWNER_UNAVAILABLE in body["document"]["warnings"]
        assert await _tasks(app) == []
        rejected = await editor.post(f"{world['base']}/kb/documents/{world['doc']}/reindex")
        assert rejected.status_code == 400, rejected.text
        assert await _tasks(app) == []
    else:
        row = await _wait_status(app, body["task"]["id"], TERMINAL)
        assert row.status == "succeeded" and row.user_id == world["author"]["id"]
        async with app.state.sessionmaker() as db:
            assert (await db.get(KbDocument, body["document"]["id"])).status == "ready_keyword_only"
    assert fake_llm.embed_calls == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(KbDocument.id)).where(KbDocument.project_id == world["project"])) == 2


async def test_authorized_editor_reindex_replaces_document_and_rebuilds(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor)
    response = await editor.post(f"{world['base']}/kb/documents/{world['doc']}/reindex")
    assert response.status_code == 202, response.text
    row = await _wait_status(app, response.json()["id"], TERMINAL)
    assert row.status == "succeeded", row.error
    assert row.user_id == world["actor"]["id"]
    _assert_owner_cost_source(row, world)
    assert set(await _embedded_keys(app, world["project"])) == {OWNER_KEY}
    async with app.state.sessionmaker() as db:
        assert (await db.get(KbDocument, world["doc"])).status == "ready"
        # 全书重建（裁决 3）：把作者旧键之外的向量替换掉，任务同样归编辑者。
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.project_id == world["project"]).values(
            embedding=OLD, embedding_key="openai_compat:other-emb:8",
        ))
        await db.commit()
    fake_llm.embed_calls.clear()
    rebuilt = await editor.post(f"{world['base']}/kb/rebuild")
    assert rebuilt.status_code == 202, rebuilt.text
    row = await _wait_status(app, rebuilt.json()["id"], TERMINAL)
    assert row.status == "succeeded", row.error
    assert row.user_id == world["actor"]["id"]
    assert set(await _embedded_keys(app, world["project"])) == {OWNER_KEY}
    assert _models(fake_llm) == {"owner-emb"}


async def test_unauthorized_editor_vector_entries_forbidden_and_keyword_only(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=False)
    for path in (f"{world['base']}/kb/documents/{world['doc']}/reindex", f"{world['base']}/kb/rebuild"):
        rejected = await editor.post(path)
        assert rejected.status_code == 403, rejected.text
        assert rejected.json()["code"] == "owner_embedding_not_allowed"
    uploaded = await editor.post(f"{world['base']}/kb/documents", files={"file": ("新.txt", "新资料".encode())})
    assert uploaded.status_code == 202 and uploaded.json()["task"] is None
    assert kb_service.KEYWORD_ONLY_COLLABORATOR in uploaded.json()["document"]["warnings"]
    created = await editor.post(f"{world['base']}/memories", json={"chapter_no": 1, "content": "萧掌门闭关"})
    assert created.status_code == 201, created.text
    assert await _tasks(app) == [] and fake_llm.embed_calls == []
    async with app.state.sessionmaker() as db:
        # 失败文档的「仅关键词重分块」同样被拒：分块和旧向量都保持原状。
        assert (await db.get(KbDocument, world["doc"])).status == "ready"
    assert set(k for k in await _embedded_keys(app, world["project"]) if k) == {"openai_compat:other-emb:8"}


async def _other_access(app, client, make_client, world, kind):
    guest = make_client()
    user = await register(guest, f"e6c-{kind}")
    if kind == "viewer":
        await _invite(client, guest, world["base"], user["username"], role="viewer")
    elif kind == "pending":
        await _invite(client, guest, world["base"], user["username"], accept=False)
    elif kind == "revoked":
        member = await _invite(client, guest, world["base"], user["username"])
        response = await client.delete(f"{world['base']}/collaborators/{user['id']}", params={
            "expected_revision": member["revision"],
        })
        assert response.status_code == 204, response.text
    elif kind == "admin":
        async with app.state.sessionmaker() as db:
            (await db.get(User, user["id"])).is_admin = True
            await db.commit()
    return guest


@pytest.mark.parametrize(("kind", "status"), [
    ("viewer", 403), ("pending", 404), ("stranger", 404), ("revoked", 404), ("admin", 404),
])
async def test_non_editor_access_matrix_zero_tasks_zero_calls(app, client, make_client, fake_llm, kind, status):
    editor = make_client()
    world = await _world(app, client, editor)
    guest = await _other_access(app, client, make_client, world, kind)
    for method, path, kwargs in (
        ("post", f"{world['base']}/kb/documents", {"files": {"file": ("x.txt", "资料".encode())}}),
        ("post", f"{world['base']}/kb/documents/{world['doc']}/reindex", {}),
        ("post", f"{world['base']}/kb/rebuild", {}),
    ):
        response = await getattr(guest, method)(path, **kwargs)
        assert response.status_code == status, (path, response.text)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(KbDocument.id)).where(KbDocument.project_id == world["project"])) == 1
    assert await _tasks(app) == [] and fake_llm.embed_calls == []


async def test_authorized_editor_memory_vectorizes_only_written_entry(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor)
    # 作者早先的记忆仍缺向量：编辑者的写入不能替它花作者的钱（裁决 4）。
    owner_memory = await client.post(f"{world['base']}/memories", json={"chapter_no": 1, "content": "作者旧记忆"})
    assert owner_memory.status_code == 201
    for task in await _tasks(app):
        await _wait_status(app, task.id, TERMINAL)
    async with app.state.sessionmaker() as db:
        await db.execute(update(RetrievalChunk).where(
            RetrievalChunk.source == "memory", RetrievalChunk.ref_id == owner_memory.json()["id"],
        ).values(embedding=None, embedding_key=None))
        await db.commit()
    before = {task.id for task in await _tasks(app)}
    fake_llm.embed_calls.clear()
    created = await editor.post(f"{world['base']}/memories", json={"chapter_no": 2, "content": "萧掌门闭关修炼"})
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]
    new = [task for task in await _tasks(app) if task.id not in before]
    assert len(new) == 1
    row = await _wait_status(app, new[0].id, TERMINAL)
    assert row.status == "succeeded", row.error
    assert row.user_id == world["actor"]["id"]
    assert row.input[kb_index_service.MANUAL_MEMORY_KEY] == [memory_id]
    _assert_owner_cost_source(row, world)
    assert [call["texts"] for call in fake_llm.embed_calls] == [["萧掌门闭关修炼"]]
    assert _models(fake_llm) == {"owner-emb"}
    async with app.state.sessionmaker() as db:
        rows = {row.ref_id: row for row in await db.scalars(select(RetrievalChunk).where(
            RetrievalChunk.source == "memory",
        ))}
        assert rows[memory_id].embedding_key == OWNER_KEY
        assert rows[owner_memory.json()["id"]].embedding is None
    # 只改置顶（正文不变、向量仍有效）：不建新任务。
    count = len(await _tasks(app))
    pinned = await editor.patch(f"{world['base']}/memories/{memory_id}", json={"pinned": True})
    assert pinned.status_code == 200, pinned.text
    assert len(await _tasks(app)) == count
    # 修改正文：只为这一条重新建向量。
    fake_llm.embed_calls.clear()
    changed = await editor.patch(f"{world['base']}/memories/{memory_id}", json={"content": "萧掌门出关"})
    assert changed.status_code == 200, changed.text
    tasks = await _tasks(app)
    assert len(tasks) == count + 1
    row = await _wait_status(app, tasks[-1].id, TERMINAL)
    assert row.status == "succeeded", row.error
    assert [call["texts"] for call in fake_llm.embed_calls] == [["萧掌门出关"]]


async def test_editor_memory_without_owner_embedding_stays_keyword(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor, owner_embedding=False)
    created = await editor.post(f"{world['base']}/memories", json={"chapter_no": 1, "content": "萧掌门闭关"})
    assert created.status_code == 201, created.text
    assert await _tasks(app) == [] and fake_llm.embed_calls == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(StoryMemory.id))) == 1


@pytest.mark.parametrize("change", ["revoke", "model_off", "viewer"])
@pytest.mark.parametrize("entry", ["upload", "memory"])
async def test_admitted_then_revoked_before_claim_fails_closed(app, client, make_client, fake_llm, change, entry):
    editor = make_client()
    world = await _world(app, client, editor)
    await app.state.task_engine.stop()
    if entry == "upload":
        response = await editor.post(f"{world['base']}/kb/documents", files={"file": ("新.txt", "新资料".encode())})
        assert response.status_code == 202, response.text
        task_id, doc_id = response.json()["task"]["id"], response.json()["document"]["id"]
    else:
        response = await editor.post(f"{world['base']}/memories", json={"chapter_no": 1, "content": "萧掌门"})
        assert response.status_code == 201, response.text
        (task,) = await _tasks(app)
        task_id, doc_id = task.id, None
    member, actor = world["member"], world["actor"]["id"]
    if change == "revoke":
        result = await client.delete(f"{world['base']}/collaborators/{actor}", params={
            "expected_revision": member["revision"],
        })
        assert result.status_code == 204, result.text
    else:
        body = {"allow_owner_ai": False} if change == "model_off" else {"role": "viewer"}
        result = await client.patch(f"{world['base']}/collaborators/{actor}", json={
            "expected_revision": member["revision"], **body,
        })
        assert result.status_code == 200, result.text
    # 受理已提交、领取前授权收紧：重启引擎后的领取按数据库最新授权失败关闭。
    await app.state.task_engine.start()
    app.state.task_engine.notify()
    row = await _wait_status(app, task_id, TERMINAL)
    assert row.status == "failed", row.status
    assert fake_llm.embed_calls == []
    if doc_id is not None:
        async with app.state.sessionmaker() as db:
            doc = await db.get(KbDocument, doc_id)
            assert doc.index_task_key is None and doc.index_claim_key is None and doc.status == "failed"


async def _failed_editor_task(app, client, editor, world, fake_llm):
    """编辑者文档任务因授权以外的原因失败（取消），可作为重试源。"""
    await app.state.task_engine.stop()
    response = await editor.post(f"{world['base']}/kb/documents/{world['doc']}/reindex")
    assert response.status_code == 202, response.text
    task = response.json()
    cancelled = await editor.post(f"/api/v1/tasks/{task['id']}/cancel", params={"task_key": task["task_key"]})
    assert cancelled.status_code in (200, 202), cancelled.text
    row = await _wait_status(app, task["id"], TERMINAL)
    assert row.status == "cancelled"
    await app.state.task_engine.start()
    return task


async def test_collaborator_retry_same_round_runs_as_editor(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor)
    task = await _failed_editor_task(app, client, editor, world, fake_llm)
    retried = await editor.post(f"/api/v1/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
    assert retried.status_code == 202, retried.text
    row = await _wait_status(app, retried.json()["id"], TERMINAL)
    assert row.status == "succeeded", row.error
    assert row.user_id == world["actor"]["id"] and row.retry_of_id == task["id"]
    _assert_owner_cost_source(row, world)
    assert _models(fake_llm) == {"owner-emb"}
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, task["id"])
        assert row.input["_collaboration_grant"] == original.input["_collaboration_grant"]


@pytest.mark.parametrize("change", ["model_off", "reinvite", "model_off_db"])
async def test_collaborator_retry_rejected_after_grant_change(app, client, make_client, fake_llm, change):
    editor = make_client()
    world = await _world(app, client, editor)
    task = await _failed_editor_task(app, client, editor, world, fake_llm)
    actor, member = world["actor"]["id"], world["member"]
    if change == "model_off":
        result = await client.patch(f"{world['base']}/collaborators/{actor}", json={
            "expected_revision": member["revision"], "allow_owner_ai": False,
        })
        assert result.status_code == 200, result.text
    elif change == "model_off_db":
        # 不递增 revision 的直接改库：同轮次，但受理在锁内直接核对作者模型授权开关。
        from app.models import ProjectCollaborator

        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == actor,
            ).values(allow_owner_ai=False))
            await db.commit()
    else:
        result = await client.delete(f"{world['base']}/collaborators/{actor}", params={
            "expected_revision": member["revision"],
        })
        assert result.status_code == 204, result.text
        await _invite(client, editor, world["base"], "e6c-editor")
    count = len(await _tasks(app))
    retried = await editor.post(f"/api/v1/tasks/{task['id']}/retry", params={"task_key": task["task_key"]})
    # 关作者模型授权会递增成员 revision（新轮次），重邀产生新邀请：旧轮次任务对新轮次不可操作 → 404，
    # 不按新轮次补签、不建任务（与其他协作任务类型的重试规则一致）。
    if change == "model_off_db":
        assert retried.status_code == 403, retried.text
        assert retried.json()["code"] == "owner_embedding_not_allowed"
    else:
        assert retried.status_code == 404, retried.text
    assert len(await _tasks(app)) == count and fake_llm.embed_calls == []
    async with app.state.sessionmaker() as db:
        # 拒绝的重试不抢占文档、不改状态（原任务取消时已释放占用）。
        doc = await db.get(KbDocument, world["doc"])
        assert doc.index_task_key is None and doc.index_claim_key is None


@pytest.mark.parametrize("change", ["revoke", "model_off"])
async def test_editor_analysis_child_index_fails_closed_after_grant_change(
    app, client, make_client, fake_llm, change,
):
    """分析派生的编辑者子任务：收据带原邀请轮次；发送前撤权 / 关作者模型授权（直接改库，无本进程通知）
    → 真实发送租约内重验拒绝，零向量调用、零写入，子任务失败。"""
    from app.models import AnalysisIndexReceipt, ProjectCollaborator
    from tests.helpers import Gate, analysis_json
    from tests.test_collaboration_chapter_entry_e4f import _member, _terminal
    from tests.test_collaboration_chapter_entry_e4f import _world as chapter_world

    guest = make_client()
    world = await chapter_world(client, guest, allow_owner_ai=True, editor_model=False)
    await create_ai_config(client, name="作者向量", model_type="embedding", model="owner-embed")
    gate = Gate()
    fake_llm.embed_before_gates = [gate]
    fake_llm.push(analysis_json(summary="协作者分析"))
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert created.status_code == 202, created.text
    assert (await _terminal(app, created.json()["id"])).status == "succeeded"
    await gate.wait_reached(10)
    member = await _member(app, world)
    async with app.state.sessionmaker() as db:
        condition = (ProjectCollaborator.id == member.id,)
        values = {"status": "revoked", "revision": member.revision + 1} if change == "revoke" \
            else {"allow_owner_ai": False}
        await db.execute(update(ProjectCollaborator).where(*condition).values(**values))
        await db.commit()
    gate.release()
    async with app.state.sessionmaker() as db:
        child = await db.scalar(select(Task).where(Task.kind == "kb.index"))
        receipt = await db.scalar(select(AnalysisIndexReceipt))
    assert child.user_id == world.guest["id"] and receipt.grant_revision == member.revision
    child = await _wait_status(app, child.id, TERMINAL)
    assert child.status == "failed", child.status
    assert fake_llm.embed_calls == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(RetrievalChunk.id)).where(
            RetrievalChunk.source == "memory", RetrievalChunk.embedding.is_not(None),
        )) == 0


async def test_editor_memory_enqueue_binds_write_time_grant(app, client, make_client, fake_llm):
    """记忆派生只按写入时的原邀请轮次受理：轮次已变（即使新轮次仍授权）不补签，不建任务，零调用。"""
    from app.services.authorization_service import AuthenticatedActor

    editor = make_client()
    world = await _world(app, client, editor)
    await app.state.task_engine.stop()
    actor, member = world["actor"]["id"], world["member"]
    async with app.state.sessionmaker() as db:
        memory = StoryMemory(project_id=world["project"], chapter_no=1, kind="manual", origin="manual",
                             content="萧掌门闭关")
        db.add(memory)
        await db.flush()
        from app.services import memory_service

        await retrieval_service.upsert_project_sources(db, world["project"], "memory", [memory_service._chunk(memory)])
        await db.commit()
        memory_id = memory.id
        user = await db.get(User, actor)
        authenticated = AuthenticatedActor(user.id, user.token_version)
    old_grant = (member["invitation_id"], member["revision"])
    revision = member["revision"]
    for allow in (False, True):
        changed = await client.patch(f"{world['base']}/collaborators/{actor}", json={
            "expected_revision": revision, "allow_owner_ai": allow,
        })
        assert changed.status_code == 200, changed.text
        revision = changed.json()["revision"]
    queued = None
    try:
        queued = await kb_index_service.enqueue_collaborator_memory(
            app.state.task_engine, authenticated_actor=authenticated, project_id=world["project"],
            grant=old_grant, memory_ids=[memory_id],
        )
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 403, exc
    assert queued is None and await _tasks(app) == []
    # 同一条目按当前轮次受理才建任务（只绑定本次条目）。
    current = await kb_index_service.enqueue_collaborator_memory(
        app.state.task_engine, authenticated_actor=authenticated, project_id=world["project"],
        grant=(member["invitation_id"], revision), memory_ids=[memory_id],
    )
    (task,) = await _tasks(app)
    assert current == task.id and task.input[kb_index_service.MANUAL_MEMORY_KEY] == [memory_id]
    assert fake_llm.embed_calls == []


async def test_unauthorized_editor_analysis_derives_no_owner_vectors(app, client, make_client, fake_llm):
    """放宽 _verified_analysis_index_source 后的负对照：未获作者模型授权的编辑者用本人文本模型分析，
    即使作者与编辑者本人都有向量配置，也不派生 kb.index、不调用任何向量模型（收据记 no_work）。"""
    from app.models import AnalysisIndexReceipt, StoryMemory
    from tests.helpers import analysis_json
    from tests.test_collaboration_chapter_entry_e4f import _terminal
    from tests.test_collaboration_chapter_entry_e4f import _world as chapter_world

    guest = make_client()
    world = await chapter_world(client, guest, allow_owner_ai=False, editor_model=True)
    await create_ai_config(client, name="作者向量", model_type="embedding", model="owner-embed")
    await create_ai_config(guest, name="个人向量", model_type="embedding", model="editor-embed")
    fake_llm.push(analysis_json(summary="未授权编辑者的分析"))
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(StoryMemory.id)).where(
            StoryMemory.project_id == world.project["id"],
        )) > 0
        receipts = list(await db.scalars(select(AnalysisIndexReceipt)))
        assert all(item.outcome == "no_work" and item.child_task_key is None for item in receipts)
    assert await _tasks(app) == [] and fake_llm.embed_calls == []
