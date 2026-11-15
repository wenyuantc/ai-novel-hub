"""E4-g1：accepted editor 的章节分析入口（chapter.analyze）。

覆盖：手动分析 `/chapters/{id}/analyze` 与一键分析 `/projects/{pid}/analysis/analyze-pending` 经受控受理入队，
Task 归真实 actor、按本人优先 → 仅无配置时按授权回退作者模型并记录费用来源；viewer/非成员/待接受/已撤权/
仅管理员身份不能触发；受理后领取前、领取后 build 前/发送前、发送后写回前撤权/降级/关闭作者模型授权失败关闭，
不写分析、设定回写、伏笔或剧情记忆；已提交结果不因随后撤权回滚；作者私人风格与提示词模板不进入协作者请求；
共享向量未开放（E6）：分析派生的记忆只维护关键词单元，不调用作者向量模型、不建索引任务。
单章生成成功后的自动分析向真实触发者派生；无法证明或已撤权时不派生且不影响章节成功。

协作者 SSE 自 E5-f 起经受控管线开放（见 test_collaboration_stream_*、test_task_sse_scope），
本文件经任务详情或数据库观察终态。
"""

import asyncio

import pytest
from sqlalchemy import func, select, update

from app.models import (
    AnalysisChange,
    AnalysisIndexReceipt,
    ChapterAnalysis,
    Foreshadow,
    Project,
    ProjectCollaborator,
    RetrievalChunk,
    StoryMemory,
    Task,
    User,
    WritingStyle,
)
from app.services import analysis_service
from app.services.task_engine import engine as engine_module
from app.services.task_engine.handlers.chapter import ChapterHandler
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, analysis_json, create_ai_config
from tests.test_collaboration_chapter_entry_e4f import (
    EDITOR_KEY,
    OWNER_KEY,
    _change,
    _member,
    _task_count,
    _terminal,
    _world,
)

MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES
SAFE_STOP = {
    MESSAGES["collaboration_revoked"], "任务输出授权已变化，已停止", "权限或模型授权已变化，已停止发送",
    "分析任务执行身份或授权已变化，已停止",
}
FOLLOWUP_WARNING = "正文已保存，自动分析状态未确认，请查看分析任务"


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": True}


async def _analysis_rows(app):
    async with app.state.sessionmaker() as db:
        return {model.__name__: await db.scalar(select(func.count()).select_from(model))
                for model in (ChapterAnalysis, AnalysisChange, StoryMemory, Foreshadow)}


async def _assert_no_analysis_writes(app):
    assert await _analysis_rows(app) == {"ChapterAnalysis": 0, "AnalysisChange": 0, "StoryMemory": 0,
                                         "Foreshadow": 0}


async def _analyze(guest, world, **body):
    return await guest.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json=body)


async def _kb_tasks(app):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count(Task.id)).where(Task.kind == "kb.index"))


