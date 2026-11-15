"""E4-f：accepted editor 的章节 AI 任务入口（chapter.generate / polish / revise）。

只开放章节 handler；协作者 SSE 订阅自 E5-f 起经受控管线开放，本文件经任务详情或数据库观察终态。
覆盖：本人模型优先、仅无配置时按授权回退作者模型、失效不回退；Task/GenerationLog 归真实 actor 并记录费用来源；
viewer/非成员/待接受/已撤权/管理员人工旁路均不能触发；受理后领取前、领取后 build 前、发送前、保存前撤权失败关闭，
按原 claim 回退章节且不写旧正文；已提交结果不因后续撤权回滚；作者私人风格与配置不可被协作者使用。
"""

import asyncio

import pytest
from sqlalchemy import func, select, update

from app.models import AIConfig, Chapter, GenerationLog, Project, ProjectCollaborator, Task, User, WritingStyle
from app.services import generation_service
from app.services.task_engine import engine as engine_module
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, create_ai_config, create_project, register, wait_until

MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES
OWNER_KEY = "sk-owner-private-0001"
EDITOR_KEY = "sk-editor-own-0002"


AUTO_ANALYZE_ON = {"allow_private_ai_endpoints": True, "auto_analyze_enabled": True}


@pytest.fixture
def settings_overrides():
    # E4-g1 起协作者章节成功会派生自动分析（另起模型调用），本文件按章节调用次数断言，默认关闭自动分析；
    # 派生/不派生的正反例见 test_collaboration_analysis_entry_e4g.py。按收据收尾的用例显式打开，
    # 验证即使开启也不派生（收尾不借当前身份补签后续任务）。
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def _world(owner, guest, *, role="editor", allow_owner_ai=False, accept=True, owner_model=True,
                 editor_model=True):
    owner_user = await register(owner, "e4f-owner")
    guest_user = await register(guest, "e4f-guest")
    project = await create_project(owner)
    created = await owner.post(f"/api/v1/projects/{project['id']}/chapters",
                               json={"title": "第一章", "content": "原正文内容"})
    assert created.status_code == 201, created.text
    owner_config = (await create_ai_config(owner, name="作者模型", model="owner-model", api_key=OWNER_KEY)
                    if owner_model else None)
    editor_config = (await create_ai_config(guest, name="本人模型", model="editor-model", api_key=EDITOR_KEY)
                     if editor_model else None)
    invite = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "e4f-guest", "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert invite.status_code == 201, invite.text
    if accept:
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    return SimpleWorld(project, created.json(), owner_user, guest_user, owner_config, editor_config)


class SimpleWorld:
    def __init__(self, project, chapter, owner, guest, owner_config, editor_config):
        self.project, self.chapter, self.owner, self.guest = project, chapter, owner, guest
        self.owner_config, self.editor_config = owner_config, editor_config


async def _member(app, world):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ))


async def _change(app, owner, world, change):
    member = await _member(app, world)
    url = f"/api/v1/projects/{world.project['id']}/collaborators/{world.guest['id']}"
    if change == "revoked":
        response = await owner.delete(url, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
    elif change == "viewer":
        response = await owner.patch(url, json={"expected_revision": member.revision, "role": "viewer"})
        assert response.status_code == 200, response.text
    elif change == "owner_ai":
        response = await owner.patch(url, json={"expected_revision": member.revision, "allow_owner_ai": False})
        assert response.status_code == 200, response.text
    elif change == "disabled":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(is_active=False))
            await db.commit()
    else:
        raise AssertionError(change)


async def _terminal(app, task_id, within=10.0):
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


async def _assert_untouched(app, world):
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, world.chapter["id"])
        assert chapter.content == "原正文内容" and chapter.status == world.chapter["status"]
        assert chapter.generation_claim_key is None and chapter.generation_claim_task_key is None
        assert await db.scalar(select(GenerationLog.id).where(
            GenerationLog.chapter_id == world.chapter["id"], GenerationLog.status == "success",
        )) is None


