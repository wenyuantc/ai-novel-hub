"""E6-f：任务侧共享向量收口（裁决 13、17）。

- 裁决 13：作者本人的向导任务也在受理时冻结共享向量选择（``_shared_embedding``，无 grant、来源 actor），
  运行时只经 for_task 守卫发送查询向量并逐次重验；作者默认向量配置本地不可用时仍受理（不冻结，只用关键词，
  保持原行为）；升级前入队、无冻结选择的作者向导任务只用关键词、零向量发送。
- 裁决 17：大纲、实体、向导任务构造作者向量 provider 遇普通故障（DNS / 端点 / provider 构造）时，新事务锁内按
  原执行复核原邀请轮次、执行身份、作者模型授权与冻结选择：都未变 → 关键词继续（零向量发送）；任一变化 → 失败关闭。
"""

import pytest
from sqlalchemy import select, update

from app.ai.providers import AIProviderError
from app.core.errors import BadRequestError
from app.models import AIConfig, ProjectCollaborator, Task
from app.services import ai_config_service, kb_service, project_ai_config, retrieval_service
from app.services.collaboration_ai_guard import EMBEDDING_KEY, CollaborationSendBlocked, SendGuard
from app.services.task_engine.handlers.wizard import WizardHandler
from tests import test_collaboration_entity_entry_e4g3b as entity_tests
from tests import test_collaboration_outline_entry_e4g3 as outline_tests
from tests import test_collaboration_wizard_entry_e4g3c as wizard_tests
from tests.helpers import Gate, create_ai_config

_terminal = entity_tests._terminal


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


# ---------------- 裁决 13：作者向导冻结共享向量选择 ----------------


async def _owner_embedding_config(app, world):
    async with app.state.sessionmaker() as db:
        return await retrieval_service.default_embedding_config(db, world.owner["id"])