@pytest.mark.parametrize("owner_fallback", [False, True])
async def test_editor_manual_analysis_runs_as_real_actor_and_writes_back(
    app, client, make_client, fake_llm, owner_fallback,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_fallback)
    if owner_fallback:
        # 作者已配置向量模型且授权：E6-c 起协作者分析派生的记忆向量任务归协作者、使用作者向量（费用来源=作者）。
        await create_ai_config(client, name="作者向量", model_type="embedding", model="owner-embed")
    fake_llm.push(analysis_json(summary="协作者分析的概要"))
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    task = created.json()
    detail = await guest.get(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
    assert detail.status_code == 200, detail.text
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert (row.user_id, row.project_id, row.kind) == (world.guest["id"], world.project["id"], "chapter.analyze")
    member = await _member(app, world)
    assert row.input["_collaboration_grant"] == {"invitation_id": member.invitation_id, "revision": member.revision}
    model = row.input["_project_model"]
    # 费用来源：本人有配置用本人密钥；无本人配置且已授权时实际使用作者密钥，触发者仍为协作者。
    expected_key, source, config_owner = (
        (OWNER_KEY, "owner", world.owner["id"]) if owner_fallback else (EDITOR_KEY, "actor", world.guest["id"])
    )
    assert (model["source"], model["config_owner_id"], model["actor_id"]) == (source, config_owner, world.guest["id"])
    assert [call["api_key"] for call in fake_llm.calls] == [expected_key]
    if owner_fallback:
        # E6-c 翻转（原断言：零向量调用、零 kb.index）：收据式派生子任务归真实触发者并携带原邀请轮次，
        # 只向量化本次分析写入的记忆，向量使用作者配置。
        async with app.state.sessionmaker() as db:
            child = await db.scalar(select(Task).where(Task.kind == "kb.index"))
        assert child is not None and child.user_id == world.guest["id"]
        assert child.input["_collaboration_grant"] == row.input["_collaboration_grant"]
        assert child.input["_shared_embedding"]["model"]["config_owner_id"] == world.owner["id"]
        child = await _terminal(app, child.id)
        assert child.status == "succeeded", child.error
        assert {call["model"] for call in fake_llm.embed_calls} == {"owner-embed"}
        async with app.state.sessionmaker() as db:
            receipt = await db.scalar(select(AnalysisIndexReceipt))
            assert (receipt.actor_id, receipt.grant_invitation_id, receipt.grant_revision) == (
                world.guest["id"], member.invitation_id, member.revision,
            )
            keys = set(await db.scalars(select(RetrievalChunk.embedding_key).where(
                RetrievalChunk.source == "memory", RetrievalChunk.project_id == world.project["id"],
            )))
            assert keys == {"openai_compat:owner-embed:64"}
    else:
        assert fake_llm.embed_calls == [] and await _kb_tasks(app) == 0
    async with app.state.sessionmaker() as db:
        analysis = await db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.task_id == row.id))
        assert analysis is not None and analysis.summary == "协作者分析的概要"
        assert analysis.project_id == world.project["id"] and analysis.chapter_id == world.chapter["id"]
        # 设定回写与剧情记忆在同一写事务内落库（记忆只有关键词检索单元）。
        assert await db.scalar(select(func.count(StoryMemory.id)).where(
            StoryMemory.chapter_id == world.chapter["id"])) >= 1
    expected_warning = (analysis_service.ANALYSIS_INDEX_PENDING_WARNING if owner_fallback
                        else analysis_service.ANALYSIS_KEYWORD_WARNING)
    assert expected_warning in row.result["warnings"]
    # 作者看到同一份分析结果（作品级共享内容），但看不到协作者的任务。
    owner_view = await client.get(f"/api/v1/analyses/{row.result['analysis_id']}")
    assert owner_view.status_code == 200 and owner_view.json()["task_id"] is None
    # 已提交的合法结果不因随后撤权回滚。
    await _change(app, client, world, "revoked")
    async with app.state.sessionmaker() as db:
        assert await db.get(ChapterAnalysis, row.result["analysis_id"]) is not None


async def test_editor_without_own_model_or_grant_and_invalid_own_model_are_rejected(
    app, client, make_client, fake_llm,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=False, editor_model=False)
    response = await _analyze(guest, world)
    assert response.status_code == 400, response.text
    # 本人配置无效时不静默回退作者配置（即使已授权）。
    third = make_client()
    other = await _world_second_guest(app, client, third, world)
    async with app.state.sessionmaker() as db:
        from app.models import AIConfig

        await db.execute(update(AIConfig).where(AIConfig.id == other["config"]["id"]).values(
            api_key_enc="not-a-valid-token",
        ))
        await db.commit()
    response = await third.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert response.status_code == 400, response.text
    # 不能显式选择作者的配置。
    chosen = await third.post(f"/api/v1/chapters/{world.chapter['id']}/analyze",
                              json={"ai_config_id": world.owner_config["id"]})
    assert chosen.status_code in (400, 404), chosen.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    await _assert_no_analysis_writes(app)


