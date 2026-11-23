"""E4-g3c：accepted editor 的开书向导（wizard.outline / wizard.characters / wizard.world / wizard.careers）。

HTTP 入口 POST /projects/{pid}/wizard/{kind}（无同步预览）；重试沿用任务路由；向导步骤进度 PATCH
/projects/{pid}/wizard-progress 随向导按 content.edit 开放。与大纲/实体不同，向导**直接写库**（整体替换角色/世界观/
职业，覆盖或追加大纲），内容替换与完成收据在同一事务提交：
- 收据提交之前任何授权变化（受理后领取前、构建前、检索前、发送前、发送中、保存屏障前）都失败关闭：作品内容不变、
  未决凭据被系统清理、任务以失败结束、`wizard:{pid}:{kind}` 释放；
- 收据提交之后（完成事件发布前 / 终态写入前）授权变化：内容保留，引擎凭本轮收据受限收尾为 succeeded（不发布、
  不派生），不停在 running；收据被篡改则保持 running 屏障，重启恢复同样拒绝；
- 重启恢复：可证完成收据 → succeeded；只有可证未决凭据 → 撤销凭据后按普通中断收尾。
模型/费用来源、归属、私人风格与模板同 E4-f。检索（E6-d 翻转，裁决 1/9）：协作执行受理时冻结作者共享向量选择；
作者已授权且有向量配置时查询经 for_task 守卫用作者共享向量（读作者旧键 provider:model:dim）；未授权编辑者只用
关键词、作者向量模型零调用。
协作者 SSE 自 E5-f 起经受控管线开放（见 test_collaboration_stream_*、test_task_sse_scope），

本文件经任务详情或数据库观察终态。
"""

import asyncio
import json

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from app.ai.providers import AIProviderError
from app.models import (
    AIConfig,
    Career,
    Character,
    Outline,
    Project,
    ProjectCollaborator,
    RetrievalChunk,
    Task,
    User,
    WorldSetting,
    WritingStyle,
)
from app.services import kb_service, retrieval_service, task_service, wizard_service
from app.services.collaboration_ai_guard import SendGuard
from app.services.task_engine import engine as engine_module
from app.services.task_engine.handlers.wizard import WizardHandler
from tests.helpers import Gate, create_ai_config, create_project, fake_vector, wait_task
from tests.test_collaboration_entity_entry_e4g3b import (
    EDITOR_KEY,
    KB_TEXT,
    MESSAGES,
    OWNER_KEY,
    STOPPED,
    _change,
    _elsewhere,
    _index_owner_kb_in_shared_space,
    _member,
    _task_count,
    _terminal,
    _world,
)

KINDS = ("outline", "characters", "world", "careers")
NOTE = engine_module.WIZARD_RECEIPT_FINALIZED_NOTE
ANSWERS = {
    "outline": json.dumps([{"title": "新章纲", "summary": "天剑宗开篇"}], ensure_ascii=False),
    "characters": json.dumps([{"name": "新角色甲"}, {"name": "新角色乙"}, {"name": "新角色丙"}], ensure_ascii=False),
    "world": json.dumps([{"title": "新山门", "content": "云海"}], ensure_ascii=False),
    "careers": json.dumps({"careers": [{"name": "剑修", "kind": "main", "stages": [{"name": "练气"}]}]},
                          ensure_ascii=False),
}
NEW_NAMES = {"outline": ["新章纲"], "characters": ["新角色甲", "新角色乙", "新角色丙"], "world": ["新山门"],
             "careers": ["剑修"]}
_MODELS = {"outline": (Outline, Outline.title), "characters": (Character, Character.name),
           "world": (WorldSetting, WorldSetting.title), "careers": (Career, Career.name)}


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def _start(client, world, kind, **body):
    return client.post(f"{world.base}/wizard/{kind}", json=body)


async def _names(app, world, kind):
    model, column = _MODELS[kind]
    async with app.state.sessionmaker() as db:
        return sorted(await db.scalars(select(column).where(model.project_id == world.project["id"])))


async def _contents(app, world):
    return tuple([tuple(await _names(app, world, kind)) for kind in KINDS])


