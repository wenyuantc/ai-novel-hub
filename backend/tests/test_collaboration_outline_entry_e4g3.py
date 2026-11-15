"""E4-g3a：accepted editor 的大纲 AI（outline.continue / outline.expand）。

HTTP 入口 POST /projects/{pid}/outlines/continue、/outlines/{oid}/expand。大纲任务只产出草稿：唯一持久化是 Task
自身的终态与 result（草稿由人工采纳接口另行写入，阶段 D 按 content.edit 开放），终态在同一事务内按原 grant 锁内
重验后写入，因此撤权后不存在“已提交、待证明”的业务副作用，也没有收据收尾。受理后领取前、领取后构建前、首次发送前、
发送中、展开的两批之间、完成事件发布前与终态写入前撤权/降级/重邀/停用/关闭作者模型授权都失败关闭：零后续发送、
result 为空、任务不停在 running、活动资源键释放。模型/费用来源、归属、私人风格与模板同 E4-f。
检索（E6-d 翻转，裁决 1/9）：作者已授权且有向量配置时，查询经 for_task 守卫用作者共享向量（读作者旧键
provider:model:dim）；未授权编辑者只用关键词、作者向量模型零调用。
协作者 SSE 自 E5-f 起经受控管线开放（见 test_collaboration_stream_*、test_task_sse_scope），
本文件经任务详情或数据库观察终态。
"""

import asyncio
import json

import pytest
from sqlalchemy import func, select, update

from app.ai.providers import AIProviderError
from app.models import AIConfig, Project, ProjectCollaborator, Task, User, WritingStyle
from app.services import outline_ai_service
from app.services.task_engine import engine as engine_module
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, create_ai_config, create_project, register

MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES
OWNER_KEY = "sk-owner-private-0001"
EDITOR_KEY = "sk-editor-own-0002"
OWNER_EMBED_KEY = "sk-owner-embed-0003"
STOPPED = (
    MESSAGES["collaboration_revoked"], MESSAGES["collaboration_model_revoked"],
    MESSAGES["collaboration_account_unavailable"], "任务输出授权已变化，已停止",
    "权限或模型授权已变化，已停止发送", "任务执行已停止",
)
CONTINUE_JSON = json.dumps({"outlines": [{"title": "续章甲", "summary": "启程"}]}, ensure_ascii=False)


def _expand_json(n, prefix="细纲"):
    return json.dumps({"chapters": [
        {"title": f"{prefix}{i}", "summary": f"梗概{i}", "key_events": [f"{prefix}事件{i}"]} for i in range(n)
    ]}, ensure_ascii=False)


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


class World:
    def __init__(self, project, outline, owner, guest, owner_config, editor_config, guest_client):
        self.project, self.outline, self.owner, self.guest = project, outline, owner, guest
        self.owner_config, self.editor_config, self.guest_client = owner_config, editor_config, guest_client

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.project['id']}"


async def _world(owner, guest, *, role="editor", allow_owner_ai=False, accept=True, editor_model=True):
    owner_user = await register(owner, "e4g3-owner")
    guest_user = await register(guest, "e4g3-guest")
    project = await create_project(owner)
    outline = await owner.post(f"/api/v1/projects/{project['id']}/outlines",
                               json={"title": "第一章纲", "summary": "主角离家"})
    assert outline.status_code == 201, outline.text
    owner_config = await create_ai_config(owner, name="作者模型", model="owner-model", api_key=OWNER_KEY)
    editor_config = (await create_ai_config(guest, name="本人模型", model="editor-model", api_key=EDITOR_KEY)
                     if editor_model else None)
    invite = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "e4g3-guest", "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert invite.status_code == 201, invite.text
    if accept:
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    return World(project, outline.json(), owner_user, guest_user, owner_config, editor_config, guest)


def _continue(client, world, **body):
    return client.post(f"{world.base}/outlines/continue", json={"count": 1, **body})


def _expand(client, world, *, count=2, **body):
    return client.post(f"{world.base}/outlines/{world.outline['id']}/expand", json={"count": count, **body})


def _start(client, world, kind, **body):
    return _continue(client, world, **body) if kind == "continue" else _expand(client, world, **body)