@pytest.mark.parametrize("kind", ["generate", "polish"])
async def test_editor_chapter_task_uses_own_model_and_saves_as_real_actor(app, client, make_client, fake_llm, kind):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    fake_llm.push("协作者生成的新正文" * 4)
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/{kind}", json={})
    assert created.status_code == 202, created.text
    task = created.json()
    # 本人 + 当前授权轮次可查看自己的任务详情（不等于开放 SSE）。
    detail = await guest.get(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
    assert detail.status_code == 200, detail.text
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert row.error is None or row.error == ""
    assert row.user_id == world.guest["id"] and row.project_id == world.project["id"]
    # 本人有配置：即使作者授权开启也优先本人模型，作者密钥零调用。
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY]
    assert fake_llm.calls[0]["model"] == "editor-model"
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, world.chapter["id"])
        assert chapter.content == "协作者生成的新正文" * 4 and chapter.status == "done"
        assert chapter.generation_claim_key is None
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == task["id"]))
        assert log.actor_user_id == world.guest["id"] and log.status == "success"
        assert (log.model_source, log.model_config_owner_id) == ("actor", world.guest["id"])
        assert log.ai_config_id == world.editor_config["id"]
        # 部署级自动分析关闭：不派生任何分析，更不会改记为作者任务。
        assert await db.scalar(select(Task.id).where(Task.kind == "chapter.analyze")) is None
    # 已提交的合法结果不因随后撤权回滚。
    await _change(app, client, world, "revoked")
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, world.chapter["id"])).content == "协作者生成的新正文" * 4


async def test_editor_without_own_model_uses_owner_model_only_when_allowed(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    fake_llm.push("借用作者模型的正文" * 3)
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"]
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY]
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == row.id))
        # 费用来源：实际使用作者密钥，触发者仍为协作者。
        assert log.actor_user_id == world.guest["id"]
        assert (log.model_source, log.model_config_owner_id) == ("owner", world.owner["id"])
    # 协作者看不到作者配置 ID（任务投影与日志投影都不泄露）。
    detail = await guest.get(f"/api/v1/tasks/{row.id}", params={"task_key": row.statistics_key})
    assert detail.status_code == 200 and detail.json().get("ai_config_id") is None


async def test_editor_without_model_and_without_grant_is_rejected(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=False, editor_model=False)
    response = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert response.status_code == 400, response.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    await _assert_untouched(app, world)


async def test_editor_invalid_own_model_never_falls_back_to_owner(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == world.editor_config["id"]).values(
            api_key_enc="not-a-valid-token",
        ))
        await db.commit()
    response = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert response.status_code == 400, response.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    await _assert_untouched(app, world)


async def test_editor_cannot_select_owner_config_or_owner_private_style(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    chosen = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate",
                              json={"ai_config_id": world.owner_config["id"]})
    assert chosen.status_code in (400, 404), chosen.text
    async with app.state.sessionmaker() as db:
        style = WritingStyle(user_id=world.owner["id"], name="作者私有风格", description="私有描述甲乙丙",
                             extra="作者私有要求丁戊己")
        db.add(style)
        await db.flush()
        await db.execute(update(Chapter).where(Chapter.id == world.chapter["id"]).values(style_id=style.id))
        await db.execute(update(Project).where(Project.id == world.project["id"]).values(default_style_id=style.id))
        style_id = style.id
        await db.commit()
    explicit = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={"style": style_id})
    assert explicit.status_code == 400, explicit.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    fake_llm.push("未使用私有风格的正文")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    sent = str(fake_llm.calls[0]["messages"])
    assert "私有描述甲乙丙" not in sent and "作者私有要求丁戊己" not in sent and "作者私有风格" not in sent
    assert fake_llm.calls[0]["api_key"] == EDITOR_KEY
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == row.id))
        assert "作者私有" not in str(log.params) and "私有描述" not in str(log.params)
        # 作者的风格绑定保持不变。
        assert (await db.get(Chapter, world.chapter["id"])).style_id == style_id