async def _assert_released(app, client, world, fake_llm, kind="characters"):
    """`wizard:{pid}:{kind}` 已释放：作者可立即对同一步骤发起向导并成功。"""
    fake_llm.push(ANSWERS[kind])
    author = await _start(client, world, kind)
    assert author.status_code == 202, author.text
    row = await _terminal(app, author.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.owner["id"] and "_collaboration_grant" not in row.input


def _no_pending(row):
    return not isinstance(row.checkpoint, dict) or "_wizard_pending" not in row.checkpoint


async def _settled(app, task_id, within=15.0):
    """等待执行体退出（收据不可证时任务会刻意停在 running）。"""
    engine = app.state.task_engine
    async with asyncio.timeout(within):
        while True:
            if task_id not in engine._running:
                async with app.state.sessionmaker() as db:
                    return await db.get(Task, task_id)
            await asyncio.sleep(0.02)


async def _index_owner_kb_in_legacy_space(app, owner, world):
    """作者资料全部写成作者旧键空间（provider:model:dim）中的可召回向量——E6 裁决 1 后这就是作者与授权编辑者
    共同读写的作品向量空间。shared:v2 键（无生产写入方）另行参数化：授权协作执行遇到它不发送查询向量。"""
    upload = await owner.post(f"{world.base}/kb/documents", files={
        "file": ("资料.md", KB_TEXT.encode(), "text/markdown"),
    })
    assert upload.status_code == 202, upload.text
    assert (await wait_task(owner, upload.json()["task"]))["status"] == "succeeded"
    config = await create_ai_config(owner, name="作者向量", model="owner-embed", model_type="embedding",
                                    api_key="sk-owner-embed-legacy")
    async with app.state.sessionmaker() as db:
        row = await db.get(AIConfig, config["id"])
        vector = retrieval_service.normalize_vector(fake_vector("天剑宗师承"))
        for chunk in await db.scalars(select(RetrievalChunk).where(
            RetrievalChunk.project_id == world.project["id"], RetrievalChunk.source == "kb",
        )):
            chunk.embedding = retrieval_service.encode_vector(vector)
            chunk.embedding_key = retrieval_service.make_embedding_key(retrieval_service.config_model_key(row),
                                                                       len(vector))
            chunk.version += 1
        await db.commit()
    retrieval_service.invalidate(world.project["id"])


# ---------------- 成功路径、费用来源与归属 ----------------


@pytest.mark.parametrize("space", ["shared", "legacy"])
@pytest.mark.parametrize("kind", KINDS)
async def test_editor_wizard_uses_own_model_writes_content_and_author_vectors(app, client, make_client, fake_llm,
                                                                              kind, space):
    """E6-d 翻转（原 ..._and_keyword_retrieval 的零向量断言）：作者已授权且有向量配置——
    legacy（作者旧键空间，即作品向量空间）：查询经受理冻结的作者共享向量（费用来源=作者），只调用作者向量模型一次；
    shared（shared:v2 键，裁决 1 后无生产写入方）：候选不属作者现用空间，不发送查询向量，退回关键词。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    await create_ai_config(guest, name="本人向量", model="editor-embed", model_type="embedding")
    if space == "shared":
        await _index_owner_kb_in_shared_space(app, client, world)
    else:
        await _index_owner_kb_in_legacy_space(app, client, world)
    calls_before, embeds_before = len(fake_llm.calls), len(fake_llm.embed_calls)
    fake_llm.push(ANSWERS[kind])
    created = await _start(guest, world, kind, instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    task = created.json()
    assert task["kind"] == f"wizard.{kind}"
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"] and row.project_id == world.project["id"]
    assert row.resource_key == f"wizard:{world.project['id']}:{kind}"
    assert row.input["_collaboration_grant"]["invitation_id"]
    sent = fake_llm.calls[calls_before:]
    # 本人有文本配置：即使作者授权开启也只用本人密钥。
    assert [call["api_key"] for call in sent] == [EDITOR_KEY]
    assert (row.input["_project_model"]["source"], row.input["_project_model"]["config_owner_id"]) == (
        "actor", world.guest["id"],
    )
    selection = row.input["_shared_embedding"]
    assert selection["mode"] == "vector" and selection["model"]["source"] == "owner"
    assert selection["model"]["config_owner_id"] == world.owner["id"]
    embeds = fake_llm.embed_calls[embeds_before:]
    if space == "legacy":
        assert len(embeds) == 1 and embeds[0]["model"] == "owner-embed"
    else:
        assert embeds == []
    # 关键词检索仍能命中共享资料（不是靠空索引让向量零调用）。
    assert "北斗" in str(sent[0]["messages"])
    # 向导直接写库：本步骤内容被替换为协作者结果；收据与输出全文在同一事务提交，可证终态。
    assert await _names(app, world, kind) == sorted(NEW_NAMES[kind])
    assert row.result["kind"] == kind and row.error == ""
    async with app.state.sessionmaker() as db:
        assert wizard_service.verified_wizard_terminal_receipt(row, project_owner_id=world.owner["id"])
        assert not wizard_service.verified_wizard_terminal_receipt(row)  # 不能由任务输入自报作者
        assert not await task_service.has_unresolved_receipt(db, row)
        # 不派生其他任务（含向量索引任务）。
        assert await db.scalar(select(func.count(Task.id)).where(
            Task.id != row.id, Task.user_id == world.guest["id"],
        )) == 0
    # 已提交的内容不因随后撤权回滚（Task 仍是本人历史）。
    await _change(app, client, world, "revoked")
    assert await _names(app, world, kind) == sorted(NEW_NAMES[kind])
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, row.id)).status == "succeeded"


@pytest.mark.parametrize("kind", ["outline", "characters"])
async def test_unauthorized_editor_wizard_retrieval_is_keyword_only(app, client, make_client, fake_llm, kind):
    """未授权编辑者：作者旧键空间有可召回向量、本人也有向量配置，仍只走关键词，任何向量模型零调用。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=False)
    await create_ai_config(guest, name="本人向量", model="editor-embed", model_type="embedding")
    await _index_owner_kb_in_legacy_space(app, client, world)
    embeds_before = len(fake_llm.embed_calls)
    fake_llm.push(ANSWERS[kind])
    created = await _start(guest, world, kind, instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.input["_shared_embedding"]["mode"] == "keyword"
    assert fake_llm.embed_calls[embeds_before:] == []
    assert "北斗" in str(fake_llm.calls[-1]["messages"])


async def test_author_wizard_freezes_shared_embedding_without_grant(app, client, make_client, fake_llm):
    """E6-f 翻转（裁决 13，原 test_author_wizard_input_unchanged_without_shared_embedding）：作者本人的向导任务
    同样在受理时冻结共享向量选择（无 grant；作者无向量配置 → keyword），与大纲 / 实体一致；向量路径的正例与
    发送前重验见 test_task_shared_embedding_e6f。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    fake_llm.push(ANSWERS["world"])
    created = await _start(client, world, "world")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert "_collaboration_grant" not in row.input
    selection = row.input["_shared_embedding"]
    assert selection["mode"] == "keyword" and selection["reason"] == "embedding_config_missing"


@pytest.mark.parametrize("change", ["owner_ai_direct", "revoked", "viewer"])
async def test_editor_wizard_vector_query_fails_closed_before_send(app, client, make_client, fake_llm, change):
    """查询向量已过 prepare_provider、尚未进入真实发送门禁时撤权 / 降级 / 直接改库关授权：零向量发送、
    零文本调用、作品内容不变、未决凭据清理（无通知的直接改库由发送租约内重验拦截）。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    await _index_owner_kb_in_legacy_space(app, client, world)
    before = await _names(app, world, "world")
    embeds_before, calls_before = len(fake_llm.embed_calls), len(fake_llm.calls)
    gate = Gate()
    fake_llm.embed_before_gates = [gate]
    fake_llm.push(ANSWERS["world"])
    created = await _start(guest, world, "world", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    changes: list = []
    await _elsewhere(app, client, world, change, changes)()
    gate.release()
    row = await _settled(app, created.json()["id"])
    assert row.status in ("failed", "cancelled"), row.status
    assert fake_llm.embed_calls[embeds_before:] == [] and len(fake_llm.calls) == calls_before
    assert await _names(app, world, "world") == before
    assert _no_pending(row)


async def test_editor_wizard_owner_model_fallback_records_cost_source(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    fake_llm.push(ANSWERS["world"])
    created = await _start(guest, world, "world")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"]
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY]
    model = row.input["_project_model"]
    assert (model["source"], model["config_owner_id"], model["config_id"]) == (
        "owner", world.owner["id"], world.owner_config["id"],
    )
    assert fake_llm.calls[0]["model"] == "owner-model"
    detail = await guest.get(f"/api/v1/tasks/{row.id}", params={"task_key": row.statistics_key})
    # 任务详情不泄露作者配置身份与私有绑定。
    assert detail.status_code == 200 and detail.json().get("ai_config_id") is None
    assert "_project_model" not in detail.text and OWNER_KEY not in detail.text


@pytest.mark.parametrize("case", ["no_model_no_grant", "broken_own_model", "explicit_owner_config"])
async def test_editor_wizard_model_errors_never_fall_back(app, client, make_client, fake_llm, case):
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
    before = await _contents(app, world)
    for kind in KINDS:
        response = await _start(guest, world, kind, **body)
        assert response.status_code in ((400, 404) if case == "explicit_owner_config" else (400,)), response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []
    assert await _contents(app, world) == before


async def test_editor_wizard_hides_owner_private_style_and_template(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    detail = (await client.get("/api/v1/prompt-templates/wizard.world")).json()
    saved = await client.put("/api/v1/prompt-templates/wizard.world", json={
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
    fake_llm.push(ANSWERS["world"])
    created = await _start(guest, world, "world")
    assert created.status_code == 202, created.text
    assert (await _terminal(app, created.json()["id"])).status == "succeeded"
    sent = str(fake_llm.calls[0]["messages"])
    assert fake_llm.calls[0]["api_key"] == EDITOR_KEY
    for secret in ("作者私有模板标记壬癸", "私有描述甲乙丙", "作者私有风格"):
        assert secret not in sent
    # 作者自己的向导仍使用作者模板与风格（正对照）。
    fake_llm.push(ANSWERS["world"])
    mine = await _start(client, world, "world")
    assert mine.status_code == 202, mine.text
    assert (await _terminal(app, mine.json()["id"])).status == "succeeded"
    own = str(fake_llm.calls[1]["messages"])
    assert "作者私有模板标记壬癸" in own and "私有描述甲乙丙" in own


@pytest.mark.parametrize("case", ["viewer", "pending", "outsider", "revoked", "admin_outsider", "admin_viewer"])
async def test_non_editors_cannot_start_wizard(app, client, make_client, fake_llm, case):
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
    before = await _contents(app, world)
    for kind in KINDS:
        response = await _start(guest, world, kind)
        assert response.status_code == expected, (kind, response.text)
        assert "师父是乙" not in response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []
    assert await _contents(app, world) == before


async def test_editor_wizard_rejected_for_other_book_and_recycled_project(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    other = await create_project(client, title="外部作品")
    outside = await guest.post(f"/api/v1/projects/{other['id']}/wizard/characters", json={})
    assert outside.status_code == 404, outside.text
    assert (await client.delete(world.base)).status_code == 204
    for kind in KINDS:
        response = await _start(guest, world, kind)
        assert response.status_code == 404, (kind, response.text)
    assert await _task_count(app) == 0 and fake_llm.calls == []


async def _hide_entry(client, world, kind, ident, **values):
    url = f"{world.base}/setting-entries/{kind}/{ident}/ai-settings"
    current = await client.get(url)
    assert current.status_code == 200, current.text
    response = await client.patch(url, json={
        "expected_ai_visibility_revision": current.json()["ai_visibility_revision"], **values,
    })
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("kind", ["outline", "characters"])
async def test_editor_wizard_prompt_respects_owner_ai_visibility(app, client, make_client, fake_llm, kind):
    """作者隐藏的字段与 never 条目不进入协作者向导提示词（未隐藏字段正对照照常发送）。"""
    guest = make_client()
    world = await _world(client, guest)
    for ident, personality in zip(world.chars, ("甲隐藏性格密语", "乙公开性格"), strict=True):
        patched = await client.patch(f"{world.base}/characters/{ident}", json={"personality": personality})
        assert patched.status_code == 200, patched.text
    never = await client.post(f"{world.base}/characters", json={"name": "丙永不发送", "profile": "丙档案密语"})
    assert never.status_code == 201, never.text
    shown = await client.post(f"{world.base}/world", json={"title": "公开山门", "content": "公开云海"})
    hidden = await client.post(f"{world.base}/world", json={"title": "隐字山门", "content": "世界隐藏密语"})
    secret = await client.post(f"{world.base}/world", json={"title": "永不发送之地", "content": "永不之地密语"})
    assert all(r.status_code == 201 for r in (shown, hidden, secret)), (shown.text, hidden.text, secret.text)
    await _hide_entry(client, world, "character", world.chars[0], ai_hidden_builtin_fields=["personality"])
    await _hide_entry(client, world, "character", never.json()["id"], ai_mode="never")
    await _hide_entry(client, world, "world", hidden.json()["id"], ai_hidden_builtin_fields=["content"])
    await _hide_entry(client, world, "world", secret.json()["id"], ai_mode="never")
    fake_llm.push(ANSWERS[kind])
    instruction = "写甲、乙、丙永不发送、公开山门、隐字山门、永不发送之地的故事"
    created = await _start(guest, world, kind, instruction=instruction)
    assert created.status_code == 202, created.text
    assert (await _terminal(app, created.json()["id"])).status == "succeeded"
    (call,) = fake_llm.calls
    prompt = str(call["messages"])
    # 正对照：未隐藏的世界观正文照常发送；大纲向导还带未隐藏的角色字段。
    assert "公开云海" in prompt and "隐字山门" in prompt  # 隐藏字段的条目本身仍在，只去掉被隐藏的正文。
    if kind == "outline":
        assert "乙公开性格" in prompt
    for leaked in ("甲隐藏性格密语", "丙档案密语", "世界隐藏密语", "永不之地密语"):
        assert leaked not in prompt, leaked


async def test_wizard_progress_follows_content_edit(app, client, make_client):
    guest = make_client()
    world = await _world(client, guest)
    url = f"{world.base}/wizard-progress"
    edited = await guest.patch(url, json={"current": "world", "done": ["outline"]})
    assert edited.status_code == 200, edited.text
    assert edited.json()["wizard_progress"]["current"] == "world"
    await _change(app, client, world, "viewer")
    assert (await guest.patch(url, json={"current": "careers"})).status_code == 403
    await _change(app, client, world, "revoked")
    assert (await guest.patch(url, json={"current": "careers"})).status_code == 404
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, world.project["id"])).wizard_progress["current"] == "world"
    # 向导属 AI 工作流：管理员人工编辑权限不扩展到此处，仅管理员身份（含管理员兼 viewer）保持原严格作者 404/403。
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == world.guest["id"]).values(is_admin=True))
        await db.commit()
    assert (await guest.patch(url, json={"current": "careers"})).status_code == 404
    # 管理员兼 viewer：管理员人工编辑权限不能补足 viewer 的只读身份。
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ).values(status="accepted", role="viewer"))
        await db.commit()
    assert (await guest.patch(url, json={"current": "careers"})).status_code == 403
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, world.project["id"])).wizard_progress["current"] == "world"
    # 管理员兼 editor：按 editor 身份可写。
    async with app.state.sessionmaker() as db:
        await db.execute(update(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ).values(role="editor"))
        await db.commit()
    assert (await guest.patch(url, json={"current": "careers"})).status_code == 200