def _answer(kind):
    return CONTINUE_JSON if kind == "continue" else _expand_json(2)


async def _member(app, world):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ))


async def _change(app, owner, world, change):
    member = await _member(app, world)
    url = f"{world.base}/collaborators/{world.guest['id']}"
    if change == "revoked":
        response = await owner.delete(url, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
    elif change == "viewer":
        response = await owner.patch(url, json={"expected_revision": member.revision, "role": "viewer"})
        assert response.status_code == 200, response.text
    elif change == "owner_ai":
        response = await owner.patch(url, json={"expected_revision": member.revision, "allow_owner_ai": False})
        assert response.status_code == 200, response.text
    elif change == "owner_ai_direct":
        # 直接关闭模型授权、不递增 revision：只能靠各检查点直接核对 allow_owner_ai。
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.id == member.id).values(
                allow_owner_ai=False,
            ))
            await db.commit()
    elif change == "disabled":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(is_active=False))
            await db.commit()
    elif change == "actor_token":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(
                token_version=User.token_version + 1,
            ))
            await db.commit()
    elif change == "reinvite":
        response = await owner.delete(url, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
        invite = await owner.post(f"{world.base}/collaborators", json={
            "username": "e4g3-guest", "role": "editor", "allow_owner_ai": member.allow_owner_ai,
        })
        assert invite.status_code == 201, invite.text
        accepted = await world.guest_client.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    else:
        raise AssertionError(change)


def _elsewhere(app, owner, world, change, changes):
    """撤权在独立任务中执行（与真实服务器一致）：执行体被通知取消时不能打断撤权请求自身的收尾。"""
    async def run():
        work = asyncio.ensure_future(_change(app, owner, world, change))
        changes.append(work)
        await asyncio.shield(work)

    return run


async def _terminal(app, task_id, within=15.0):
    async with asyncio.timeout(within):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
                if row.status not in ("queued", "running") and row.execution_key is None:
                    return row
            await asyncio.sleep(0.02)


async def _task_count(app):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count(Task.id)))