@pytest.mark.parametrize("case", ["viewer", "pending", "outsider", "revoked", "admin_outsider", "admin_viewer"])
async def test_non_editors_cannot_start_chapter_ai(app, client, make_client, fake_llm, case):
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
    for kind in ("generate", "polish"):
        response = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/{kind}", json={})
        # 可读但缺 ai.use → 403；无协作读取来源（含仅凭管理员身份）→ 404，均不建任务。
        expected = 403 if case in ("viewer", "admin_viewer") else 404
        assert response.status_code == expected, (kind, response.text)
    assert await _task_count(app) == 0 and fake_llm.calls == []
    await _assert_untouched(app, world)


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "disabled"])
async def test_revoke_after_admission_before_claim_fails_closed(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, world.chapter["id"])
        assert chapter.status == "generating" and chapter.generation_claim_key is not None
    await _change(app, client, world, change)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    expected = "collaboration_account_unavailable" if change == "disabled" else "collaboration_revoked"
    assert row.status == "failed" and row.error == MESSAGES[expected]
    assert fake_llm.calls == []
    await _assert_untouched(app, world)


@pytest.mark.parametrize("stage", ["build", "send"])
@pytest.mark.parametrize("change", ["revoked", "viewer"])
async def test_revoke_after_claim_before_send_never_sends(app, client, make_client, fake_llm, monkeypatch,
                                                           stage, change):
    guest = make_client()
    world = await _world(client, guest)
    handler = get_handler("chapter.generate")
    original_build = type(handler).build_job
    original_chapter_job = generation_service.build_chapter_job
    fired = asyncio.Event()
    changes = []

    async def revoke_elsewhere():
        # 撤权请求在独立任务中执行（与真实服务器一致）：ASGITransport 会在调用方任务里运行应用，
        # 若在执行体任务内直接请求，随后的撤权通知会取消执行体并打断撤权请求自身的数据库收尾。
        change_task = asyncio.ensure_future(_change(app, client, world, change))
        changes.append(change_task)
        await asyncio.shield(change_task)

    async def revoke_before_build(self, ctx, task, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke_elsewhere()
        return await original_build(self, ctx, task, **kwargs)

    async def revoke_after_build(*args, **kwargs):
        job = await original_chapter_job(*args, **kwargs)
        if not fired.is_set():
            fired.set()
            await revoke_elsewhere()
        return job

    if stage == "build":
        monkeypatch.setattr(type(handler), "build_job", revoke_before_build)
    else:
        monkeypatch.setattr(generation_service, "build_chapter_job", revoke_after_build)
    fake_llm.push("不应发送的正文")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired.is_set()
    await asyncio.gather(*changes)
    assert row.status == "failed" and row.output_text == ""
    assert fake_llm.calls == []
    await _assert_untouched(app, world)


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_revoke_after_send_before_save_never_writes(app, client, make_client, fake_llm, monkeypatch,
                                                          change, notify):
    """notify=False 模拟跨实例/未收到通知：只能靠保存前的锁内重验拒绝旧结果。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    gate = Gate()
    fake_llm.push(["旧授权下", gate, "生成的正文"])
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _change(app, client, world, change)
    gate.release()
    row = await _terminal(app, created.json()["id"])
    # 只允许固定安全文案：本进程通知复查（notify=True）或输出发布/保存前锁内核验（跨实例）。
    assert row.status == "failed" and row.error in (
        MESSAGES["collaboration_revoked"], "任务输出授权已变化，已停止", "权限或模型授权已变化，已停止发送",
    ), row.error
    # 撤权前已合法发布的分片可作为本人失败输出保留；撤权后的分片绝不落库。
    assert row.output_text in ("", "旧授权下") and "生成的正文" not in row.output_text and row.result is None
    assert len(fake_llm.calls) == 1
    await _assert_untouched(app, world)


async def test_editor_retry_keeps_round_and_reinvite_blocks_old_retry(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    from app.ai.providers import AIProviderError

    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed"
    await _assert_untouched(app, world)
    fake_llm.push("重试后的正文")
    retried = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    second = await _terminal(app, retried.json()["id"])
    assert second.status == "succeeded", second.error
    assert second.user_id == world.guest["id"] and second.retry_of_id == first.id
    assert second.input["_collaboration_grant"] == first.input["_collaboration_grant"]
    # 再失败一次，然后撤权并重新邀请：旧轮次的任务不能借新邀请重试。
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    third_created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/polish", json={})
    assert third_created.status_code == 202, third_created.text
    third = await _terminal(app, third_created.json()["id"])
    assert third.status == "failed"
    await _change(app, client, world, "revoked")
    invite = await client.post(f"/api/v1/projects/{world.project['id']}/collaborators", json={
        "username": "e4f-guest", "role": "editor",
    })
    assert invite.status_code == 201, invite.text
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": invite.json()["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    calls = len(fake_llm.calls)
    stale = await guest.post(f"/api/v1/tasks/{third.id}/retry", params={"task_key": third.statistics_key})
    # 原模型绑定携带旧邀请轮次，预检即拒绝（不借新邀请补签），无任务、无发送。
    assert stale.status_code == 400 and stale.json()["detail"] == "模型绑定无效或已失效", stale.text
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == third.id)) == 0


async def test_author_chapter_task_unchanged_alongside_editor(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push("作者自己的正文")
    created = await client.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded" and row.user_id == world.owner["id"]
    assert "_collaboration_grant" not in row.input
    assert fake_llm.calls[0]["api_key"] == OWNER_KEY
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == row.id))
        assert (log.actor_user_id, log.model_source, log.model_config_owner_id) == (
            world.owner["id"], "actor", world.owner["id"],
        )


async def test_revoke_cancel_waits_for_in_flight_event_check(app, client, make_client, fake_llm, monkeypatch):
    """撤权复查的取消若打断持锁核验事务，SQLite 会遗留写事务使原 claim 回退永远拿不到锁。

    确定性复现：delta 核验开始后阻塞，撤权通知触发 cancel_running；取消必须等核验结束再传递，
    任务随后失败关闭、章节占用释放、旧正文不写入。
    """
    engine = app.state.task_engine
    original = type(engine)._event_task
    entered, release = asyncio.Event(), asyncio.Event()
    interrupted = []

    async def blocking(self, db, execution, current, event):
        if event == "delta" and not entered.is_set():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.append(event)
                raise
        return await original(self, db, execution, current, event)

    monkeypatch.setattr(type(engine), "_event_task", blocking)
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push("撤权前开始的输出")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    await asyncio.wait_for(entered.wait(), 10)
    await _change(app, client, world, "viewer")
    # 给复查与 cancel_running 足够时间到达；修复前取消会在此处打断核验。
    await asyncio.sleep(0.3)
    assert interrupted == []
    release.set()
    row = await _terminal(app, created.json()["id"])
    assert interrupted == []
    assert row.status == "failed" and row.error in (
        MESSAGES["collaboration_revoked"], "任务输出授权已变化，已停止",
    ), row.error
    await _assert_untouched(app, world)


@pytest.mark.parametrize("allow_owner_ai", [False, True])
async def test_editor_retrieval_uses_owner_vector_only_when_allowed(app, client, make_client, fake_llm,
                                                                   allow_owner_ai):
    """共享索引由作者向量建立：未授权时协作者生成只走关键词、零向量调用；授权后查询嵌入用作者向量模型。"""
    from tests.helpers import wait_task

    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=allow_owner_ai)
    await create_ai_config(client, name="作者向量", model_type="embedding", model="emb-owner")
    base = f"/api/v1/projects/{world.project['id']}/kb"
    text = "# 门派\n\n天剑宗位于北境雪山之巅，宗主林远山以剑道闻名天下。\n\n# 地理\n\n城南集市以售卖灵草闻名。"
    uploaded = await client.post(f"{base}/documents", files={"file": ("设定.md", text.encode(), "text/markdown")})
    assert uploaded.status_code == 202, uploaded.text
    indexed = await wait_task(client, uploaded.json()["task"])
    assert indexed["status"] == "succeeded", indexed
    assert fake_llm.embed_calls and all(call["model"] == "emb-owner" for call in fake_llm.embed_calls)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == world.chapter["id"]).values(title="天剑宗雪山比剑"))
        await db.commit()
    before = len(fake_llm.embed_calls)
    fake_llm.push("协作者在共享资料下生成的正文")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    text_calls = [call for call in fake_llm.calls if call["model"] == "editor-model"]
    assert len(text_calls) == 1 and text_calls[0]["api_key"] == EDITOR_KEY
    query_embeds = fake_llm.embed_calls[before:]
    if allow_owner_ai:
        # 显式例外：授权后查询嵌入使用作者项目向量模型（费用由作者密钥承担），只读不改共享索引。
        assert query_embeds and all(call["model"] == "emb-owner" for call in query_embeds)
    else:
        assert query_embeds == []
    async with app.state.sessionmaker() as db:
        selection = row.input["_shared_embedding"]
        assert selection["mode"] == ("vector" if allow_owner_ai else "keyword")
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "kb.index",
                                                                  Task.user_id == world.guest["id"])) == 0


async def test_revoke_cancel_waits_for_in_flight_build_job(app, client, make_client, fake_llm, monkeypatch):
    """build_job 含持锁事务（require_write_execution 等）：撤权复查的取消须等其结束后再传递，随后失败关闭并释放占用。"""
    handler = get_handler("chapter.generate")
    original = type(handler).build_job
    entered, release = asyncio.Event(), asyncio.Event()
    interrupted = []

    async def blocking(self, ctx, task, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            interrupted.append(True)
            raise
        return await original(self, ctx, task, **kwargs)

    monkeypatch.setattr(type(handler), "build_job", blocking)
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push("不应发送的正文")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    await asyncio.wait_for(entered.wait(), 10)
    await _change(app, client, world, "revoked")
    await asyncio.sleep(0.3)
    assert interrupted == []
    release.set()
    row = await _terminal(app, created.json()["id"])
    assert interrupted == [] and fake_llm.calls == []
    assert row.status == "failed" and row.error == MESSAGES["collaboration_revoked"], row.error
    await _assert_untouched(app, world)


# ---- 保存已提交、撤权落在提交与完成事件核验之间：凭完成收据受限系统收尾（不永久占用章节） ----

RECEIPT_NOTE = engine_module.RECEIPT_FINALIZED_NOTE
RECEIPT_DETAIL = engine_module.RECEIPT_FINALIZED_DETAIL


async def _bump_token(app, user_id):
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1))
        await db.commit()


def _change_before_done(app, monkeypatch, change):
    """done 核验取锁之前，由独立任务完整提交授权变化（保存事务已提交）。"""
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


async def _assert_receipt_completed(app, row, world, text, calls, fake_llm):
    assert row.status == "succeeded" and row.execution_key is None, row.error
    assert row.error == RECEIPT_NOTE and row.progress == 100 and row.finished_at is not None
    assert row.checkpoint["_chapter_claim"]["phase"] == "completed"
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, world.chapter["id"])
        # 已提交的合法结果保留，章节占用已在保存事务内释放。
        assert chapter.content == text and chapter.status == "done"
        assert chapter.generation_claim_key is None and chapter.generation_claim_task_key is None
        assert await db.scalar(select(func.count(GenerationLog.id)).where(
            GenerationLog.task_id == row.id, GenerationLog.status == "success",
        )) == 1
        # 不派生自动分析（收尾不借当前身份补签任何后续任务）。
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "chapter.analyze")) == 0
    engine = app.state.task_engine
    assert not engine.has_cleanup_failure(world.project["id"])
    assert row.id not in engine.hubs


@pytest.mark.parametrize("settings_overrides", [AUTO_ANALYZE_ON])
@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_revoke_between_commit_and_done_completes_from_receipt(app, client, make_client, fake_llm,
                                                                    monkeypatch, change, notify):
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    fired, changes = _change_before_done(app, monkeypatch, lambda: _change(app, client, world, change))
    fake_llm.push("提交后才撤权的正文")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired == ["done"]
    await _assert_receipt_completed(app, row, world, "提交后才撤权的正文", 1, fake_llm)
    # 活动资源已释放：作者可立即对该章再次发起 AI；失权的协作者不能。
    fake_llm.push("作者随后的正文")
    again = await client.post(f"/api/v1/chapters/{world.chapter['id']}/polish", json={})
    assert again.status_code == 202, again.text
    assert (await _terminal(app, again.json()["id"])).status == "succeeded"
    denied = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/polish", json={})
    # 撤权 404、降为 viewer 403；仍是 editor 但无本人模型且作者授权已关 → 400（不回退作者模型）。
    assert denied.status_code == {"revoked": 404, "viewer": 403, "owner_ai": 400}[change], denied.text


@pytest.mark.parametrize("settings_overrides", [AUTO_ANALYZE_ON])
async def test_author_token_change_between_commit_and_done_completes_from_receipt(app, client, make_client,
                                                                                 fake_llm, monkeypatch):
    guest = make_client()
    world = await _world(client, guest)
    fired, changes = _change_before_done(app, monkeypatch, lambda: _bump_token(app, world.owner["id"]))
    fake_llm.push("作者改密前提交的正文")
    created = await client.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired == ["done"] and "_collaboration_grant" not in row.input
    await _assert_receipt_completed(app, row, world, "作者改密前提交的正文", 1, fake_llm)


@pytest.mark.parametrize("settings_overrides", [AUTO_ANALYZE_ON])
async def test_receipt_completion_rejects_tampered_receipt(app, client, make_client, fake_llm, monkeypatch):
    """收据被篡改（与版本摘要不一致）时不能收尾为成功，保留原屏障待核对。"""
    guest = make_client()
    world = await _world(client, guest)

    async def tamper_then_revoke():
        async with app.state.sessionmaker() as db:
            task = await db.scalar(select(Task).where(Task.user_id == world.guest["id"]))
            receipt = dict(task.checkpoint["_chapter_claim"])
            receipt["version_hash"] = "0" * 64
            await db.execute(update(Task).where(Task.id == task.id).values(
                checkpoint={**task.checkpoint, "_chapter_claim": receipt},
            ))
            await db.commit()
        await _change(app, client, world, "revoked")

    fired, changes = _change_before_done(app, monkeypatch, tamper_then_revoke)
    fake_llm.push("收据被篡改的正文")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    engine = app.state.task_engine
    task_id = created.json()["id"]
    await wait_until(lambda: task_id not in engine._running and bool(changes) and changes[0].done(), within=10)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert row.status == "running" and row.execution_key is not None
    assert engine.has_cleanup_failure(world.project["id"])
    # 恢复路径同样拒绝伪造收据。
    await engine.recover()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).status == "running"


@pytest.mark.parametrize("settings_overrides", [AUTO_ANALYZE_ON])
async def test_recover_completes_valid_receipt_left_running(app, client, make_client, fake_llm, monkeypatch):
    """进程内收尾不可用（例如收尾前进程退出）时，重启恢复凭有效完成收据收尾，不永久占用。"""
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    unavailable = []

    async def unavailable_completion(self, task, **kwargs):
        unavailable.append(task.id)
        return None

    monkeypatch.setattr(type(engine), "_complete_from_chapter_receipt", unavailable_completion)
    fired, changes = _change_before_done(app, monkeypatch, lambda: _change(app, client, world, "revoked"))
    fake_llm.push("恢复时收尾的正文")
    created = await guest.post(f"/api/v1/chapters/{world.chapter['id']}/generate", json={})
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]
    await wait_until(lambda: task_id not in engine._running and bool(unavailable), within=10)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).status == "running"
    monkeypatch.undo()
    await engine.recover()
    row = await _terminal(app, task_id)
    assert row.result is None  # 恢复路径没有已验证的完成载荷，不伪造结果。
    assert row.status == "succeeded" and row.error == RECEIPT_NOTE and row.execution_key is None
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, world.chapter["id"])
        assert chapter.content == "恢复时收尾的正文" and chapter.generation_claim_key is None
    assert len(fake_llm.calls) == 1