@pytest.mark.parametrize("change", ["revoked", "viewer", "reinvite", "owner_ai"])
async def test_change_between_preflight_and_admission_creates_no_task(app, client, make_client, fake_llm,
                                                                    monkeypatch, change):
    """锁外预检（ai.use 短读、模型、DNS）完成后、受理事务前授权变化：受理锁内按原 grant 重验，零任务零调用。"""
    from app.services.task_engine import admission as admission_module

    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai")
    original = admission_module.prepare_task_admission
    fired = []

    async def change_after_preflight(*args, **kwargs):
        proof = await original(*args, **kwargs)
        if not fired:
            fired.append(change)
            await _change(app, client, world, change)
        return proof

    monkeypatch.setattr(admission_module, "prepare_task_admission", change_after_preflight)
    response = await _start(guest, world, "characters")
    assert fired == [change]
    assert response.status_code in (400, 403, 404, 409), response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []


# ---------------- 受理后领取前、构建前、检索前、发送前 ----------------


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "owner_ai_direct", "disabled", "reinvite"])
async def test_change_after_admission_before_claim_fails_closed(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    owner_model = change.startswith("owner_ai")
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_model)
    before = await _contents(app, world)
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    await _change(app, client, world, change)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    expected = {"disabled": "collaboration_account_unavailable", "owner_ai_direct": "collaboration_model_revoked"}
    assert row.status == "failed" and row.error == MESSAGES[expected.get(change, "collaboration_revoked")]
    assert row.result is None and fake_llm.calls == [] and _no_pending(row)
    assert await _contents(app, world) == before
    await _assert_released(app, client, world, fake_llm)