async def _assert_released(app, client, world, kind, fake_llm):
    """活动资源键已释放：作者可立即对同一资源发起大纲 AI 并成功。"""
    fake_llm.push(_answer(kind))
    author = await _start(client, world, kind)
    assert author.status_code == 202, author.text
    row = await _terminal(app, author.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.owner["id"] and "_collaboration_grant" not in row.input


async def _index_owner_memory_in_author_space(app, world):
    """直接写入一条按作者旧键空间（provider:model:dim，与作者 / 授权编辑者索引任务产物同形，裁决 1）索引的记忆，
    不经任务、不调用模型。维度与 FakeLLM 一致，授权协作执行的查询向量可直接命中（hybrid）。"""
    from app.models import RetrievalChunk, StoryMemory
    from app.services import retrieval_service
    from tests.helpers import fake_vector

    async with app.state.sessionmaker() as db:
        config = await retrieval_service.default_embedding_config(db, world.owner["id"])
        content = "第一章纲 作者旧情节向量记忆 主角离家"
        vector = retrieval_service.normalize_vector(fake_vector(content))
        key = retrieval_service.make_embedding_key(retrieval_service.config_model_key(config), len(vector))
        # 第 0 章：位于续写起点与展开（第 1 章纲）之前，两种任务都会召回。
        memory = StoryMemory(project_id=world.project["id"], chapter_no=0, kind="manual", content=content)
        db.add(memory)
        await db.flush()
        await retrieval_service.upsert_many(db, world.project["id"], "memory", [
            retrieval_service.ChunkInput(ref_id=memory.id, text=memory.content, chapter_no=0),
        ])
        await db.execute(update(RetrievalChunk).where(
            RetrievalChunk.project_id == world.project["id"], RetrievalChunk.source == "memory",
            RetrievalChunk.ref_id == memory.id,
        ).values(embedding=retrieval_service.encode_vector(vector), embedding_key=key))
        await db.commit()
    retrieval_service.invalidate(world.project["id"])


# ---------------- 成功路径、费用来源与归属 ----------------


@pytest.mark.parametrize("kind", ["continue", "expand"])
async def test_editor_outline_uses_own_model_and_author_shared_vectors(app, client, make_client, fake_llm, kind):
    """E6-d 翻转（原 test_editor_outline_uses_own_model_and_keyword_retrieval 的零向量断言）：作者已授权且有向量
    配置时，协作者大纲检索用受理冻结的作者共享向量（费用来源=作者），读作者旧键空间；协作者本人向量配置不参与。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    await create_ai_config(client, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key=OWNER_EMBED_KEY)
    await create_ai_config(guest, name="本人向量", model="editor-embed", model_type="embedding")
    # 作者旧键空间中已有可召回的记忆向量（续写起点之前）。
    await _index_owner_memory_in_author_space(app, world)
    fake_llm.push(_answer(kind))
    created = await _start(guest, world, kind)
    assert created.status_code == 202, created.text
    task = created.json()
    detail = await guest.get(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
    assert detail.status_code == 200, detail.text
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"] and row.project_id == world.project["id"]
    assert row.input["_collaboration_grant"]["invitation_id"]
    # 本人有文本配置：即使作者授权开启也只用本人密钥。
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY]
    assert (row.input["_project_model"]["source"], row.input["_project_model"]["config_owner_id"]) == (
        "actor", world.guest["id"],
    )
    selection = row.input["_shared_embedding"]
    assert selection["mode"] == "vector" and selection["model"]["source"] == "owner"
    assert selection["model"]["config_owner_id"] == world.owner["id"]
    # 查询向量只调用作者向量模型；记忆在作者旧键空间命中（作者向量费用来源）。
    assert fake_llm.embed_calls and {call["model"] for call in fake_llm.embed_calls} == {"owner-embed"}
    assert "作者旧情节向量记忆" in str(fake_llm.calls[0]["messages"])
    if kind == "continue":
        assert [d["title"] for d in row.result["drafts"]] == ["续章甲"]
    else:
        assert row.result["outline_id"] == world.outline["id"] and len(row.result["drafts"]) == 2
    # 只产出草稿：任务不写大纲/细纲，也不派生其他任务（含向量索引任务）。
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.id != row.id)) == 0
    outlines = (await client.get(f"{world.base}/outlines")).json()
    assert [o["title"] for o in outlines] == ["第一章纲"]
    # 已完成的草稿不因随后撤权回滚（Task 仍是本人历史）。
    await _change(app, client, world, "revoked")
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, row.id)).status == "succeeded"


@pytest.mark.parametrize("kind", ["continue", "expand"])
async def test_unauthorized_editor_outline_retrieval_is_keyword_only(app, client, make_client, fake_llm, kind):
    """未授权编辑者：作者向量空间有可召回向量、本人也有向量配置，仍只走关键词，任何向量模型零调用。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=False)
    await create_ai_config(client, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key=OWNER_EMBED_KEY)
    await create_ai_config(guest, name="本人向量", model="editor-embed", model_type="embedding")
    await _index_owner_memory_in_author_space(app, world)
    fake_llm.push(_answer(kind))
    created = await _start(guest, world, kind)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.input["_shared_embedding"]["mode"] == "keyword"
    assert fake_llm.embed_calls == []
    # 关键词召回仍命中作者记忆（不是靠空索引让向量零调用）。
    assert "作者旧情节向量记忆" in str(fake_llm.calls[0]["messages"])