async def _world_second_guest(app, owner, client, world):
    from tests.helpers import register

    user = await register(client, "e4g-third")
    config = await create_ai_config(client, name="第三人模型", model="third-model", api_key="sk-third-0003")
    invite = await owner.post(f"/api/v1/projects/{world.project['id']}/collaborators", json={
        "username": "e4g-third", "role": "editor", "allow_owner_ai": True,
    })
    assert invite.status_code == 201, invite.text
    accepted = await client.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return {"user": user, "config": config}


@pytest.mark.parametrize("case", ["viewer", "pending", "outsider", "revoked", "admin_outsider", "admin_viewer"])
async def test_non_editors_cannot_start_analysis(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(
        client, guest, role="viewer" if case in ("viewer", "admin_viewer") else "editor",
        accept=case not in ("pending", "outsider", "admin_outsider"), allow_owner_ai=True,
    )
    if case in ("outsider", "admin_outsider"):
        member = await _member(app, world)
        response = await client.delete(
            f"/api/v1/projects/{world.project['id']}/collaborators/{world.guest['id']}",
            params={"expected_revision": member.revision},
        )
        assert response.status_code == 204, response.text
    if case == "revoked":
        await _change(app, client, world, "revoked")
    if case.startswith("admin"):
        # 管理员人工读取/编辑权限不能旁路协作 AI。
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(is_admin=True))
            await db.commit()
    expected = 403 if case in ("viewer", "admin_viewer") else 404
    manual = await _analyze(guest, world)
    assert manual.status_code == expected, manual.text
    pending = await guest.post(f"/api/v1/projects/{world.project['id']}/analysis/analyze-pending", json={})
    assert pending.status_code == expected, pending.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    await _assert_no_analysis_writes(app)


async def test_editor_analyze_pending_and_dedup_are_personal(app, client, make_client, fake_llm):
    await app.state.task_engine.stop()
    guest = make_client()
    world = await _world(client, guest)
    second = await client.post(f"/api/v1/projects/{world.project['id']}/chapters",
                               json={"title": "第二章", "content": "第二章正文"})
    assert second.status_code == 201, second.text
    empty = await client.post(f"/api/v1/projects/{world.project['id']}/chapters", json={"title": "空章"})
    assert empty.status_code == 201, empty.text
    response = await guest.post(f"/api/v1/projects/{world.project['id']}/analysis/analyze-pending", json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["queued"] == 2
    member = await _member(app, world)
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(Task).where(Task.id.in_(result["task_ids"])).order_by(Task.id)))
        assert [row.chapter_id for row in rows] == [world.chapter["id"], second.json()["id"]]
        for row in rows:
            assert row.user_id == world.guest["id"] and row.status == "queued"
            assert row.input["_collaboration_grant"]["revision"] == member.revision
    # 再次点击：本人同轮次已排队 → 不重复；单章手动请求返回已有任务（200）。
    again = await guest.post(f"/api/v1/projects/{world.project['id']}/analysis/analyze-pending", json={})
    assert again.json()["queued"] == 0
    same = await _analyze(guest, world)
    assert same.status_code == 200 and same.json()["id"] == result["task_ids"][0]
    # 去重只看本人：作者的请求另建作者自己的任务，不返回协作者的私人任务。
    owner_task = await client.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert owner_task.status_code == 202, owner_task.text
    assert owner_task.json()["id"] not in result["task_ids"]
    async with app.state.sessionmaker() as db:
        owner_row = await db.get(Task, owner_task.json()["id"])
        assert owner_row.user_id == world.owner["id"] and "_collaboration_grant" not in owner_row.input
    assert fake_llm.calls == []