@pytest.mark.parametrize("stage", ["build", "retrieval", "send"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_change_after_claim_before_first_send_never_sends(app, client, make_client, fake_llm, monkeypatch,
                                                                stage, change):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    if stage == "retrieval":
        upload = await client.post(f"{world.base}/kb/documents", files={
            "file": ("资料.md", KB_TEXT.encode(), "text/markdown"),
        })
        assert upload.status_code == 202, upload.text
        assert (await wait_task(client, upload.json()["task"]))["status"] == "succeeded"
    before = await _contents(app, world)
    original_build = WizardHandler.build_job
    original_require = SendGuard.require_task_write
    original_job = wizard_service.build_wizard_job
    original_search = kb_service.search_hits
    fired, changes, searched = asyncio.Event(), [], []
    revoke = _elsewhere(app, client, world, change, changes)

    async def before_build(self, ctx, task, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_build(self, ctx, task, **kwargs)

    async def before_retrieval_check(self, db):
        # 协作执行在 build 内的第一次 require_task_write 就是检索前的锁内重验。
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_require(self, db)

    async def after_job_build(*args, **kwargs):
        job = await original_job(*args, **kwargs)
        if not fired.is_set():
            fired.set()
            await revoke()
        return job

    async def recording_search(*args, **kwargs):
        searched.append(args[2] if len(args) > 2 else kwargs.get("query"))
        return await original_search(*args, **kwargs)

    monkeypatch.setattr(kb_service, "search_hits", recording_search)
    if stage == "build":
        monkeypatch.setattr(WizardHandler, "build_job", before_build)
    elif stage == "retrieval":
        monkeypatch.setattr(SendGuard, "require_task_write", before_retrieval_check)
    else:
        monkeypatch.setattr(wizard_service, "build_wizard_job", after_job_build)
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired.is_set()
    assert row.status == "failed" and row.output_text == "" and row.result is None, row.error
    assert fake_llm.calls == [] and fake_llm.embed_calls == [] and _no_pending(row)
    assert await _contents(app, world) == before
    if stage in ("build", "retrieval"):
        # 检索前锁内重验失败：关键词检索都未执行，不读取共享资料。
        assert searched == []


async def test_build_job_rejects_revoked_grant_without_notification(app, client, make_client, fake_llm, monkeypatch):
    """构建前的锁内重验本身即失败关闭（不依赖本进程通知取消执行体），未决凭据从未写入。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)
    original_build = WizardHandler.build_job
    original_begin = wizard_service.begin_wizard_pending
    seen, begun = [], []

    async def before_build(self, ctx, task, **kwargs):
        if not seen:
            seen.append(task.id)
            await _change(app, client, world, "revoked")
        return await original_build(self, ctx, task, **kwargs)

    async def recording_begin(*args, **kwargs):
        begun.append(True)
        return await original_begin(*args, **kwargs)

    monkeypatch.setattr(WizardHandler, "build_job", before_build)
    monkeypatch.setattr(wizard_service, "begin_wizard_pending", recording_begin)
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert seen and row.status == "failed" and row.result is None, row.error
    assert fake_llm.calls == [] and begun == [] and _no_pending(row)
    await _assert_released(app, client, world, fake_llm)


# ---------------- 发送中与保存屏障 ----------------


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_change_during_send_never_writes_content(app, client, make_client, fake_llm, monkeypatch, change,
                                                       notify):
    """notify=False 模拟跨实例/未收到通知：只能靠逐帧发布核验与保存锁内重验拒绝旧结果。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    before = await _contents(app, world)
    gate = Gate()
    fake_llm.push(['[{"name":"旧授权', gate, '下的角色"},{"name":"乙二"},{"name":"丙三"}]'])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _change(app, client, world, change)
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert "下的角色" not in (row.output_text or "") and row.result is None and _no_pending(row)
    assert len(fake_llm.calls) == 1
    assert await _contents(app, world) == before
    assert not app.state.task_engine.has_cleanup_failure(world.project["id"])
    await _assert_released(app, client, world, fake_llm)


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct", "disabled"])
async def test_change_before_save_transaction_never_writes_content(app, client, make_client, fake_llm, monkeypatch,
                                                                   change, notify):
    """模型输出已完整发布、内容替换事务开始前授权变化：保存锁内（require_write_execution）按原 grant 重验失败，
    内容与收据都不写入，未决凭据经系统清理（不要求当前授权），任务失败而非停在 running。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    before = await _contents(app, world)
    original_save = wizard_service.save_wizard_result
    fired, changes = [], []
    revoke = _elsewhere(app, client, world, change, changes)

    async def revoke_then_save(*args, **kwargs):
        if not fired:
            fired.append(change)
            await revoke()
        return await original_save(*args, **kwargs)

    monkeypatch.setattr(wizard_service, "save_wizard_result", revoke_then_save)
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired == [change]
    assert row.status == "failed" and row.result is None and row.error in STOPPED, (row.status, row.error)
    assert _no_pending(row) and "_wizard_completion" not in (row.checkpoint or {})
    assert len(fake_llm.calls) == 1
    assert await _contents(app, world) == before
    assert not app.state.task_engine.has_cleanup_failure(world.project["id"])
    if change != "disabled":
        await _assert_released(app, client, world, fake_llm)


# ---------------- 收据提交之后：完成事件发布前与终态前 ----------------


def _hook_after_commit(monkeypatch, app, hook, action, kind="characters"):
    engine_cls = type(app.state.task_engine)
    fired = []
    if hook == "done_event":
        original_event = engine_cls._event_task

        async def before_done(self, db, execution, current, event):
            if event == "done" and not fired and execution.kind == f"wizard.{kind}":
                fired.append(hook)
                await asyncio.shield(asyncio.ensure_future(action()))
            return await original_event(self, db, execution, current, event)

        monkeypatch.setattr(engine_cls, "_event_task", before_done)
    else:
        # 完成事件已核验发布、终态事务开始前：撤权不能放进终态事务内部（该事务已持写锁，会与撤权互等）。
        original_complete = engine_cls.complete_state

        async def before_final(self, task, **kwargs):
            if (not fired and task is not None and task.kind == f"wizard.{kind}"
                    and kwargs.get("status") == "succeeded"):
                fired.append(hook)
                await asyncio.shield(asyncio.ensure_future(action()))
            return await original_complete(self, task, **kwargs)

        monkeypatch.setattr(engine_cls, "complete_state", before_final)
    return fired


@pytest.mark.parametrize("hook", ["done_event", "finalization"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct", "actor_token"])
async def test_change_after_commit_finalizes_from_receipt(app, client, make_client, fake_llm, monkeypatch, hook,
                                                          change):
    """内容与收据已同事务提交、完成事件或终态核验前授权变化：内容保留，按本轮收据受限收尾为 succeeded（不发布），
    不停在 running、无清理失败残留，`wizard:{pid}:characters` 释放；终态可被证明（可裁剪/删除）。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    fired = _hook_after_commit(monkeypatch, app, hook, lambda: _change(app, client, world, change))
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == [hook]
    assert row.status == "succeeded" and row.error == NOTE, (row.status, row.error)
    assert row.result == row.checkpoint["_wizard_completion"]["result"] and row.result["kind"] == "characters"
    assert row.execution_key is None and len(fake_llm.calls) == 1
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])
    assert not app.state.task_engine.has_cleanup_failure(world.project["id"])
    async with app.state.sessionmaker() as db:
        assert not await task_service.has_unresolved_receipt(db, row)
    await _assert_released(app, client, world, fake_llm)


async def test_direct_role_downgrade_before_finalization_finalizes_from_receipt(app, client, make_client, fake_llm,
                                                                                monkeypatch):
    """不递增 revision 的直接降级（role=viewer）落在终态前：终态事务内的原 grant 重验（require_write_execution）
    与模型绑定重验（按当前能力重新推导）任一层都会拒绝成功发布；内容已提交，按收据收尾。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)

    async def demote():
        member = await _member(app, world)
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.id == member.id).values(
                role="viewer",
            ))
            await db.commit()

    fired = _hook_after_commit(monkeypatch, app, "finalization", demote)
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == ["finalization"]
    assert row.status == "succeeded" and row.error == NOTE, (row.status, row.error)
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])


def _diagnose(app, row, **extra):
    """断言失败时的定位信息：只取状态与标记，不含任务输入、正文或模型输出。"""
    checkpoint = row.checkpoint if isinstance(row.checkpoint, dict) else {}
    engine = app.state.task_engine
    return {
        "status": row.status, "error": row.error, "execution_key_cleared": row.execution_key is None,
        "pending": "_wizard_pending" in checkpoint, "completion": "_wizard_completion" in checkpoint,
        "cleanup_failure": engine.has_cleanup_failure(row.project_id), **extra,
    }


async def test_revocation_notification_after_commit_still_finalizes_from_receipt(app, client, make_client, fake_llm,
                                                                                 monkeypatch):
    """本进程撤权通知取消执行体、取消落在内容提交之后：同样凭收据收尾为 succeeded。

    取消的确切落点由调度决定；各落点另有下方 `test_revocation_cancel_window_*` 逐一固定验证。
    """
    guest = make_client()
    world = await _world(client, guest)
    fired = _hook_after_commit(monkeypatch, app, "done_event", lambda: _change(app, client, world, "revoked"))
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    diag = _diagnose(app, row, fired=fired)
    assert fired == ["done_event"], diag
    assert row.status == "succeeded" and row.error == NOTE, diag
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"]), diag
    await _assert_released(app, client, world, fake_llm)


# ---------------- 撤权通知取消的落点：逐一固定 ----------------


def _record_revocation_cancel(monkeypatch, app):
    """撤权复查调用 cancel_running 时置位；不改变取消行为本身。"""
    engine_cls = type(app.state.task_engine)
    original = engine_cls.cancel_running
    reached = asyncio.Event()

    async def recording(self, task_id, **kwargs):
        reached.set()
        return await original(self, task_id, **kwargs)

    monkeypatch.setattr(engine_cls, "cancel_running", recording)
    return reached


async def _revoke_and_wait_cancel(app, client, world, reached, cancelled):
    """真实撤权（含本进程通知），等到复查把取消投递给执行体；取消若打断当前协程则记下并原样传递。"""
    await asyncio.shield(asyncio.ensure_future(_change(app, client, world, "revoked")))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        # 取消与置位在复查的同一步内发出；让出一次以便取消在此处落地。
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        cancelled.append(True)
        raise


async def _run_cancel_window(app, client, guest, world, fake_llm):
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    return await _terminal(app, created.json()["id"])


async def _assert_receipt_finalized(app, client, world, fake_llm, row, **extra):
    diag = _diagnose(app, row, **extra)
    assert row.status == "succeeded" and row.error == NOTE, diag
    assert row.result == row.checkpoint["_wizard_completion"]["result"], diag
    assert row.execution_key is None and _no_pending(row), diag
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"]), diag
    assert not app.state.task_engine.has_cleanup_failure(world.project["id"]), diag
    async with app.state.sessionmaker() as db:
        assert not await task_service.has_unresolved_receipt(db, row), diag
    await _assert_released(app, client, world, fake_llm)


async def test_revocation_cancel_window_during_done_verification(app, client, make_client, fake_llm, monkeypatch):
    """W1：内容与收据已提交，取消投递时执行体正等待 done 事件核验（核验在独立协程中，取消不打断它）。
    核验因撤权失败、取消被吞，执行体按收据收尾为 succeeded，不发布结果。"""
    guest = make_client()
    world = await _world(client, guest)
    reached = _record_revocation_cancel(monkeypatch, app)
    engine_cls = type(app.state.task_engine)
    original_event = engine_cls._event_task
    fired, cancelled, requested = [], [], []

    async def before_done(self, db, execution, current, event):
        if event == "done" and not fired and execution.kind == "wizard.characters":
            fired.append(event)
            await _revoke_and_wait_cancel(app, client, world, reached, cancelled)
            requested.append(execution.task_id in self._cancel_requested)
        return await original_event(self, db, execution, current, event)

    monkeypatch.setattr(engine_cls, "_event_task", before_done)
    row = await _run_cancel_window(app, client, guest, world, fake_llm)
    # 复查确已对执行体发出取消。
    assert fired == ["done"] and requested == [True], _diagnose(app, row, fired=fired, requested=requested)
    # 核验协程受 shield 保护：取消只落在执行体上，不打断核验本身。
    assert cancelled == [], _diagnose(app, row, cancelled=cancelled)
    await _assert_receipt_finalized(app, client, world, fake_llm, row, window="W1")


async def test_revocation_cancel_window_after_done_published(app, client, make_client, fake_llm, monkeypatch):
    """W2：done 已通过核验并写入执行体结果，取消落在执行体进入收尾之前。按「done 后才收到取消」视作成功提交，
    终态核验因撤权拒绝发布，再按收据收尾为 succeeded。"""
    guest = make_client()
    world = await _world(client, guest)
    reached = _record_revocation_cancel(monkeypatch, app)
    original_run = WizardHandler.run
    fired, cancelled = [], []

    async def run_then_revoke(self, job, ctx):
        async for event, data in original_run(self, job, ctx):
            yield event, data
            if event == "done" and not fired:
                fired.append(event)
                await _revoke_and_wait_cancel(app, client, world, reached, cancelled)

    monkeypatch.setattr(WizardHandler, "run", run_then_revoke)
    row = await _run_cancel_window(app, client, guest, world, fake_llm)
    assert fired == ["done"] and cancelled == [True], _diagnose(app, row, fired=fired, cancelled=cancelled)
    await _assert_receipt_finalized(app, client, world, fake_llm, row, window="W2")


async def test_revocation_cancel_window_during_finalization(app, client, make_client, fake_llm, monkeypatch):
    """W3：撤权与通知落在执行体收尾（终态事务前）期间。复查见到收尾中的任务即跳过，不取消执行体；
    终态核验按原 grant 拒绝发布，按收据收尾为 succeeded。"""
    guest = make_client()
    world = await _world(client, guest)
    reached = _record_revocation_cancel(monkeypatch, app)
    engine = app.state.task_engine
    engine_cls = type(engine)
    original_complete = engine_cls.complete_state
    fired, observed = [], []

    async def revoke_before_final(self, task, **kwargs):
        if (not fired and task is not None and task.kind == "wizard.characters"
                and kwargs.get("status") == "succeeded"):
            fired.append(task.id)
            await asyncio.shield(asyncio.ensure_future(_change(app, client, world, "revoked")))
            checks = set(self._collaboration_checks)
            if checks:
                await asyncio.wait(checks, timeout=5)
            observed.append((bool(checks), all(c.done() for c in checks), task.id in self._cancel_requested))
        return await original_complete(self, task, **kwargs)

    monkeypatch.setattr(engine_cls, "complete_state", revoke_before_final)
    row = await _run_cancel_window(app, client, guest, world, fake_llm)
    diag = _diagnose(app, row, fired=fired, observed=observed, cancel_reached=reached.is_set())
    assert len(fired) == 1, diag
    # 复查已运行并结束，且没有对收尾中的执行体发出取消。
    assert observed == [(True, True, False)] and not reached.is_set(), diag
    await _assert_receipt_finalized(app, client, world, fake_llm, row, window="W3")


async def test_revocation_after_terminal_leaves_task_untouched(app, client, make_client, fake_llm):
    """W4：执行体已结束后撤权：复查不再有运行中目标，已提交的成功终态与结果保持不变。"""
    guest = make_client()
    world = await _world(client, guest)
    row = await _run_cancel_window(app, client, guest, world, fake_llm)
    assert row.status == "succeeded" and row.error == "", _diagnose(app, row)
    await _change(app, client, world, "revoked")
    engine = app.state.task_engine
    checks = set(engine._collaboration_checks)
    if checks:
        await asyncio.wait(checks, timeout=5)
    async with app.state.sessionmaker() as db:
        after = await db.get(Task, row.id)
    assert (after.status, after.error, after.result) == (row.status, row.error, row.result), _diagnose(app, after)
    assert not engine.has_cleanup_failure(world.project["id"])
    await _assert_released(app, client, world, fake_llm)


async def test_tampered_receipt_keeps_running_barrier_and_recover_refuses(app, client, make_client, fake_llm,
                                                                          monkeypatch):
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)
    holder = {}

    async def tamper_then_revoke():
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, holder["id"])
            checkpoint = json.loads(json.dumps(row.checkpoint))
            checkpoint["_wizard_completion"]["result_hash"] = "0" * 64
            row.checkpoint = checkpoint
            await db.commit()
        await _change(app, client, world, "revoked")

    fired = _hook_after_commit(monkeypatch, app, "done_event", tamper_then_revoke)
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    holder["id"] = created.json()["id"]
    row = await _settled(app, holder["id"])
    assert fired == ["done_event"]
    assert row.status == "running" and row.execution_key is not None
    assert app.state.task_engine.has_cleanup_failure(world.project["id"])
    await app.state.task_engine.stop()
    await app.state.task_engine.start()
    async with app.state.sessionmaker() as db:
        again = await db.get(Task, holder["id"])
        assert (again.status, again.execution_key) == ("running", row.execution_key)


# ---------------- 重启恢复 ----------------


@pytest.mark.parametrize("temporary_read_failure", [False, True])
async def test_recover_finalizes_committed_receipt_when_in_process_finalize_unavailable(
    app, client, make_client, fake_llm, monkeypatch, temporary_read_failure,
):
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    original = type(engine)._complete_from_wizard_receipt

    async def unavailable(self, task, **kwargs):
        return None

    monkeypatch.setattr(type(engine), "_complete_from_wizard_receipt", unavailable)
    fired = _hook_after_commit(monkeypatch, app, "done_event", lambda: _change(app, client, world, "revoked"))
    fake_llm.push(ANSWERS["characters"])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    stuck = await _settled(app, created.json()["id"])
    assert fired == ["done_event"] and stuck.status == "running"
    monkeypatch.setattr(type(engine), "_complete_from_wizard_receipt", original)
    await engine.stop()
    if temporary_read_failure:
        async def busy_on_read(*args, **kwargs):
            raise OperationalError(None, None, RuntimeError("模拟收据读暂时失败"))

        with monkeypatch.context() as patch:
            patch.setattr(wizard_service, "completed_wizard_receipt", busy_on_read)
            await engine.start()
            async with app.state.sessionmaker() as db:
                waiting = await db.get(Task, stuck.id)
                assert waiting.status == "running" and waiting.worker_key == engine.lease.worker_key
                assert waiting.recovered_at is None
        # 继续由同一份有效登记处理原收据。无需重启进程或等待本进程失租。
        await engine.recover()
    else:
        await engine.start()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, stuck.id)
        assert (row.status, row.error, row.execution_key) == ("succeeded", NOTE, None)
        assert row.result == row.checkpoint["_wizard_completion"]["result"]
        assert not await task_service.has_unresolved_receipt(db, row)
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])
    await _assert_released(app, client, world, fake_llm)


@pytest.mark.parametrize("actor", ["editor", "author"])
async def test_recover_releases_provable_uncommitted_pending(app, client, make_client, fake_llm, monkeypatch, actor):
    """只带逐字可证未决凭据（内容从未提交）的遗留 running 向导：撤销凭据后按普通中断收尾，资源释放（作者同样适用，
    此前一律保持待核对）。"""
    guest = make_client()
    world = await _world(client, guest)
    before = await _contents(app, world)

    original = wizard_service.discard_wizard_pending

    async def unavailable(*args, **kwargs):
        return False

    monkeypatch.setattr(wizard_service, "discard_wizard_pending", unavailable)
    fake_llm.push([AIProviderError("模型调用失败：上游中断")])
    created = await _start(guest if actor == "editor" else client, world, "characters")
    assert created.status_code == 202, created.text
    stuck = await _settled(app, created.json()["id"])
    assert stuck.status == "running" and "_wizard_pending" in stuck.checkpoint
    monkeypatch.setattr(wizard_service, "discard_wizard_pending", original)
    await app.state.task_engine.stop()
    await app.state.task_engine.start()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, stuck.id)
        assert (row.status, row.execution_key) == ("interrupted", None) and _no_pending(row)
    assert await _contents(app, world) == before
    await _assert_released(app, client, world, fake_llm)


@pytest.mark.parametrize("damage", ["input_hash", "nonce", "with_completion"])
async def test_recover_keeps_damaged_pending_unresolved(app, client, make_client, fake_llm, monkeypatch, damage):
    """未决凭据与原 Task 不逐字一致（或与完成收据并存）时不能推断“从未提交”：recover 保留 running 待核对屏障。"""
    guest = make_client()
    world = await _world(client, guest)

    async def unavailable(*args, **kwargs):
        return False

    monkeypatch.setattr(wizard_service, "discard_wizard_pending", unavailable)
    fake_llm.push([AIProviderError("模型调用失败：上游中断")])
    created = await _start(guest, world, "characters")
    assert created.status_code == 202, created.text
    stuck = await _settled(app, created.json()["id"])
    assert stuck.status == "running" and "_wizard_pending" in stuck.checkpoint
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, stuck.id)
        checkpoint = json.loads(json.dumps(row.checkpoint))
        if damage == "input_hash":
            checkpoint["_wizard_pending"]["input_hash"] = "0" * 64
        elif damage == "nonce":
            checkpoint["_wizard_pending"]["nonce"] = "not-a-uuid"
        else:
            checkpoint["_wizard_completion"] = {**checkpoint["_wizard_pending"], "result": {"kind": "characters"}}
        row.checkpoint = checkpoint
        await db.commit()
    await app.state.task_engine.stop()
    await app.state.task_engine.start()
    async with app.state.sessionmaker() as db:
        again = await db.get(Task, stuck.id)
        assert (again.status, again.execution_key) == ("running", stuck.execution_key)
        assert "_wizard_pending" in again.checkpoint
    assert app.state.task_engine.has_cleanup_failure(world.project["id"])


# ---------------- 续写、重试与作者链路 ----------------


PARTIAL = '[{"name":"续甲"},{"name":"续'
REST = ',{"name":"续乙"},{"name":"续丙"}]'


async def _failed_partial(app, client, world, fake_llm):
    fake_llm.push([PARTIAL, AIProviderError("模型调用失败：上游中断")])
    created = await _start(client, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.output_text == PARTIAL
    return row


async def test_editor_resumes_own_task_only_within_same_round(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    source = await _failed_partial(app, guest, world, fake_llm)
    fake_llm.push(REST)
    resumed = await _start(guest, world, "characters", continue_from_task_id=source.id,
                           continue_from_task_key=source.statistics_key)
    assert resumed.status_code == 202, resumed.text
    row = await _terminal(app, resumed.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.retry_of_id == source.id and row.input["_collaboration_grant"] == source.input["_collaboration_grant"]
    assert await _names(app, world, "characters") == ["续丙", "续乙", "续甲"]
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY, EDITOR_KEY]
    # 作者不能接协作者的任务（不同真实 actor）。
    second = await _failed_partial(app, guest, world, fake_llm)
    calls = len(fake_llm.calls)
    by_author = await _start(client, world, "characters", continue_from_task_id=second.id,
                             continue_from_task_key=second.statistics_key)
    assert by_author.status_code == 400, by_author.text
    # 撤权重邀后：旧轮次来源不能借新邀请续写。
    await _change(app, client, world, "reinvite")
    stale = await _start(guest, world, "characters", continue_from_task_id=second.id,
                         continue_from_task_key=second.statistics_key)
    assert stale.status_code == 400, stale.text
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == second.id)) == 0


async def test_editor_cannot_resume_author_task(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    source = await _failed_partial(app, client, world, fake_llm)
    calls = len(fake_llm.calls)
    response = await _start(guest, world, "characters", continue_from_task_id=source.id,
                            continue_from_task_key=source.statistics_key)
    assert response.status_code == 400 and "续甲" not in response.text, response.text
    assert len(fake_llm.calls) == calls


async def test_editor_retry_keeps_round_and_reinvite_blocks_old_retry(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await _start(guest, world, "world")
    assert created.status_code == 202, created.text
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed" and _no_pending(first)
    fake_llm.push(ANSWERS["world"])
    retried = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    second = await _terminal(app, retried.json()["id"])
    assert second.status == "succeeded", second.error
    assert second.user_id == world.guest["id"] and second.retry_of_id == first.id
    assert second.input["_collaboration_grant"] == first.input["_collaboration_grant"]
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY, EDITOR_KEY]
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    third_created = await _start(guest, world, "world")
    assert third_created.status_code == 202, third_created.text
    third = await _terminal(app, third_created.json()["id"])
    assert third.status == "failed"
    await _change(app, client, world, "reinvite")
    calls = len(fake_llm.calls)
    stale = await guest.post(f"/api/v1/tasks/{third.id}/retry", params={"task_key": third.statistics_key})
    assert stale.status_code in (400, 409), stale.text
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == third.id)) == 0
    await _change(app, client, world, "viewer")
    blocked = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert blocked.status_code in (400, 403, 404, 409), blocked.text
    assert len(fake_llm.calls) == calls


@pytest.mark.parametrize("kind", KINDS)
async def test_author_wizard_unchanged_alongside_editor(app, client, make_client, fake_llm, kind):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(ANSWERS[kind])
    created = await _start(client, world, kind)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded" and row.user_id == world.owner["id"] and row.error == "", row.error
    assert "_collaboration_grant" not in row.input
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY]
    assert (row.input["_project_model"]["source"], row.input["_project_model"]["config_owner_id"]) == (
        "actor", world.owner["id"],
    )
    assert await _names(app, world, kind) == sorted(NEW_NAMES[kind])
    assert wizard_service.verified_wizard_terminal_receipt(row)
    if kind != "characters":
        return
    # 同一作品同一步骤（wizard:{pid}:{kind}）作者与协作者互斥：作者排队中时编辑者 409。
    await app.state.task_engine.stop()
    fake_llm.push(ANSWERS[kind])
    queued = await _start(client, world, kind)
    assert queued.status_code == 202, queued.text
    conflict = await _start(guest, world, kind)
    assert conflict.status_code == 409, conflict.text
    await app.state.task_engine.start()
    assert (await _terminal(app, queued.json()["id"])).status == "succeeded"
    assert await _task_count(app) == 2


async def test_author_retry_pins_original_model(app, client, make_client, fake_llm):
    """作者行为变化：向导改为受控受理后，重试沿用原任务固定的模型（此前重试按当时默认配置重新解析）。"""
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await _start(client, world, "world")
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed"
    await create_ai_config(client, name="新默认", model="model-new", is_default=True)
    fake_llm.push(ANSWERS["world"])
    retried = await client.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    assert (await _terminal(app, retried.json()["id"])).status == "succeeded"
    assert [call["model"] for call in fake_llm.calls] == ["owner-model", "owner-model"]


async def test_author_token_change_mid_send_fails_instead_of_staying_running(app, client, make_client, fake_llm):
    """作者行为变化：发送中作者改密，未决凭据改为系统清理，任务失败并释放资源（此前凭据无法清理、永久 running）。"""
    guest = make_client()
    world = await _world(client, guest)
    before = await _contents(app, world)
    gate = Gate()
    fake_llm.push([gate, ANSWERS["characters"]])
    created = await _start(client, world, "characters")
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == world.owner["id"]).values(
            token_version=User.token_version + 1,
        ))
        await db.commit()
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.result is None and _no_pending(row), row.error
    assert await _contents(app, world) == before
    assert not app.state.task_engine.has_cleanup_failure(world.project["id"])


@pytest.mark.parametrize("change", ["actor_token", "visibility"])
async def test_author_change_after_commit_finalizes_from_receipt(app, client, make_client, fake_llm, monkeypatch,
                                                                 change):
    """作者行为变化：内容已提交、终态前作者改密或 AI 可见性版本变化，由「停在 running、登记清理失败」改为按收据
    记 succeeded（附固定说明，不发布）。"""
    guest = make_client()
    world = await _world(client, guest)

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

    fired = _hook_after_commit(monkeypatch, app, "finalization", bump)
    fake_llm.push(ANSWERS["characters"])
    created = await _start(client, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == ["finalization"]
    assert row.status == "succeeded" and row.error == NOTE and row.user_id == world.owner["id"], row.error
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])
    assert not app.state.task_engine.has_cleanup_failure(world.project["id"])
    assert wizard_service.verified_wizard_terminal_receipt(row)


async def test_author_cancel_after_commit_finalizes_from_receipt(app, client, make_client, fake_llm, monkeypatch):
    """作者行为变化：用户取消恰好落在内容提交之后、done 产出之前——内容已保存不可撤回，按收据记 succeeded（附固定
    说明；此前停在 running、登记清理失败），资源释放；不能记为 cancelled 误导用户内容未改。"""
    guest = make_client()
    world = await _world(client, guest)
    original = wizard_service.save_wizard_result
    fired = []

    async def save_then_cancel(*args, **kwargs):
        payload = await original(*args, **kwargs)
        if not fired:
            fired.append(True)
            async with app.state.sessionmaker() as db:
                task = await db.scalar(select(Task).where(Task.kind == "wizard.characters", Task.status == "running"))

            async def cancel():
                response = await client.post(f"/api/v1/tasks/{task.id}/cancel",
                                             params={"task_key": task.statistics_key})
                assert response.status_code in (200, 202), response.text

            await asyncio.shield(asyncio.ensure_future(cancel()))
            await asyncio.sleep(1)  # 取消在此处送达执行体，done 尚未产出
        return payload

    monkeypatch.setattr(wizard_service, "save_wizard_result", save_then_cancel)
    fake_llm.push(ANSWERS["characters"])
    created = await _start(client, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == [True]
    assert row.status == "succeeded" and row.error == NOTE and row.user_id == world.owner["id"], row.error
    assert await _names(app, world, "characters") == sorted(NEW_NAMES["characters"])
    assert not app.state.task_engine.has_cleanup_failure(world.project["id"])
    await _assert_released(app, client, world, fake_llm)


async def test_author_resume_source_error_still_reported_before_model(app, client, fake_llm):
    """作者报错顺序保持原语义：续写来源不存在先于模型错误报告（锁外无锁预检，受理锁内仍完整重验）；均零任务。"""
    from uuid import uuid4

    from tests.helpers import register

    await register(client, "e4g3c-nomodel")
    project = await create_project(client)
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/characters", json={
        "continue_from_task_id": 99, "continue_from_task_key": str(uuid4()),
    })
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "要继续的向导任务不存在"
    plain = await client.post(f"/api/v1/projects/{project['id']}/wizard/characters", json={})
    assert plain.status_code == 400 and "模型" in plain.json()["detail"], plain.text
    assert await _task_count(app) == 0 and fake_llm.calls == []


async def test_author_direct_build_still_requires_ownership(app, client, make_client):
    """无执行身份的直调（普通 guard_provider 分支）仍是严格作者路径：协作者无法进入。"""
    from app.core.errors import NotFoundError
    from app.schemas.chapter import WizardRequest

    guest = make_client()
    world = await _world(client, guest)
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await wizard_service.build_wizard_job(
                db, user_id=world.guest["id"], project_id=world.project["id"], kind="world", req=WizardRequest(),
                ai_config_id=None, cipher=app.state.cipher, factory=app.state.provider_factory, ai_timeout=30,
                allow_private=True,
            )


def test_agent_cannot_start_wizard_tasks():
    """作者 Agent 不能发起向导任务（不在白名单），协作者 Agent 仍关闭；向导只能经 HTTP 受控受理。"""
    from app.services.agent.tools import tasks as agent_tasks

    for kind in KINDS:
        assert f"wizard.{kind}" not in agent_tasks.ALLOWED_KINDS
        assert not agent_tasks.admission_required(f"wizard.{kind}")