@pytest.mark.parametrize("change", ["owner_ai_direct", "revoked", "viewer"])
async def test_editor_outline_vector_query_fails_closed_before_send(app, client, make_client, fake_llm, change):
    """查询向量已过 prepare_provider、尚未进入真实发送门禁时撤权 / 降级 / 直接改库关授权：零向量发送、
    零文本调用，任务失败关闭（无通知的直接改库由发送租约内重验拦截）。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    await create_ai_config(client, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key=OWNER_EMBED_KEY)
    await _index_owner_memory_in_author_space(app, world)
    gate = Gate()
    fake_llm.embed_before_gates = [gate]
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    changes: list = []
    await _elsewhere(app, client, world, change, changes)()
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status in ("failed", "cancelled"), row.status
    assert row.result is None
    assert fake_llm.embed_calls == [] and fake_llm.calls == []


async def test_editor_outline_owner_model_fallback_records_cost_source(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"]
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY]
    model = row.input["_project_model"]
    assert (model["source"], model["config_owner_id"], model["config_id"]) == (
        "owner", world.owner["id"], world.owner_config["id"],
    )
    detail = await guest.get(f"/api/v1/tasks/{row.id}", params={"task_key": row.statistics_key})
    # 任务详情不泄露作者配置身份与私有绑定。
    assert detail.status_code == 200 and detail.json().get("ai_config_id") is None
    assert "_project_model" not in detail.text and OWNER_KEY not in detail.text


@pytest.mark.parametrize("case", ["no_model_no_grant", "broken_own_model", "explicit_owner_config"])
async def test_editor_outline_model_errors_never_fall_back(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=case != "no_model_no_grant",
                         editor_model=case != "no_model_no_grant")
    body = {}
    if case == "broken_own_model":
        # 本人配置损坏：即使作者授权开启也不回退作者配置。
        async with app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == world.editor_config["id"]).values(
                api_key_enc="not-a-valid-token",
            ))
            await db.commit()
    elif case == "explicit_owner_config":
        body = {"ai_config_id": world.owner_config["id"]}
    for kind in ("continue", "expand"):
        response = await _start(guest, world, kind, **body)
        assert response.status_code in ((400, 404) if case == "explicit_owner_config" else (400,)), response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []


async def test_editor_outline_hides_owner_private_style_and_template(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    detail = (await client.get("/api/v1/prompt-templates/outline.continue")).json()
    saved = await client.put("/api/v1/prompt-templates/outline.continue", json={
        "system": detail["default_system"] + "\n作者私有模板标记壬癸", "user": detail["default_user"],
    })
    assert saved.status_code == 200, saved.text
    async with app.state.sessionmaker() as db:
        style = WritingStyle(user_id=world.owner["id"], name="作者私有风格", description="私有描述甲乙丙",
                             extra="作者私有要求丁戊己")
        db.add(style)
        await db.flush()
        await db.execute(update(Project).where(Project.id == world.project["id"]).values(default_style_id=style.id))
        await db.commit()
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    sent = str(fake_llm.calls[0]["messages"])
    assert fake_llm.calls[0]["api_key"] == EDITOR_KEY
    for secret in ("作者私有模板标记壬癸", "私有描述甲乙丙", "作者私有风格"):
        assert secret not in sent
    # 作者自己的续写仍使用作者模板与风格（正对照）。
    fake_llm.push(CONTINUE_JSON)
    mine = await _continue(client, world)
    assert mine.status_code == 202, mine.text
    assert (await _terminal(app, mine.json()["id"])).status == "succeeded"
    own = str(fake_llm.calls[1]["messages"])
    assert "作者私有模板标记壬癸" in own and "私有描述甲乙丙" in own


@pytest.mark.parametrize("case", ["viewer", "pending", "outsider", "revoked", "admin_outsider", "admin_viewer"])
async def test_non_editors_cannot_start_outline_ai(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(
        client, guest, role="viewer" if case in ("viewer", "admin_viewer") else "editor",
        accept=case not in ("pending", "outsider", "admin_outsider"), allow_owner_ai=True,
    )
    if case in ("outsider", "admin_outsider", "revoked"):
        member = await _member(app, world)
        response = await client.delete(f"{world.base}/collaborators/{world.guest['id']}",
                                       params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
    if case.startswith("admin"):
        # 管理员人工读取/编辑权限不能旁路协作 AI。
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(is_admin=True))
            await db.commit()
    expected = 403 if case in ("viewer", "admin_viewer") else 404
    for kind in ("continue", "expand"):
        response = await _start(guest, world, kind)
        assert response.status_code == expected, response.text
        assert "第一章纲" not in response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []


async def test_editor_cannot_expand_outline_of_another_book(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    other = await create_project(client, title="外部作品")
    secret = await client.post(f"/api/v1/projects/{other['id']}/outlines", json={"title": "外书秘密章纲"})
    assert secret.status_code == 201, secret.text
    response = await guest.post(f"{world.base}/outlines/{secret.json()['id']}/expand", json={"count": 2})
    assert response.status_code == 404 and "外书秘密" not in response.text, response.text
    assert await _task_count(app) == 0 and fake_llm.calls == []


# ---------------- 受理后领取前、构建前、发送前 ----------------


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "owner_ai_direct", "disabled", "reinvite"])
async def test_change_after_admission_before_claim_fails_closed(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    owner_model = change.startswith("owner_ai")
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_model)
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    await _change(app, client, world, change)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    expected = {"disabled": "collaboration_account_unavailable", "owner_ai_direct": "collaboration_model_revoked"}
    assert row.status == "failed" and row.error == MESSAGES[expected.get(change, "collaboration_revoked")]
    assert row.result is None and fake_llm.calls == []
    await _assert_released(app, client, world, "continue", fake_llm)


@pytest.mark.parametrize("stage", ["build", "send"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_change_after_claim_before_first_send_never_sends(app, client, make_client, fake_llm, monkeypatch,
                                                                stage, change):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    handler = get_handler("outline.continue")
    original_build = type(handler).build_job
    original_job = outline_ai_service.build_outline_job
    fired, changes = asyncio.Event(), []
    revoke = _elsewhere(app, client, world, change, changes)

    async def before_build(self, ctx, task, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_build(self, ctx, task, **kwargs)

    async def after_job_build(*args, **kwargs):
        job = await original_job(*args, **kwargs)
        if not fired.is_set():
            fired.set()
            await revoke()
        return job

    if stage == "build":
        monkeypatch.setattr(type(handler), "build_job", before_build)
    else:
        monkeypatch.setattr(outline_ai_service, "build_outline_job", after_job_build)
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired.is_set()
    assert row.status == "failed" and row.output_text == "" and row.result is None, row.error
    assert fake_llm.calls == [] and fake_llm.embed_calls == []
    await _assert_released(app, client, world, "continue", fake_llm)


async def test_build_job_rejects_revoked_grant_without_notification(app, client, make_client, fake_llm, monkeypatch):
    """构建前的锁内重验本身即失败关闭（不依赖本进程通知取消执行体）。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)
    handler = get_handler("outline.continue")
    original_build = type(handler).build_job
    seen = []

    async def before_build(self, ctx, task, **kwargs):
        if not seen:
            seen.append(task.id)
            await _change(app, client, world, "revoked")
        return await original_build(self, ctx, task, **kwargs)

    monkeypatch.setattr(type(handler), "build_job", before_build)
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert seen and row.status == "failed" and row.result is None, row.error
    assert fake_llm.calls == []