async def test_author_wizard_freezes_vector_selection_and_sends_via_guard(app, client, make_client, fake_llm):
    world = await entity_tests._world(client, make_client())
    await wizard_tests._index_owner_kb_in_legacy_space(app, client, world)
    embeds_before = len(fake_llm.embed_calls)
    fake_llm.push(wizard_tests.ANSWERS["world"])
    created = await wizard_tests._start(client, world, "world", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    selection = row.input[EMBEDDING_KEY]
    assert "_collaboration_grant" not in row.input
    assert selection["mode"] == "vector" and selection["model"]["source"] == "actor"
    assert selection["model"]["config_owner_id"] == world.owner["id"]
    embeds = fake_llm.embed_calls[embeds_before:]
    assert [call["model"] for call in embeds] == ["owner-embed"]
    assert "北斗" in str(fake_llm.calls[-1]["messages"])
    assert await wizard_tests._names(app, world, "world") == wizard_tests.NEW_NAMES["world"]


async def test_author_wizard_without_embedding_config_freezes_keyword(app, client, make_client, fake_llm):
    world = await entity_tests._world(client, make_client())
    fake_llm.push(wizard_tests.ANSWERS["characters"])
    created = await wizard_tests._start(client, world, "characters")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.input[EMBEDDING_KEY]["mode"] == "keyword"
    assert row.input[EMBEDDING_KEY]["reason"] == "embedding_config_missing"
    assert fake_llm.embed_calls == []


async def test_author_wizard_with_locally_broken_embedding_config_still_runs_keyword(
    app, client, make_client, fake_llm,
):
    """作者本人向量配置本地不可用：保持原行为（受理成功、检索退回关键词），不冻结选择、零向量发送。"""
    world = await entity_tests._world(client, make_client())
    await wizard_tests._index_owner_kb_in_legacy_space(app, client, world)
    config = await _owner_embedding_config(app, world)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == config.id).values(api_key_enc="not-a-valid-token"))
        await db.commit()
    embeds_before = len(fake_llm.embed_calls)
    fake_llm.push(wizard_tests.ANSWERS["world"])
    created = await wizard_tests._start(client, world, "world", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert EMBEDDING_KEY not in row.input
    assert fake_llm.embed_calls[embeds_before:] == []
    assert "北斗" in str(fake_llm.calls[-1]["messages"])


async def test_pre_upgrade_author_wizard_without_selection_runs_keyword_only(
    app, client, make_client, fake_llm, monkeypatch,
):
    """升级前入队的作者向导任务（输入没有冻结选择）：即使作者向量配置可用，也只用关键词、零向量发送。"""
    world = await entity_tests._world(client, make_client())
    await wizard_tests._index_owner_kb_in_legacy_space(app, client, world)
    original = WizardHandler._claim

    async def legacy_claim(self, *args, **kwargs):
        spec = await original(self, *args, **kwargs)
        spec.input.pop(EMBEDDING_KEY, None)
        return spec

    monkeypatch.setattr(WizardHandler, "_claim", legacy_claim)
    embeds_before = len(fake_llm.embed_calls)
    fake_llm.push(wizard_tests.ANSWERS["world"])
    created = await wizard_tests._start(client, world, "world", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert EMBEDDING_KEY not in row.input
    assert fake_llm.embed_calls[embeds_before:] == []
    assert "北斗" in str(fake_llm.calls[-1]["messages"])


@pytest.mark.parametrize("change", ["model", "default"])
async def test_author_wizard_vector_send_rechecks_frozen_selection(app, client, make_client, fake_llm, change):
    """作者向导的查询向量在真实发送租约内重验冻结选择：发送前作者改了向量配置或换了默认向量，零向量、零文本、
    内容不变（作者旧路径按运行时默认解析，不会发现此变化——负对照见 implement.md）。"""
    world = await entity_tests._world(client, make_client())
    await wizard_tests._index_owner_kb_in_legacy_space(app, client, world)
    config = await _owner_embedding_config(app, world)
    before = await wizard_tests._names(app, world, "world")
    embeds_before, calls_before = len(fake_llm.embed_calls), len(fake_llm.calls)
    gate = Gate()
    fake_llm.embed_before_gates = [gate]
    fake_llm.push(wizard_tests.ANSWERS["world"])
    created = await wizard_tests._start(client, world, "world", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    if change == "model":
        async with app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == config.id).values(model="owner-embed-v2"))
            await db.commit()
    else:
        replacement = await create_ai_config(client, name="作者新向量", model="owner-embed-new",
                                             model_type="embedding")
        assert (await client.post(f"/api/v1/ai-configs/{replacement['id']}/default")).status_code == 200
    gate.release()
    row = await wizard_tests._settled(app, created.json()["id"])
    assert row.status in ("failed", "cancelled"), row.status
    assert fake_llm.embed_calls[embeds_before:] == [] and len(fake_llm.calls) == calls_before
    assert await wizard_tests._names(app, world, "world") == before
    assert wizard_tests._no_pending(row)


async def test_author_wizard_retry_refreezes_current_selection(app, client, make_client, fake_llm):
    """裁决 15 不变：作者重试按当前授权 / 默认配置重新冻结（作者无向量 → 有向量）。"""
    world = await entity_tests._world(client, make_client())
    fake_llm.push(AIProviderError("模型调用失败：首次失败"))
    created = await wizard_tests._start(client, world, "world")
    assert created.status_code == 202, created.text
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed" and first.input[EMBEDDING_KEY]["mode"] == "keyword"
    await wizard_tests._index_owner_kb_in_legacy_space(app, client, world)
    fake_llm.push(wizard_tests.ANSWERS["world"])
    retried = await client.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    row = await _terminal(app, retried.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.input[EMBEDDING_KEY]["mode"] == "vector"


# ---------------- 裁决 17：构造 provider 普通故障 ----------------


def _fail_embedding_build(monkeypatch):
    original = ai_config_service.build_provider

    async def failing(cfg, *args, **kwargs):
        if cfg.model_type == "embedding":
            raise BadRequestError("端点 owner-secret.example 无法解析")
        return await original(cfg, *args, **kwargs)

    monkeypatch.setattr(ai_config_service, "build_provider", failing)


async def _prepare_world(app, client, make_client, task_kind, *, author):
    """授权编辑者（或作者本人）+ 作者旧键空间已有可召回向量 + 作者向量配置可用。返回 (world, starter, kind)。"""
    guest = make_client()
    if task_kind == "outline":
        world = await outline_tests._world(client, guest, allow_owner_ai=True)
        await create_ai_config(client, name="作者向量", model="owner-embed", model_type="embedding",
                               api_key=outline_tests.OWNER_EMBED_KEY)
        await outline_tests._index_owner_memory_in_author_space(app, world)
        kind, answer = "continue", outline_tests._answer("continue")

        def start(c):
            return outline_tests._start(c, world, kind)
    elif task_kind == "entity":
        world = await entity_tests._world(client, guest, allow_owner_ai=True)
        await entity_tests._index_owner_kb_in_author_space(app, client, world)
        kind, answer = "character.generate", entity_tests._answer("character.generate")

        def start(c):
            return entity_tests._start(c, world, kind, instruction="天剑宗师承")
    else:
        world = await entity_tests._world(client, guest, allow_owner_ai=True)
        await wizard_tests._index_owner_kb_in_legacy_space(app, client, world)
        kind, answer = "world", wizard_tests.ANSWERS["world"]

        def start(c):
            return wizard_tests._start(c, world, kind, instruction="天剑宗师承")
    return world, (client if author else guest), start, answer


@pytest.mark.parametrize("author", [False, True], ids=["editor", "author"])
@pytest.mark.parametrize("task_kind", ["outline", "entity", "wizard"])
async def test_embedding_prepare_failure_degrades_to_keyword_when_unchanged(
    app, client, make_client, fake_llm, monkeypatch, task_kind, author,
):
    world, actor, start, answer = await _prepare_world(app, client, make_client, task_kind, author=author)
    _fail_embedding_build(monkeypatch)
    embeds_before = len(fake_llm.embed_calls)
    fake_llm.push(answer)
    created = await start(actor)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.input[EMBEDDING_KEY]["mode"] == "vector"
    assert fake_llm.embed_calls[embeds_before:] == []
    assert len(fake_llm.calls) == 1
    sent = str(fake_llm.calls[0]["messages"])
    assert ("作者旧情节向量记忆" if task_kind == "outline" else "北斗") in sent
    assert "owner-secret" not in (row.error or "")


async def _direct_change(app, world, change):
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ))
        if change == "owner_ai_direct":
            member.allow_owner_ai = False
        else:
            member.status, member.revision = "revoked", member.revision + 1
        await db.commit()


