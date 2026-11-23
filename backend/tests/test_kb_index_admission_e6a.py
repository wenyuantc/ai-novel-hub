"""E6-a：kb.index 协作受理与任务身份（不开放协作者 HTTP 入口）。

- 作者任务：受理、执行、结果与关键词降级不变，输入不带协作保留键。
- 内部协作受理：Task 归真实 actor，服务端写入原邀请轮次与作者向量选择，不使用协作者本人的向量配置。
- build：授权轮次/作者模型授权/令牌/作者向量配置任一变化即失败关闭，且零向量请求。
- 任务详情不泄露作者配置 ID 或内部绑定。
- E6-b 起：受控发送/每批写已接入，有效的协作者任务按冻结的作者向量正常执行（引擎可领取）。
"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.ai.providers import create_provider
from app.core.errors import AppError
from app.models import AIConfig, KbDocument, ProjectCollaborator, Task, User
from app.services import kb_index_service, project_ai_config, task_service
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.execution_identity import from_queued_task
from tests.helpers import create_ai_config, create_project, register, run_task, task_params, wait_task

SECT = "# 门派\n\n天剑宗位于北境雪山之巅。\n".encode()


async def _world(app, owner, editor, *, allow_owner_ai=True, owner_embedding=True, role="editor"):
    await app.state.task_engine.stop()
    author = await register(owner, "e6a-owner")
    actor = await register(editor, "e6a-editor")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    invited = await owner.post(f"{base}/collaborators", json={
        "username": "e6a-editor", "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    owner_cfg = None
    if owner_embedding:
        owner_cfg = await create_ai_config(owner, name="作者向量", model_type="embedding", model="owner-emb")
    # 协作者本人也有默认向量配置：共享索引绝不能采用它。
    editor_cfg = await create_ai_config(editor, name="个人向量", model_type="embedding", model="editor-emb")
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="资料.txt", sha256="a" * 64,
                         text="天剑宗位于北境雪山之巅。", status="ready_keyword_only")
        db.add(doc)
        await db.flush()
        await kb_index_service.chunk_document(db, doc)
        await db.commit()
        doc_id = doc.id
    return {"author": author, "actor": actor, "project": project["id"], "base": base,
            "member": accepted.json(), "owner_cfg": owner_cfg, "editor_cfg": editor_cfg, "doc": doc_id}


async def _admit(app, user_id: int, params: dict):
    async with app.state.sessionmaker() as db:
        user = await db.get(User, user_id)
        db.info["authenticated_actor"] = AuthenticatedActor(user.id, user.token_version)
        return await task_service.create_task(db, app.state.task_engine, user_id, "kb.index", params)


def _params(world, mode):
    params = {"project_id": world["project"], "mode": mode}
    if mode == "document":
        params["document_id"] = world["doc"]
    return params


async def _claim(app, task_id: int):
    """模拟引擎领取：原 queued 行 → running，并轮换文档占用（同一事务）。"""
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        queued = await kb_index_service.queued_document_claim(db, task)
        execution = from_queued_task(task, str(uuid4()))
        await db.execute(update(Task).where(Task.id == task_id).values(
            status="running", execution_key=execution.execution_key,
        ))
        if queued is not None:
            await kb_index_service.activate_document_claim(db, queued, execution_key=execution.execution_key)
        await db.commit()
    return execution


async def _build(app, task_id: int, execution, factory=create_provider):
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        return await kb_index_service.build_index_job(
            db, task=task, execution=execution, cipher=app.state.cipher, factory=factory,
            ai_timeout=5, allow_private=True,
        )


async def test_owner_index_identity_and_behavior_unchanged(app, client, fake_llm):
    solo = await register(client, "e6a-solo")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/kb"
    uploaded = await client.post(f"{base}/documents", files={"file": ("设定.md", SECT)})
    assert uploaded.status_code == 202, uploaded.text
    task = await wait_task(client, uploaded.json()["task"])
    assert task["status"] == "succeeded"
    doc = (await client.get(f"{base}/documents")).json()[0]
    assert doc["status"] == "ready_keyword_only" and fake_llm.embed_calls == []
    await create_ai_config(client, name="作者向量", model_type="embedding", model="owner-emb")
    rebuilt, _ = await run_task(client, await client.post(f"{base}/rebuild"))
    assert rebuilt["status"] == "succeeded"
    assert (await client.get(f"{base}/documents")).json()[0]["status"] == "ready"
    assert fake_llm.embed_calls and all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(Task).where(Task.kind == "kb.index")))
        assert len(rows) == 2 and all(row.user_id == solo["id"] for row in rows)
        for row in rows:
            assert "_collaboration_grant" not in row.input and "_shared_embedding" not in row.input


@pytest.mark.parametrize("mode", ["document", "rebuild"])
async def test_collaborator_admission_records_actor_grant_and_owner_selection(
    app, client, make_client, fake_llm, mode,
):
    editor = make_client()
    world = await _world(app, client, editor)
    out = await _admit(app, world["actor"]["id"], _params(world, mode))
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, out.id)
        assert task.user_id == world["actor"]["id"] and task.status == "queued"
        member = world["member"]
        assert task.input["_collaboration_grant"] == {
            "invitation_id": member["invitation_id"], "revision": member["revision"],
        }
        selection = project_ai_config.deserialize_shared_embedding(
            task.input["_shared_embedding"], actor_id=world["actor"]["id"], project_id=world["project"],
            project_owner_id=world["author"]["id"], task_grant=task.input["_collaboration_grant"],
        )
        assert selection.mode == "vector" and selection.model.config_owner_id == world["author"]["id"]
        assert selection.model.config_id == world["owner_cfg"]["id"] != world["editor_cfg"]["id"]
        assert task.input["_task_actor_token_version"] == (await db.get(User, world["actor"]["id"])).token_version
        doc = await db.get(KbDocument, world["doc"])
        if mode == "document":
            assert doc.status == "pending" and doc.index_task_key == task.statistics_key
            assert task.resource_key == f"kbdoc:{world['doc']}"
        else:
            assert doc.status == "ready_keyword_only" and doc.index_task_key is None
            assert task.resource_key == f"kbrebuild:{world['project']}"
    # 任务详情：本人可读，但不泄露内部绑定或作者配置身份。
    detail = await editor.get(f"/api/v1/tasks/{out.id}", params=task_params(out.model_dump(mode="json")))
    assert detail.status_code == 200, detail.text
    for text in (detail.text, out.model_dump_json()):
        for secret in ("_shared_embedding", "_collaboration_grant", "config_id", "config_owner_id",
                       "fingerprint", "owner-emb", "作者向量"):
            assert secret not in text
    assert fake_llm.embed_calls == []


async def test_collaborator_admission_fails_closed_without_side_effects(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor, allow_owner_ai=False)
    actor = world["actor"]["id"]
    with pytest.raises(AppError) as denied:
        await _admit(app, actor, _params(world, "document"))
    assert denied.value.status_code == 403 and denied.value.extra["code"] == "owner_embedding_not_allowed"
    # E6-c：协作者记忆模式只能绑定写入时的原邀请轮次与本次条目；缺绑定参数一律拒绝（不补全书）。
    with pytest.raises(AppError) as memory:
        await _admit(app, actor, {"project_id": world["project"], "mode": "memory"})
    assert memory.value.status_code == 409 and memory.value.extra["code"] == "task_admission_changed"
    # 客户端不能注入保留键。
    with pytest.raises(AppError) as forged:
        await _admit(app, actor, {**_params(world, "rebuild"), "_shared_embedding": {"mode": "vector"}})
    assert forged.value.status_code == 409
    stranger = make_client()
    other = await register(stranger, "e6a-stranger")
    with pytest.raises(AppError) as hidden:
        await _admit(app, other["id"], _params(world, "rebuild"))
    assert hidden.value.status_code == 404
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready_keyword_only" and doc.index_task_key is None
    assert fake_llm.embed_calls == []


async def test_collaborator_admission_requires_owner_embedding_config(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor, owner_embedding=False)
    with pytest.raises(AppError) as missing:
        await _admit(app, world["actor"]["id"], _params(world, "document"))
    assert missing.value.status_code == 400 and "向量" in missing.value.detail
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        assert (await db.get(KbDocument, world["doc"])).status == "ready_keyword_only"
    assert fake_llm.embed_calls == []


async def test_viewer_cannot_be_admitted(app, client, make_client, fake_llm):
    editor = make_client()
    world = await _world(app, client, editor, role="viewer")
    with pytest.raises(AppError) as denied:
        await _admit(app, world["actor"]["id"], _params(world, "rebuild"))
    assert denied.value.status_code == 403
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0


@pytest.mark.parametrize("mode", ["document", "rebuild"])
async def test_collaborator_job_uses_frozen_owner_selection_and_runs_guarded(
    app, client, make_client, fake_llm, mode,
):
    from app.models import RetrievalChunk

    editor = make_client()
    world = await _world(app, client, editor)
    out = await _admit(app, world["actor"]["id"], _params(world, mode))
    execution = await _claim(app, out.id)
    job = await _build(app, out.id, execution, fake_llm.factory)
    member = world["member"]
    assert job.actor_id == world["actor"]["id"]
    assert job.grant_identity == (member["invitation_id"], member["revision"])
    assert job.selection.model.config_owner_id == world["author"]["id"]
    assert job.selection.model.config_id == world["owner_cfg"]["id"]
    # 不解析任何未受控 embedder，更不能是协作者本人的模型。
    assert job.embedder is None
    assert (job.document_claim is not None) == (mode == "document")
    # E6-b：受控发送接入后按冻结的作者向量执行，写作者旧键空间。
    events = [event async for event in kb_index_service.run_index_job(job, app.state.sessionmaker)]
    assert events[-1][0] == "done", events
    assert fake_llm.embed_calls and all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)
    async with app.state.sessionmaker() as db:
        keys = set(await db.scalars(select(RetrievalChunk.embedding_key).where(
            RetrievalChunk.project_id == world["project"],
        )))
        assert keys and all(key.startswith("openai_compat:owner-emb:") for key in keys)
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready" and doc.index_task_key is None and doc.index_claim_key is None


@pytest.mark.parametrize("change", [
    "model_off", "model_off_db", "revoke", "regrant", "token", "owner_config", "downgrade",
])
async def test_collaborator_build_fails_closed_when_grant_changes(app, client, make_client, fake_llm, change):
    editor = make_client()
    world = await _world(app, client, editor)
    out = await _admit(app, world["actor"]["id"], _params(world, "document"))
    execution = await _claim(app, out.id)
    base, actor, member = world["base"], world["actor"]["id"], world["member"]
    if change == "model_off":
        response = await client.patch(f"{base}/collaborators/{actor}", json={
            "expected_revision": member["revision"], "allow_owner_ai": False,
        })
        assert response.status_code == 200, response.text
    elif change == "revoke":
        response = await client.delete(f"{base}/collaborators/{actor}", params={
            "expected_revision": member["revision"],
        })
        assert response.status_code == 204, response.text
    elif change == "regrant":
        off = await client.patch(f"{base}/collaborators/{actor}", json={
            "expected_revision": member["revision"], "allow_owner_ai": False,
        })
        assert off.status_code == 200, off.text
        on = await client.patch(f"{base}/collaborators/{actor}", json={
            "expected_revision": off.json()["revision"], "allow_owner_ai": True,
        })
        assert on.status_code == 200, on.text
    elif change == "downgrade":
        response = await client.patch(f"{base}/collaborators/{actor}", json={
            "expected_revision": member["revision"], "role": "viewer",
        })
        assert response.status_code == 200, response.text
    elif change == "model_off_db":
        # 不递增 revision 的直接改库（非支持路径）：原轮次仍匹配，作者模型开关本身也必须被核对。
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == actor,
            ).values(allow_owner_ai=False))
            await db.commit()
    elif change == "token":
        async with app.state.sessionmaker() as db:
            user = await db.get(User, actor)
            user.token_version += 1
            await db.commit()
    else:
        # 作者换了默认向量配置：不能按当前默认补签，原冻结选择失效。
        await create_ai_config(client, name="作者新向量", model_type="embedding", model="owner-emb-2",
                               is_default=True)
    with pytest.raises(AppError) as failed:
        await _build(app, out.id, execution)
    if change == "token":
        assert failed.value.status_code == 401
    elif change == "owner_config":
        assert failed.value.status_code == 400 and "向量模型已变化" in failed.value.detail
    else:
        # 撤权/降级/关授权（含不递增 revision 的直接改库）/重邀：原执行身份失效。
        assert failed.value.status_code == 409 and failed.value.extra["code"] == "task_execution_changed"
    assert fake_llm.embed_calls == []


async def test_non_owner_task_without_grant_is_rejected(app, client, make_client, fake_llm):
    """去掉 project.user_id == task.user_id 假设后，无授权轮次的非作者任务仍不能按作者执行。"""
    editor = make_client()
    world = await _world(app, client, editor)
    async with app.state.sessionmaker() as db:
        user = await db.get(User, world["actor"]["id"])
        task = Task(user_id=user.id, project_id=world["project"], kind="kb.index", status="queued",
                    serial_key=kb_index_service.serial_key(world["project"]),
                    input={"request": {"project_id": world["project"], "mode": "rebuild"},
                           "_task_actor_token_version": user.token_version})
        db.add(task)
        await db.commit()
        task_id = task.id
    execution = await _claim(app, task_id)
    with pytest.raises(AppError):
        await _build(app, task_id, execution)
    assert fake_llm.embed_calls == []


@pytest.mark.parametrize("valid", [True, False])
async def test_engine_claim_runs_valid_collaborator_index_and_closes_invalid(
    app, client, make_client, fake_llm, valid,
):
    """引擎：授权仍有效时领取并按作者向量执行（E6-b 起就绪）；关闭作者模型授权时受限收尾并释放文档占用。"""
    editor = make_client()
    world = await _world(app, client, editor)
    out = await _admit(app, world["actor"]["id"], _params(world, "document"))
    if not valid:
        response = await client.patch(f"{world['base']}/collaborators/{world['actor']['id']}", json={
            "expected_revision": world["member"]["revision"], "allow_owner_ai": False,
        })
        assert response.status_code == 200, response.text
    engine = app.state.task_engine
    # 调度协程已停止；只恢复领取开关，直接驱动一次领取（与既有引擎测试相同）。
    await engine.lease.register()
    engine._stopping = False
    await engine._claim_and_run(out.id, out.task_key)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, out.id)
        doc = await db.get(KbDocument, world["doc"])
        if valid:
            assert task.status == "succeeded" and task.user_id == world["actor"]["id"], task.error
            assert doc.status == "ready" and doc.index_task_key is None and doc.index_claim_key is None
        else:
            # 授权开关变更同时递增成员 revision：原轮次失效先于模型开关检查。
            assert task.status == "failed" and task.error in (
                "协作授权已失效，任务已停止", "作者模型授权已关闭，任务已停止",
            )
            assert doc.index_task_key is None and doc.index_claim_key is None
    if valid:
        assert fake_llm.embed_calls and all(call["model"] == "owner-emb" for call in fake_llm.embed_calls)
    else:
        assert fake_llm.embed_calls == []


async def test_owner_task_build_resolves_owner_config_not_task_shortcut(app, client, make_client, fake_llm):
    """作者任务按作品作者当前默认向量解析（原行为），并记录作者为执行者、无授权轮次。"""
    editor = make_client()
    world = await _world(app, client, editor)
    out = await _admit(app, world["author"]["id"], _params(world, "rebuild"))
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, out.id)
        assert "_collaboration_grant" not in task.input and "_shared_embedding" not in task.input
        assert (await db.scalar(select(AIConfig.model).where(AIConfig.id == world["owner_cfg"]["id"]))) == "owner-emb"
    execution = await _claim(app, out.id)
    job = await _build(app, out.id, execution)
    assert job.actor_id == world["author"]["id"] and job.grant_identity is None
    assert job.project_owner_id == world["author"]["id"]
    # E6-b：作者也走受控发送；构建期绑定作者当前默认向量（不写入 Task），不留未受控 embedder。
    assert job.embedder is None
    assert job.selection.model.config_id == world["owner_cfg"]["id"] and job.selection.model.source == "actor"


@pytest.mark.parametrize("tamper", ["actor_grant", "grant_only", "owner"])
async def test_run_index_job_rejects_tampered_identity(app, client, make_client, fake_llm, tamper):
    """作业身份与领取时冻结的执行身份不一致即失败关闭：零请求、零向量写入（取代 E6-a 的协作者一律关闭层）。"""
    from app.models import RetrievalChunk

    editor = make_client()
    world = await _world(app, client, editor)
    out = await _admit(app, world["author"]["id"], _params(world, "rebuild"))
    execution = await _claim(app, out.id)
    job = await _build(app, out.id, execution, fake_llm.factory)
    member = world["member"]
    if tamper in ("actor_grant", "grant_only"):
        job.grant_identity = (member["invitation_id"], member["revision"])
    if tamper == "actor_grant":
        job.actor_id = world["actor"]["id"]
    if tamper == "owner":
        job.project_owner_id = world["actor"]["id"]
    events = [event async for event in kb_index_service.run_index_job(job, app.state.sessionmaker)]
    assert [name for name, _ in events] == ["error"]
    assert fake_llm.embed_calls == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(RetrievalChunk.id)).where(
            RetrievalChunk.project_id == world["project"], RetrievalChunk.embedding.is_not(None),
        )) == 0
        assert (await db.get(KbDocument, world["doc"])).status == "ready_keyword_only"


@pytest.mark.parametrize("key", ["_collaboration_grant", "_shared_embedding"])
@pytest.mark.parametrize("who", ["author", "actor"])
async def test_client_reserved_keys_rejected_for_author_and_collaborator(app, client, make_client, fake_llm, key, who):
    """客户端参数中的保留键（含嵌套）一律在中央受理入口拒绝，不能借此进入协作分支或绕过作者校验。"""
    editor = make_client()
    world = await _world(app, client, editor)
    forged = {"invitation_id": world["member"]["invitation_id"], "revision": world["member"]["revision"]}
    for params in ({**_params(world, "document"), key: forged},
                   {**_params(world, "document"), "extra": {key: forged}}):
        with pytest.raises(AppError) as denied:
            await _admit(app, world[who]["id"], params)
        assert denied.value.status_code == 409 and denied.value.extra["code"] == "task_actor_unavailable"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        doc = await db.get(KbDocument, world["doc"])
        assert doc.status == "ready_keyword_only" and doc.index_task_key is None
    assert fake_llm.embed_calls == []