# ---------------- 发送中、两批之间、完成发布与终态前 ----------------


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_change_during_send_discards_drafts(app, client, make_client, fake_llm, monkeypatch, change, notify):
    """notify=False 模拟跨实例/未收到通知：只能靠逐帧发布核验与终态锁内重验拒绝旧结果。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    gate = Gate()
    fake_llm.push(['{"outlines":[{"title":"旧授权', gate, '下的草稿","summary":"秘密"}]}'])
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _change(app, client, world, change)
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert "下的草稿" not in (row.output_text or "") and row.result is None
    assert len(fake_llm.calls) == 1
    await _assert_released(app, client, world, "continue", fake_llm)


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct", "disabled", "reinvite"])
async def test_change_between_expand_batches_never_sends_second_batch(app, client, make_client, fake_llm,
                                                                      monkeypatch, change, notify):
    """展开超过 5 章分批生成：第一批完成后撤权，第二批零发送、无结果。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    calls, changes = [], []
    revoke = _elsewhere(app, client, world, change, changes)
    # 第二批进入发送前停在钩子处，由测试在外部完成撤权后再放行。
    second, proceed = asyncio.Event(), asyncio.Event()
    original_stream = outline_ai_service._stream

    async def gated_stream(job, messages, pieces):
        calls.append(messages)
        if len(calls) == 2:
            second.set()
            await proceed.wait()
        async for event in original_stream(job, messages, pieces):
            yield event

    monkeypatch.setattr(outline_ai_service, "_stream", gated_stream)
    fake_llm.push(_expand_json(5, "第一批"))
    fake_llm.push(_expand_json(1, "第二批"))
    created = await _expand(guest, world, count=6)
    assert created.status_code == 202, created.text
    await asyncio.wait_for(second.wait(), 15)
    await revoke()
    await asyncio.gather(*changes)
    proceed.set()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.error in STOPPED and row.result is None, row.error
    assert len(fake_llm.calls) == 1
    assert "第二批" not in (row.output_text or "")
    await _assert_released(app, client, world, "expand", fake_llm)