@pytest.mark.parametrize("change", ["owner_ai_direct", "revoked_direct"])
@pytest.mark.parametrize("task_kind", ["outline", "entity", "wizard"])
async def test_embedding_prepare_failure_with_grant_change_fails_closed(
    app, client, make_client, fake_llm, monkeypatch, task_kind, change,
):
    """构造失败期间授权已变化：不能当作普通故障降级，任务失败关闭，零向量、零文本发送。"""
    world, actor, start, answer = await _prepare_world(app, client, make_client, task_kind, author=False)
    original = SendGuard.prepare_provider

    async def change_then_fail(guard, *args, **kwargs):
        if type(guard._binding) is project_ai_config.ResolvedSharedEmbedding:
            await _direct_change(app, world, change)
            raise CollaborationSendBlocked()
        return await original(guard, *args, **kwargs)

    monkeypatch.setattr(SendGuard, "prepare_provider", change_then_fail)
    # 锁内复核在构造失败处即失败关闭：不能先降级去做关键词检索、再靠后续文本发送检查兜底。
    retrieved = []
    for name in ("knowledge_text", "search_hits"):
        original_fn = getattr(kb_service, name)

        async def recording(*args, _fn=original_fn, _name=name, **kwargs):
            retrieved.append(_name)
            return await _fn(*args, **kwargs)

        monkeypatch.setattr(kb_service, name, recording)
    before = await wizard_tests._names(app, world, "world") if task_kind == "wizard" else None
    embeds_before = len(fake_llm.embed_calls)
    fake_llm.push(answer)
    created = await start(actor)
    assert created.status_code == 202, created.text
    row = await (wizard_tests._settled(app, created.json()["id"]) if task_kind == "wizard"
                 else _terminal(app, created.json()["id"]))
    assert row.status in ("failed", "cancelled"), row.status
    assert fake_llm.embed_calls[embeds_before:] == [] and fake_llm.calls == []
    assert retrieved == []
    assert not row.result
    if task_kind == "wizard":
        assert await wizard_tests._names(app, world, "world") == before
        assert wizard_tests._no_pending(row)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, row.id)).status in ("failed", "cancelled")