async def test_editor_analysis_uses_actor_template_and_hides_owner_private_style(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    detail = (await client.get("/api/v1/prompt-templates/analysis.chapter")).json()
    saved = await client.put("/api/v1/prompt-templates/analysis.chapter", json={
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
    fake_llm.push(analysis_json())
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    sent = str(fake_llm.calls[0]["messages"])
    assert fake_llm.calls[0]["api_key"] == EDITOR_KEY
    for secret in ("作者私有模板标记壬癸", "私有描述甲乙丙", "作者私有要求丁戊己", "作者私有风格"):
        assert secret not in sent
    # 作者自己的分析仍使用作者模板（正对照）。
    fake_llm.push(analysis_json())
    mine = await client.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert mine.status_code == 202, mine.text
    assert (await _terminal(app, mine.json()["id"])).status == "succeeded"
    assert "作者私有模板标记壬癸" in str(fake_llm.calls[1]["messages"])


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "disabled"])
async def test_revoke_after_admission_before_claim_fails_closed(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    await _change(app, client, world, change)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    expected = "collaboration_account_unavailable" if change == "disabled" else "collaboration_revoked"
    assert row.status == "failed" and row.error == MESSAGES[expected]
    assert fake_llm.calls == []
    await _assert_no_analysis_writes(app)


@pytest.mark.parametrize("stage", ["build", "send"])
@pytest.mark.parametrize("change", ["revoked", "viewer"])
async def test_revoke_after_claim_before_send_never_sends(app, client, make_client, fake_llm, monkeypatch,
                                                           stage, change):
    guest = make_client()
    world = await _world(client, guest)
    handler = get_handler("chapter.analyze")
    original_build = type(handler).build_job
    original_job = analysis_service.build_analysis_job
    fired = asyncio.Event()
    changes = []

    async def revoke_elsewhere():
        # 与 E4-f 相同：撤权请求放在独立任务，避免撤权通知取消执行体时打断撤权请求自身的收尾。
        change_task = asyncio.ensure_future(_change(app, client, world, change))
        changes.append(change_task)
        await asyncio.shield(change_task)

    async def revoke_before_build(self, ctx, task, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke_elsewhere()
        return await original_build(self, ctx, task, **kwargs)

    async def revoke_after_build(*args, **kwargs):
        job = await original_job(*args, **kwargs)
        if not fired.is_set():
            fired.set()
            await revoke_elsewhere()
        return job

    if stage == "build":
        monkeypatch.setattr(type(handler), "build_job", revoke_before_build)
    else:
        monkeypatch.setattr(analysis_service, "build_analysis_job", revoke_after_build)
    fake_llm.push(analysis_json())
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired.is_set()
    await asyncio.gather(*changes)
    assert row.status == "failed" and row.output_text == "" and row.result is None, row.error
    assert not (row.checkpoint or {}).get("_analysis_pending") and not (row.checkpoint or {}).get(
        "_analysis_completion")
    assert fake_llm.calls == []
    await _assert_no_analysis_writes(app)


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_revoke_during_stream_never_publishes_or_writes(app, client, make_client, fake_llm, monkeypatch,
                                                              change, notify):
    """流式输出中撤权：本进程通知取消执行体，或（notify=False，跨实例）下一帧发布前锁内核验拒绝。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    raw = analysis_json(summary="撤权后不能写回的分析")
    gate = Gate()
    fake_llm.push([raw[:20], gate, raw[20:]])
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _change(app, client, world, change)
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.error in SAFE_STOP, row.error
    assert row.result is None and "撤权后不能写回" not in (row.output_text or "")
    assert not (row.checkpoint or {}).get("_analysis_pending") and not (row.checkpoint or {}).get(
        "_analysis_completion")
    assert len(fake_llm.calls) == 1
    await _assert_no_analysis_writes(app)


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "disabled"])
async def test_revoke_after_stream_before_writeback_never_writes(app, client, make_client, fake_llm, monkeypatch,
                                                                 change, notify):
    """模型输出已全部产出、写回事务尚未取锁时授权变化：写回锁内按原 grant/模型来源重验失败，不写任何结果。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    original = analysis_service._analysis_write_barrier
    fired = []

    async def change_then_lock(db, execution, *, status="running"):
        # 首次调用在 begin_analysis_pending（发送前）；模型调用之后的第一次才是写回事务的屏障。
        if status == "running" and fake_llm.calls and not fired:
            work = asyncio.ensure_future(_change(app, client, world, change))
            fired.append(work)
            await asyncio.shield(work)
        return await original(db, execution, status=status)

    monkeypatch.setattr(analysis_service, "_analysis_write_barrier", change_then_lock)
    fake_llm.push(analysis_json(summary="写回前撤权的分析"))
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*fired)
    assert fired, "未到达写回屏障"
    if change == "disabled":
        # 账号停用后收尾无法再以原身份认证：只做无正文系统收尾（固定文案），或由本进程复查先行失败。
        expected = {MESSAGES["collaboration_account_unavailable"], "任务执行已停止"}
    elif notify:
        expected = SAFE_STOP
    else:
        # 无本进程通知：只能是写回事务锁内重验失败（受控 code → 固定文案）。
        expected = {"分析任务执行身份或授权已变化，已停止"}
    assert row.status == "failed" and row.error in expected, row.error
    assert row.result is None
    assert not (row.checkpoint or {}).get("_analysis_pending") and not (row.checkpoint or {}).get(
        "_analysis_completion")
    assert len(fake_llm.calls) == 1
    await _assert_no_analysis_writes(app)


async def test_editor_retry_keeps_round_and_reinvite_blocks_old_retry(app, client, make_client, fake_llm):
    from app.ai.providers import AIProviderError

    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await _analyze(guest, world)
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed"
    fake_llm.push(analysis_json())
    retried = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    second = await _terminal(app, retried.json()["id"])
    assert second.status == "succeeded", second.error
    assert second.retry_of_id == first.id and second.user_id == world.guest["id"]
    assert second.input["_collaboration_grant"] == first.input["_collaboration_grant"]
    await _change(app, client, world, "revoked")
    invite = await client.post(f"/api/v1/projects/{world.project['id']}/collaborators", json={
        "username": "e4f-guest", "role": "editor",
    })
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    calls = len(fake_llm.calls)
    stale = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    # 旧轮次任务不能借新邀请重试（预检/锁内重验拒绝），不建任务、不发送。
    assert stale.status_code in (400, 404, 409), stale.text
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == first.id)) == 1