@pytest.mark.parametrize("hook", ["done_event", "finalization"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct", "actor_token"])
async def test_change_before_publish_fails_closed_without_running_residue(app, client, make_client, fake_llm,
                                                                          monkeypatch, hook, change):
    """模型输出已完整、完成事件发布或终态写入前授权变化：大纲没有已提交的业务副作用，终态失败且无 result，
    不得停在 running，活动资源键释放、无项目清理失败残留。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    engine = app.state.task_engine
    fired = []
    if hook == "done_event":
        engine_cls = type(engine)
        original_event = engine_cls._event_task

        async def before_done(self, db, execution, current, event):
            if event == "done" and not fired:
                fired.append(hook)
                await asyncio.shield(asyncio.ensure_future(_change(app, client, world, change)))
            return await original_event(self, db, execution, current, event)

        monkeypatch.setattr(engine_cls, "_event_task", before_done)
    else:
        # 完成事件已核验发布、终态事务开始前：撤权不能放进终态事务内部（该事务已持写锁，会与撤权互等）。
        engine_cls = type(engine)
        original_complete = engine_cls.complete_state

        async def before_final(self, task, **kwargs):
            if (not fired and task is not None and task.kind == "outline.continue"
                    and kwargs.get("status") == "succeeded"):
                fired.append(hook)
                await asyncio.shield(asyncio.ensure_future(_change(app, client, world, change)))
            return await original_complete(self, task, **kwargs)

        monkeypatch.setattr(engine_cls, "complete_state", before_final)
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == [hook]
    assert row.status == "failed" and row.result is None, (row.status, row.error)
    assert row.error in STOPPED, row.error
    assert len(fake_llm.calls) == 1
    assert not engine.has_cleanup_failure(world.project["id"])
    await _assert_released(app, client, world, "continue", fake_llm)


@pytest.mark.parametrize("change", ["actor_token", "visibility"])
async def test_author_finalization_window_change_fails_closed(app, client, make_client, fake_llm, monkeypatch,
                                                              change):
    """作者路径受 _DRAFT_ONLY_KINDS 收尾影响的范围：完成事件已发布、终态事务前作者改密或 AI 可见性版本变化
    （任何令作者 verify_finalization 抛错的变化）由「停在 running、占资源键、登记清理失败」改为无结果 failed。
    作者在窗口外的普通成功不受影响（见 test_author_outline_unchanged_alongside_editor）。"""
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    engine_cls = type(engine)
    original_complete = engine_cls.complete_state
    fired = []

    async def bump():
        async with app.state.sessionmaker() as db:
            if change == "actor_token":
                await db.execute(update(User).where(User.id == world.owner["id"]).values(
                    token_version=User.token_version + 1,
                ))
            else:
                await db.execute(update(Project).where(Project.id == world.project["id"]).values(
                    ai_visibility_revision=Project.ai_visibility_revision + 1,
                ))
            await db.commit()

    async def before_final(self, task, **kwargs):
        if (not fired and task is not None and task.kind == "outline.continue"
                and kwargs.get("status") == "succeeded"):
            fired.append(change)
            await asyncio.shield(asyncio.ensure_future(bump()))
        return await original_complete(self, task, **kwargs)

    monkeypatch.setattr(engine_cls, "complete_state", before_final)
    fake_llm.push(CONTINUE_JSON)
    created = await _continue(client, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == [change]
    assert row.status == "failed" and row.result is None and row.user_id == world.owner["id"], row.error
    expected = "任务执行已停止" if change == "actor_token" else "任务输出授权已变化，已停止"
    assert row.error == expected
    assert len(fake_llm.calls) == 1
    assert not engine.has_cleanup_failure(world.project["id"])
    if change == "actor_token":
        # 改密后原会话失效，资源键释放由协作者以本人模型发起同一续写来证明。
        fake_llm.push(CONTINUE_JSON)
        other = await _continue(guest, world)
        assert other.status_code == 202, other.text
        assert (await _terminal(app, other.json()["id"])).status == "succeeded"
    else:
        await _assert_released(app, client, world, "continue", fake_llm)


# ---------------- 重试与作者链路 ----------------


async def test_editor_retry_keeps_round_and_reinvite_blocks_old_retry(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await _continue(guest, world)
    assert created.status_code == 202, created.text
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed"
    fake_llm.push(CONTINUE_JSON)
    retried = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    second = await _terminal(app, retried.json()["id"])
    assert second.status == "succeeded", second.error
    assert second.user_id == world.guest["id"] and second.retry_of_id == first.id
    assert second.input["_collaboration_grant"] == first.input["_collaboration_grant"]
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY, EDITOR_KEY]
    # 再失败一次，然后撤权并重新邀请：旧轮次任务不能借新邀请重试。
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    third_created = await _continue(guest, world)
    assert third_created.status_code == 202, third_created.text
    third = await _terminal(app, third_created.json()["id"])
    assert third.status == "failed"
    await _change(app, client, world, "reinvite")
    calls = len(fake_llm.calls)
    stale = await guest.post(f"/api/v1/tasks/{third.id}/retry", params={"task_key": third.statistics_key})
    assert stale.status_code == 400 and stale.json()["detail"] == "模型绑定无效或已失效", stale.text
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == third.id)) == 0
    # viewer 不能重试本人旧任务。
    await _change(app, client, world, "viewer")
    blocked = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert blocked.status_code in (400, 403, 404, 409), blocked.text
    assert len(fake_llm.calls) == calls


@pytest.mark.parametrize("kind", ["continue", "expand"])
async def test_author_outline_unchanged_alongside_editor(app, client, make_client, fake_llm, kind):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(_answer(kind))
    created = await _start(client, world, kind)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded" and row.user_id == world.owner["id"], row.error
    assert "_collaboration_grant" not in row.input
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY]
    assert (row.input["_project_model"]["source"], row.input["_project_model"]["config_owner_id"]) == (
        "actor", world.owner["id"],
    )
    # 同一资源键（续写 = 作品级、展开 = 章纲级）作者与协作者互斥：作者排队中时编辑者 409。
    await app.state.task_engine.stop()
    fake_llm.push(_answer(kind))
    queued = await _start(client, world, kind)
    assert queued.status_code == 202, queued.text
    conflict = await _start(guest, world, kind)
    assert conflict.status_code == 409, conflict.text
    await app.state.task_engine.start()
    assert (await _terminal(app, queued.json()["id"])).status == "succeeded"
    assert await _task_count(app) == 2


async def test_author_agent_outline_confirmed_through_signed_proposal(app, client, fake_llm):
    """作者 Agent 的大纲续写只能经签名提案确认（execute_admitted）受理，行为保持：确认后真实执行并产出草稿。"""
    from tests.helpers import wait_task
    from tests.test_agent_runtime import call, send_task, setup_agent, turn_action

    project, base, url = await setup_agent(client, auto=True)
    fake_llm.push_tools([call("start_task", {"kind": "outline.continue", "params": {"count": 1}}, "start")])
    task_ref = await send_task(client, url)
    task = await wait_task(client, task_ref, statuses=("paused", "failed"))
    assert task["status"] == "paused", task
    fake_llm.push(CONTINUE_JSON)
    response = await client.post(base + f"/turns/{task_ref['id']}/confirm-all",
                                 json=await turn_action(client, url, task_ref, "confirm-all"))
    assert response.status_code == 200, response.text
    change = response.json()["changes"][-1]
    assert change["status"] == "executed", response.text
    child_id = change["result"]["task_id"]
    children = [item for item in (await client.get("/api/v1/tasks", params={"kind": "outline.continue"})).json()
                if item["id"] == child_id]
    assert len(children) == 1
    done = await wait_task(client, children[0], within=15)
    assert done["status"] == "succeeded", done
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, child_id)
        assert row.user_id == (await db.get(Project, project["id"])).user_id
        assert "_collaboration_grant" not in row.input and row.input["_project_model"]["source"] == "actor"
        assert [d["title"] for d in row.result["drafts"]] == ["续章甲"]