@pytest.mark.parametrize("task_kind", ["outline", "entity", "wizard"])
async def test_author_embedding_prepare_failure_with_changed_frozen_config_fails_closed(
    app, client, make_client, fake_llm, monkeypatch, task_kind,
):
    """作者任务（无 grant）同样不能把构造失败期间的冻结选择变化当普通故障降级：锁内复核冻结的向量配置指纹，
    作者在此期间改了向量配置 → 失败关闭，零向量、零文本发送，且未做关键词检索（复核 E6-f 补充）。"""
    world, actor, start, answer = await _prepare_world(app, client, make_client, task_kind, author=True)
    original = SendGuard.prepare_provider

    async def change_then_fail(guard, *args, **kwargs):
        if type(guard._binding) is project_ai_config.ResolvedSharedEmbedding:
            assert guard._binding.grant_identity is None
            async with app.state.sessionmaker() as db:
                await db.execute(update(AIConfig).where(
                    AIConfig.id == guard._binding.model.config_id, AIConfig.user_id == world.owner["id"],
                ).values(model="owner-embed-v2"))
                await db.commit()
            raise CollaborationSendBlocked()
        return await original(guard, *args, **kwargs)

    monkeypatch.setattr(SendGuard, "prepare_provider", change_then_fail)
    retrieved = []
    for name in ("knowledge_text", "search_hits"):
        original_fn = getattr(kb_service, name)

        async def recording(*args, _fn=original_fn, _name=name, **kwargs):
            retrieved.append(_name)
            return await _fn(*args, **kwargs)

        monkeypatch.setattr(kb_service, name, recording)
    before = await wizard_tests._names(app, world, "world") if task_kind == "wizard" else None
    embeds_before = len(fake_llm.embed_calls)
    fake_llm.push(answer)
    created = await start(actor)
    assert created.status_code == 202, created.text
    row = await (wizard_tests._settled(app, created.json()["id"]) if task_kind == "wizard"
                 else _terminal(app, created.json()["id"]))
    assert "_collaboration_grant" not in row.input and row.input[EMBEDDING_KEY]["mode"] == "vector"
    assert row.status in ("failed", "cancelled"), row.status
    assert fake_llm.embed_calls[embeds_before:] == [] and fake_llm.calls == []
    assert retrieved == []
    assert not row.result
    if task_kind == "wizard":
        assert await wizard_tests._names(app, world, "world") == before
        assert wizard_tests._no_pending(row)


async def test_author_wizard_resume_vector_query_rechecks_resume_source(app, client, fake_llm, monkeypatch):
    """作者续写的查询向量经带续写来源的 for_task 守卫发送：向量 provider 构造后来源被改，查询向量零发送。"""
    from app.models import KbChunk, KbDocument, RetrievalChunk
    from app.services.ai_dispatch import VisibilityPolicyError
    from tests.helpers import fake_vector
    from tests.test_wizard_resume_identity import _running, _source

    _, project, source = await _source(app, client)
    config = await create_ai_config(client, name="向量模型", model_type="embedding", model="embedding-test")
    async with app.state.sessionmaker() as db:
        doc = KbDocument(project_id=project["id"], filename="资料.txt", sha256="1" * 64, text="天剑宗资料",
                         status="ready", chunk_count=1)
        db.add(doc)
        await db.flush()
        chunk = KbChunk(document_id=doc.id, seq=1, heading_path="天剑宗", content="天剑宗位于北境")
        db.add(chunk)
        await db.flush()
        await retrieval_service.upsert_many(db, project["id"], "kb", [
            retrieval_service.ChunkInput(ref_id=chunk.id, text="天剑宗\n天剑宗位于北境"),
        ])
        cfg = await db.get(AIConfig, config["id"])
        vector = retrieval_service.normalize_vector(fake_vector("天剑宗"))
        await db.execute(update(RetrievalChunk).where(RetrievalChunk.project_id == project["id"]).values(
            embedding=retrieval_service.encode_vector(vector),
            embedding_key=retrieval_service.make_embedding_key(retrieval_service.config_model_key(cfg), len(vector)),
        ))
        await db.commit()
    retrieval_service.invalidate(project["id"])
    app.state.task_engine.runtime.settings_confirmed = False
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/outline", json={
        "continue_from_task_id": source["id"], "continue_from_task_key": source["task_key"], "instruction": "天剑宗",
    })
    assert response.status_code == 202, response.text
    original = SendGuard.prepare_provider

    async def change_after_build(guard, *args, **kwargs):
        bound = await original(guard, *args, **kwargs)
        if type(guard._binding) is project_ai_config.ResolvedSharedEmbedding:
            async with app.state.sessionmaker() as writer:
                (await writer.get(Task, source["id"])).output_text = '{"volumes":[{"title":"已变更"}'
                await writer.commit()
        return bound

    monkeypatch.setattr(SendGuard, "prepare_provider", change_after_build)
    task, execution = await _running(app, response.json()["id"])
    assert task.input[EMBEDDING_KEY]["mode"] == "vector"
    with pytest.raises(VisibilityPolicyError):
        await WizardHandler().build_job(app.state.task_engine.ctx, task, execution=execution)
    assert fake_llm.embed_calls == [] and fake_llm.calls == []