async def test_author_analysis_unchanged_alongside_editor(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(analysis_json())
    created = await client.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded" and row.user_id == world.owner["id"], row.error
    assert "_collaboration_grant" not in row.input and row.input["_project_model"]["source"] == "actor"
    assert fake_llm.calls[0]["api_key"] == OWNER_KEY


# ---------------- 自动分析派生 ----------------


@pytest.mark.parametrize("owner_fallback", [False, True])
async def test_editor_chapter_success_derives_analysis_for_real_actor(
    app, client, make_client, fake_llm, owner_fallback,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_fallback)
    fake_llm.push("协作者生成的新正文" * 4)
    fake_llm.push(analysis_json(summary="派生分析"))
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    chapter_row = await _terminal(app, created.json()["id"])
    assert chapter_row.status == "succeeded" and not chapter_row.error, chapter_row.error
    async with app.state.sessionmaker() as db:
        child = await db.scalar(select(Task).where(Task.kind == "chapter.analyze"))
        assert child is not None
    child = await _terminal(app, child.id, within=15)
    assert child.status == "succeeded", child.error
    # 子任务归真实触发者，继承原令牌、模型绑定（含费用来源）与授权轮次；不改记作者任务。
    assert child.user_id == world.guest["id"] and child.input["auto"] is True
    assert child.input["_collaboration_grant"] == chapter_row.input["_collaboration_grant"]
    assert child.input["_project_model"] == chapter_row.input["_project_model"]
    assert child.input["_project_model"]["source"] == ("owner" if owner_fallback else "actor")
    assert child.input["_task_actor_token_version"] == chapter_row.input["_task_actor_token_version"]
    key = OWNER_KEY if owner_fallback else EDITOR_KEY
    assert [call["api_key"] for call in fake_llm.calls] == [key, key]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(
            Task.kind == "chapter.analyze", Task.user_id == world.owner["id"])) == 0
        analysis = await db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.task_id == child.id))
        assert analysis is not None and analysis.summary == "派生分析"
    assert await _kb_tasks(app) == 0


async def test_project_auto_analyze_off_does_not_derive(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    assert (await client.patch(f"/api/v1/projects/{world.project['id']}",
                               json={"auto_analyze": False})).status_code == 200
    fake_llm.push("不派生分析的正文" * 3)
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded" and not row.error, row.error
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 0


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_change_before_derivation_skips_analysis_without_failing_chapter(
    app, client, make_client, fake_llm, monkeypatch, change, notify,
):
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    original = ChapterHandler.on_success
    fired = []

    async def change_then_derive(self, ctx, task, *, execution):
        if not fired:
            work = asyncio.ensure_future(_change(app, client, world, change))
            fired.append(work)
            await asyncio.shield(work)
        return await original(self, ctx, task, execution=execution)

    monkeypatch.setattr(ChapterHandler, "on_success", change_then_derive)
    fake_llm.push("派生前撤权的正文" * 3)
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*fired)
    # 章节已成功提交且保留；派生无法在锁内证明当前授权 → 不入队，只留固定提示。
    assert row.status == "succeeded" and row.error == FOLLOWUP_WARNING, row.error
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 0
        from app.models import Chapter

        assert (await db.get(Chapter, world.chapter["id"])).content == "派生前撤权的正文" * 3
    assert len(fake_llm.calls) == 1


async def test_reinvited_editor_cannot_derive_from_old_round(app, client, make_client, fake_llm, monkeypatch):
    """原执行的 grant 已被新一轮邀请取代：不能借新轮次补签派生任务。"""
    await app.state.task_engine.stop()
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push("旧轮次生成的正文" * 3)
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    original = ChapterHandler.on_success
    fired = []

    async def reinvite_then_derive(self, ctx, task, *, execution):
        async def reinvite():
            await _change(app, client, world, "revoked")
            invite = await client.post(f"/api/v1/projects/{world.project['id']}/collaborators", json={
                "username": "e4f-guest", "role": "editor",
            })
            await guest.post(
                f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
                json={"expected_revision": invite.json()["revision"]},
            )
        work = asyncio.ensure_future(reinvite())
        fired.append(work)
        await asyncio.shield(work)
        return await original(self, ctx, task, execution=execution)

    monkeypatch.setattr(ChapterHandler, "on_success", reinvite_then_derive)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*fired)
    assert row.status == "succeeded" and row.error == FOLLOWUP_WARNING, row.error
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 0
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"]))
        assert member.status == "accepted"


# ---------------- 写回已提交、done 核验前授权变化：凭完成收据受限收尾 ----------------

ANALYSIS_NOTE = engine_module.ANALYSIS_RECEIPT_FINALIZED_NOTE


def _change_before_done(app, monkeypatch, change):
    engine = app.state.task_engine
    original = type(engine)._event_task
    fired, changes = [], []

    async def wrapped(self, db, execution, current, event):
        if event == "done" and not fired:
            fired.append(event)
            work = asyncio.ensure_future(change())
            changes.append(work)
            await asyncio.shield(work)
        return await original(self, db, execution, current, event)

    monkeypatch.setattr(type(engine), "_event_task", wrapped)
    return fired, changes


async def _bump_token(app, user_id):
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1))
        await db.commit()


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "author_token"])
async def test_change_between_writeback_commit_and_done_completes_from_receipt(
    app, client, make_client, fake_llm, monkeypatch, change, notify,
):
    """写回事务已提交、done 核验前授权变化：不回滚已提交结果，也不能永久占用 analysis 串行键。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    starter = client if change == "author_token" else guest

    async def do_change():
        if change == "author_token":
            await _bump_token(app, world.owner["id"])
        else:
            await _change(app, client, world, change)

    fired, changes = _change_before_done(app, monkeypatch, do_change)
    fake_llm.push(analysis_json(summary="提交后才变化的分析"))
    created = await starter.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired == ["done"]
    assert row.status == "succeeded" and row.execution_key is None and row.error == ANALYSIS_NOTE, row.error
    assert row.checkpoint.get("_analysis_completion") and "_analysis_pending" not in row.checkpoint
    async with app.state.sessionmaker() as db:
        analysis = await db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.task_id == row.id))
        assert analysis is not None and analysis.summary == "提交后才变化的分析"
        # 结果由收据逐字重建（终态删除/裁剪证明要求一致）。
        assert row.result["analysis_id"] == analysis.id
        assert await analysis_service.completed_terminal_analysis_receipt(db, row) is not None
        # 收尾不派生记忆索引等后续任务。
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "kb.index")) == 0
    engine = app.state.task_engine
    assert not engine.has_cleanup_failure(world.project["id"]) and row.id not in engine.hubs
    assert len(fake_llm.calls) == 1
    # 串行键已释放：仍有权的用户可立即再次分析并成功（作者改密后其旧会话已失效，改由编辑者发起）。
    follower = guest if change == "author_token" else client
    fake_llm.push(analysis_json(summary="随后的分析"))
    again = await follower.post(f"/api/v1/chapters/{world.chapter['id']}/analyze", json={})
    assert again.status_code == 202, again.text
    assert (await _terminal(app, again.json()["id"])).status == "succeeded"


async def test_analysis_receipt_completion_rejects_tampered_receipt(app, client, make_client, fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest)

    async def tamper_then_revoke():
        async with app.state.sessionmaker() as db:
            task = await db.scalar(select(Task).where(Task.user_id == world.guest["id"]))
            receipt = dict(task.checkpoint["_analysis_completion"])
            receipt["result_hash"] = "0" * 64
            await db.execute(update(Task).where(Task.id == task.id).values(
                checkpoint={**task.checkpoint, "_analysis_completion": receipt},
            ))
            await db.commit()
        await _change(app, client, world, "revoked")

    _fired, changes = _change_before_done(app, monkeypatch, tamper_then_revoke)
    fake_llm.push(analysis_json())
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    engine = app.state.task_engine
    task_id = created.json()["id"]
    from tests.helpers import wait_until

    await wait_until(lambda: task_id not in engine._running and bool(changes) and changes[0].done(), within=10)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert row.status == "running" and row.execution_key is not None
    assert engine.has_cleanup_failure(world.project["id"])
    await engine.recover()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).status == "running"


async def test_recover_completes_valid_analysis_receipt_left_running(app, client, make_client, fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    unavailable = []

    async def unavailable_completion(self, task, **kwargs):
        unavailable.append(task.id)
        return None

    monkeypatch.setattr(type(engine), "_complete_from_analysis_receipt", unavailable_completion)
    _fired, _changes = _change_before_done(app, monkeypatch, lambda: _change(app, client, world, "revoked"))
    fake_llm.push(analysis_json())
    created = await _analyze(guest, world)
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]
    from tests.helpers import wait_until

    await wait_until(lambda: task_id not in engine._running and bool(unavailable), within=10)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).status == "running"
    monkeypatch.undo()
    await engine.recover()
    row = await _terminal(app, task_id)
    assert row.status == "succeeded" and row.error == ANALYSIS_NOTE and row.execution_key is None
    async with app.state.sessionmaker() as db:
        assert await analysis_service.completed_terminal_analysis_receipt(db, row) is not None
    assert len(fake_llm.calls) == 1
