"""E4-g2：accepted editor 的批量章节生成（chapter.batch，HTTP 入口 POST /projects/{pid}/batch-generate）。

批量的每一章都是独立的抢占、发送与保存：受理后领取前、领取后构建前、某章发送前、某章保存前、两章之间、
逐章分析前以及完成发布前撤权/降级/重邀/停用/关闭作者模型授权都必须失败关闭——已提交的章节保留，
未完成章节按原 claim/attempt 回退，不写旧正文、不继续后续章节；已保存但收尾核验失败时按持久断点受限收尾，
任务不得停在 running。模型解析/费用来源/归属/私人风格同 E4-f；逐章分析在本任务内按真实触发者执行，
记忆只维护关键词单元（E6 未开放），零作者向量调用。
协作者 SSE 自 E5-f 起经受控管线开放（见 test_collaboration_stream_*、test_task_sse_scope），
本文件经任务详情或数据库观察终态。
"""

import asyncio

import pytest
from sqlalchemy import func, select, update

from app.ai.providers import AIProviderError
from app.models import (
    AIConfig,
    Chapter,
    ChapterAnalysis,
    ChapterVersion,
    GenerationLog,
    Project,
    ProjectCollaborator,
    Task,
    User,
    WritingStyle,
)
from app.services import batch_service, generation_service
from app.services.task_engine import engine as engine_module
from app.services.task_engine.registry import get_handler
from tests.helpers import analysis_json, create_ai_config, create_project, register, wait_until

MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES
OWNER_KEY = "sk-owner-private-0001"
EDITOR_KEY = "sk-editor-own-0002"
BATCH_NOTE = engine_module.BATCH_RECEIPT_FINALIZED_NOTE
# 授权失效后允许出现的固定安全文案（本进程通知复查 / 输出发布核验 / 锁内发送或断点屏障 / 分析屏障）。
STOPPED = (
    MESSAGES["collaboration_revoked"], MESSAGES["collaboration_model_revoked"],
    MESSAGES["collaboration_account_unavailable"], "任务输出授权已变化，已停止",
    "权限或模型授权已变化，已停止发送", "分析任务执行身份或授权已变化，已停止",
    # 账号停用后终态写入无法再以原身份认证：引擎按原执行做无正文系统收尾的固定文案。
    "任务执行已停止",
)
AUTO_ON = {"allow_private_ai_endpoints": True, "auto_analyze_enabled": True}


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


class World:
    def __init__(self, project, chapters, owner, guest, owner_config, editor_config, guest_client=None):
        self.project, self.chapters, self.owner, self.guest = project, chapters, owner, guest
        self.owner_config, self.editor_config = owner_config, editor_config
        self.guest_client = guest_client

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.project['id']}"


async def _world(owner, guest, *, role="editor", allow_owner_ai=False, accept=True, editor_model=True, count=2):
    owner_user = await register(owner, "e4g2-owner")
    guest_user = await register(guest, "e4g2-guest")
    project = await create_project(owner)
    chapters = []
    for index in range(count):
        created = await owner.post(f"/api/v1/projects/{project['id']}/chapters",
                                   json={"title": f"第{index + 1}章", "content": ""})
        assert created.status_code == 201, created.text
        chapters.append(created.json())
    owner_config = await create_ai_config(owner, name="作者模型", model="owner-model", api_key=OWNER_KEY)
    editor_config = (await create_ai_config(guest, name="本人模型", model="editor-model", api_key=EDITOR_KEY)
                     if editor_model else None)
    invite = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
        "username": "e4g2-guest", "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert invite.status_code == 201, invite.text
    if accept:
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    return World(project, chapters, owner_user, guest_user, owner_config, editor_config, guest)


def _start(client, world, *, first=0, last=None, **body):
    last = len(world.chapters) - 1 if last is None else last
    payload = {"start_chapter_id": world.chapters[first]["id"], "end_chapter_id": world.chapters[last]["id"],
               "auto_continue": False, **body}
    return client.post(f"{world.base}/batch-generate", json=payload)


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
    elif change == "reinvite":
        response = await owner.delete(url, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
        invite = await owner.post(f"{world.base}/collaborators", json={
            "username": "e4g2-guest", "role": "editor", "allow_owner_ai": member.allow_owner_ai,
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


async def _task_count(app, kind=None):
    async with app.state.sessionmaker() as db:
        statement = select(func.count(Task.id))
        if kind is not None:
            statement = statement.where(Task.kind == kind)
        return await db.scalar(statement)


async def _chapter(app, chapter_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Chapter, chapter_id)


async def _assert_untouched(app, chapter_id):
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.content == "" and chapter.status == "draft", (chapter.content, chapter.status)
        assert chapter.generation_claim_key is None and chapter.generation_claim_task_key is None
        assert await db.scalar(select(GenerationLog.id).where(
            GenerationLog.chapter_id == chapter_id, GenerationLog.status == "success",
        )) is None
        assert await db.scalar(select(ChapterVersion.id).where(
            ChapterVersion.chapter_id == chapter_id, ChapterVersion.source == "generate",
        )) is None


async def _assert_saved(app, chapter_id, content, task_id, actor_id):
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapter_id)
        assert chapter.content == content and chapter.status == "done"
        assert chapter.generation_claim_key is None and chapter.generation_claim_task_key is None
        log = await db.scalar(select(GenerationLog).where(
            GenerationLog.chapter_id == chapter_id, GenerationLog.task_id == task_id,
        ))
        assert log.status == "success" and log.actor_user_id == actor_id
        return log


# ---------------- 成功路径、费用来源与归属 ----------------


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_editor_batch_uses_own_model_and_analyzes_as_real_actor(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    # 作者向量配置存在且已授权：E6-c 起逐章分析的记忆派生向量任务归协作者（携带原邀请轮次），使用作者向量。
    await create_ai_config(client, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key="sk-owner-embed-0003")
    fake_llm.push("第一章协作正文" * 3)
    fake_llm.push(analysis_json())
    fake_llm.push("第二章协作正文" * 3)
    fake_llm.push(analysis_json())
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    task = created.json()
    detail = await guest.get(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
    assert detail.status_code == 200, detail.text
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"] and row.project_id == world.project["id"]
    assert row.input["_collaboration_grant"]["invitation_id"]
    assert row.result["done"] == 2 and row.result["failed"] == []
    # 本人有文本配置：即使作者授权开启也只用本人密钥（生成与分析共四次），作者文本密钥零调用。
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY] * 4
    for chapter, text in zip(world.chapters, ("第一章协作正文" * 3, "第二章协作正文" * 3), strict=True):
        log = await _assert_saved(app, chapter["id"], text, row.id, world.guest["id"])
        assert (log.model_source, log.model_config_owner_id) == ("actor", world.guest["id"])
        assert log.ai_config_id == world.editor_config["id"]
    async with app.state.sessionmaker() as db:
        analyses = list(await db.scalars(select(ChapterAnalysis).where(
            ChapterAnalysis.project_id == world.project["id"],
        )))
        assert {a.chapter_id for a in analyses} == {c["id"] for c in world.chapters}
        assert {a.task_id for a in analyses} == {row.id}
        # 分析在批量任务内执行：不另建分析任务；E6-c 翻转（原断言为零派生）：每章一个归协作者的
        # 记忆向量子任务（收据式派生，原邀请轮次），绝不建作者名下的任务。
        others = list(await db.scalars(select(Task).where(Task.id != row.id)))
        assert len(others) == 2 and {t.kind for t in others} == {"kb.index"}
        assert {t.user_id for t in others} == {world.guest["id"]}
        assert all(t.input["_collaboration_grant"] == row.input["_collaboration_grant"] for t in others)
    for child in others:
        assert (await _terminal(app, child.id)).status == "succeeded"
    assert {call["model"] for call in fake_llm.embed_calls} == {"owner-embed"}
    assert all(item["analysis"] == "ok" for item in row.checkpoint["items"])
    # 已提交结果不因随后撤权回滚。
    await _change(app, client, world, "revoked")
    assert (await _chapter(app, world.chapters[0]["id"])).content == "第一章协作正文" * 3


async def test_editor_batch_owner_model_fallback_records_cost_source(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    fake_llm.push("借用作者模型一")
    fake_llm.push("借用作者模型二")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"]
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY, OWNER_KEY]
    assert row.input["_project_model"]["source"] == "owner"
    for chapter in world.chapters:
        async with app.state.sessionmaker() as db:
            log = await db.scalar(select(GenerationLog).where(GenerationLog.chapter_id == chapter["id"]))
        assert log.actor_user_id == world.guest["id"]
        assert (log.model_source, log.model_config_owner_id) == ("owner", world.owner["id"])
    detail = await guest.get(f"/api/v1/tasks/{row.id}", params={"task_key": row.statistics_key})
    assert detail.status_code == 200 and detail.json().get("ai_config_id") is None


@pytest.mark.parametrize("case", ["no_model_no_grant", "broken_own_model", "explicit_owner_config"])
async def test_editor_batch_model_errors_never_fall_back(app, client, make_client, fake_llm, case):
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
    response = await _start(guest, world, **body)
    assert response.status_code in ((400, 404) if case == "explicit_owner_config" else (400,)), response.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    for chapter in world.chapters:
        await _assert_untouched(app, chapter["id"])


async def test_editor_cannot_use_owner_private_style(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    async with app.state.sessionmaker() as db:
        style = WritingStyle(user_id=world.owner["id"], name="作者私有风格", description="私有描述甲乙丙",
                             extra="作者私有要求丁戊己")
        db.add(style)
        await db.flush()
        await db.execute(update(Chapter).where(Chapter.project_id == world.project["id"]).values(style_id=style.id))
        await db.execute(update(Project).where(Project.id == world.project["id"]).values(default_style_id=style.id))
        style_id = style.id
        await db.commit()
    explicit = await _start(guest, world, style=style_id)
    assert explicit.status_code == 400, explicit.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    fake_llm.push("不含私有风格一")
    fake_llm.push("不含私有风格二")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    for call in fake_llm.calls:
        sent = str(call["messages"])
        assert "私有描述甲乙丙" not in sent and "作者私有要求丁戊己" not in sent and "作者私有风格" not in sent
        assert call["api_key"] == EDITOR_KEY


@pytest.mark.parametrize("case", ["viewer", "pending", "outsider", "revoked", "admin_outsider", "admin_viewer"])
async def test_non_editors_cannot_preview_or_start_batch(app, client, make_client, fake_llm, case):
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
    payload = {"start_chapter_id": world.chapters[0]["id"], "end_chapter_id": world.chapters[-1]["id"]}
    preview = await guest.post(f"{world.base}/batch-generate/preview", json=payload)
    assert preview.status_code == expected, preview.text
    started = await guest.post(f"{world.base}/batch-generate", json=payload)
    assert started.status_code == expected, started.text
    assert await _task_count(app) == 0 and fake_llm.calls == []
    for chapter in world.chapters:
        await _assert_untouched(app, chapter["id"])


async def test_editor_preview_is_allowed(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    payload = {"start_chapter_id": world.chapters[0]["id"], "end_chapter_id": world.chapters[-1]["id"]}
    preview = await guest.post(f"{world.base}/batch-generate/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert [item["chapter_id"] for item in preview.json()["items"]] == [c["id"] for c in world.chapters]
    assert fake_llm.calls == [] and await _task_count(app) == 0


# ---------------- 受理后领取前、构建前、发送前 ----------------


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "owner_ai_direct", "disabled", "reinvite"])
async def test_change_after_admission_before_claim_fails_closed(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    owner_model = change.startswith("owner_ai")
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_model)
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    await _change(app, client, world, change)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    expected = {"disabled": "collaboration_account_unavailable", "owner_ai_direct": "collaboration_model_revoked"}
    assert row.status == "failed" and row.error == MESSAGES[expected.get(change, "collaboration_revoked")]
    assert fake_llm.calls == []
    for chapter in world.chapters:
        await _assert_untouched(app, chapter["id"])
    # 批量资源键已释放：作者可立即发起自己的批量。
    fake_llm.push("作者正文一")
    fake_llm.push("作者正文二")
    author = await _start(client, world)
    assert author.status_code == 202, author.text
    assert (await _terminal(app, author.json()["id"])).status == "succeeded"


@pytest.mark.parametrize("stage", ["build", "send"])
@pytest.mark.parametrize("change", ["revoked", "viewer"])
async def test_change_after_claim_before_first_send_never_sends(app, client, make_client, fake_llm, monkeypatch,
                                                                stage, change):
    guest = make_client()
    world = await _world(client, guest)
    handler = get_handler(batch_service.KIND)
    original_build = type(handler).build_job
    original_chapter_job = generation_service.build_chapter_job
    fired, changes = asyncio.Event(), []
    revoke = _elsewhere(app, client, world, change, changes)

    async def before_build(self, ctx, task, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_build(self, ctx, task, **kwargs)

    async def after_chapter_build(*args, **kwargs):
        job = await original_chapter_job(*args, **kwargs)
        if not fired.is_set():
            fired.set()
            await revoke()
        return job

    if stage == "build":
        monkeypatch.setattr(type(handler), "build_job", before_build)
    else:
        monkeypatch.setattr(generation_service, "build_chapter_job", after_chapter_build)
    fake_llm.push("不应发送的正文")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired.is_set()
    assert row.status == "failed" and row.output_text == "" and row.result is None, row.error
    assert fake_llm.calls == []
    for chapter in world.chapters:
        await _assert_untouched(app, chapter["id"])


# ---------------- 发送后保存前、两章之间、分析前 ----------------


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_change_during_send_before_save_never_writes(app, client, make_client, fake_llm, monkeypatch,
                                                           change, notify):
    """notify=False 模拟跨实例/未收到通知：只能靠发布核验与保存前锁内重验拒绝旧结果。"""
    from tests.helpers import Gate

    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    gate = Gate()
    fake_llm.push(["旧授权下", gate, "生成的正文"])
    fake_llm.push("不应发送的第二章")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _change(app, client, world, change)
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert "生成的正文" not in (row.output_text or "") and row.result is None
    assert len(fake_llm.calls) == 1
    for chapter in world.chapters:
        await _assert_untouched(app, chapter["id"])
    assert row.checkpoint["current"] is None


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("stage", ["send", "between"])
async def test_skip_on_fail_does_not_swallow_revocation(app, client, make_client, fake_llm, monkeypatch,
                                                        stage, notify):
    """skip_on_fail 只跳过普通单章失败；授权失效（发送中或两章之间）必须停止整个批量，后续章节零发送。"""
    from tests.helpers import Gate

    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest, count=3)
    changes = []
    if stage == "send":
        gate = Gate()
        fake_llm.push(["旧授权下", gate, "生成的正文"])
    else:
        original = batch_service._generate_chapter
        fired = asyncio.Event()
        revoke = _elsewhere(app, client, world, "revoked", changes)

        async def between(ctx, job, index, item):
            if index == 1 and not fired.is_set():
                fired.set()
                await revoke()
            async for event in original(ctx, job, index, item):
                yield event

        monkeypatch.setattr(batch_service, "_generate_chapter", between)
        fake_llm.push("第一章已保存的正文")
    fake_llm.push("不应发送的第二章")
    fake_llm.push("不应发送的第三章")
    created = await _start(guest, world, skip_on_fail=True)
    assert created.status_code == 202, created.text
    if stage == "send":
        await gate.wait_reached(10)
        await _change(app, client, world, "revoked")
        gate.release()
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert len(fake_llm.calls) == 1
    first = 0 if stage == "send" else 1
    if stage == "between":
        await _assert_saved(app, world.chapters[0]["id"], "第一章已保存的正文", row.id, world.guest["id"])
    for chapter in world.chapters[first:]:
        await _assert_untouched(app, chapter["id"])
    # 未把授权失效记为单章失败后跳过：停在原位置，后续章节保持 pending。
    assert row.checkpoint["current"] is None and row.step_cursor == first
    assert [item["state"] for item in row.checkpoint["items"][first + 1:]] == ["pending"] * (2 - first)


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct", "disabled", "reinvite"])
async def test_change_between_chapters_keeps_committed_chapter(app, client, make_client, fake_llm, monkeypatch,
                                                               change, notify):
    """第一章已完整提交（正文、版本、日志、断点），第二章抢占前授权变化：停止，不发送第二章。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_model = change == "owner_ai_direct"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_model)
    original = batch_service._generate_chapter
    fired, changes = asyncio.Event(), []
    revoke = _elsewhere(app, client, world, change, changes)

    async def between(ctx, job, index, item):
        if index == 1 and not fired.is_set():
            fired.set()
            await revoke()
        async for event in original(ctx, job, index, item):
            yield event

    monkeypatch.setattr(batch_service, "_generate_chapter", between)
    fake_llm.push("第一章已保存的正文")
    fake_llm.push("不应发送的第二章")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired.is_set()
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert len(fake_llm.calls) == 1
    await _assert_saved(app, world.chapters[0]["id"], "第一章已保存的正文", row.id, world.guest["id"])
    await _assert_untouched(app, world.chapters[1]["id"])
    # 部分完成：断点记录已完成的第一章，没有活动章节占用。
    assert row.step_cursor == 1 and row.checkpoint["current"] is None
    assert [item["state"] for item in row.checkpoint["items"]] == ["done", "pending"]
    # 资源键已释放：作者可继续对第二章发起批量。
    fake_llm.responses.clear()
    fake_llm.push("作者续写第二章")
    author = await _start(client, world, first=1)
    assert author.status_code == 202, author.text
    assert (await _terminal(app, author.json()["id"])).status == "succeeded"
    assert (await _chapter(app, world.chapters[1]["id"])).content == "作者续写第二章"


@pytest.mark.parametrize("notify", [True, False])
async def test_revoke_after_chapter_commit_before_checkpoint_sync(app, client, make_client, fake_llm, monkeypatch,
                                                                  notify):
    """取消/撤权落在某章保存提交之后、执行体同步断点之前：执行体快照无法证明，按持久断点受限收尾。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)
    original = batch_service._sync_completed
    fired, changes = asyncio.Event(), []
    revoke = _elsewhere(app, client, world, "revoked", changes)

    async def after_commit(ctx, job, claim):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original(ctx, job, claim)

    monkeypatch.setattr(batch_service, "_sync_completed", after_commit)
    fake_llm.push("提交后撤权的第一章")
    fake_llm.push("不应发送的第二章")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert row.execution_key is None and len(fake_llm.calls) == 1
    await _assert_saved(app, world.chapters[0]["id"], "提交后撤权的第一章", row.id, world.guest["id"])
    await _assert_untouched(app, world.chapters[1]["id"])
    # 第一章完成收据保留在断点（之后重试可从分析阶段继续），没有活动占用。
    assert row.checkpoint["current"] is None and row.step_cursor == 0
    assert row.checkpoint["phase"] == "analyze" and "_batch_completion" in row.checkpoint
    assert app.state.task_engine.has_cleanup_failure(world.project["id"]) is False


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
@pytest.mark.parametrize("notify", [True, False])
async def test_revoke_before_chapter_analysis_stops_without_analysis(app, client, make_client, fake_llm,
                                                                     monkeypatch, notify):
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)
    original = batch_service._reserve_review_analysis
    fired, changes = asyncio.Event(), []
    revoke = _elsewhere(app, client, world, "revoked", changes)

    async def before_analysis(ctx, job, chapter_id, ref):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original(ctx, job, chapter_id, ref)

    monkeypatch.setattr(batch_service, "_reserve_review_analysis", before_analysis)
    fake_llm.push("分析前撤权的第一章")
    fake_llm.push(analysis_json())
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert len(fake_llm.calls) == 1
    await _assert_saved(app, world.chapters[0]["id"], "分析前撤权的第一章", row.id, world.guest["id"])
    await _assert_untouched(app, world.chapters[1]["id"])
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ChapterAnalysis.id)) is None


@pytest.mark.parametrize("hook", ["done_event", "finalization"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "actor_token"])
async def test_change_after_all_chapters_before_publish_completes_from_checkpoint(
    app, client, make_client, fake_llm, monkeypatch, hook, change,
):
    """全部章节已按断点持久化，完成事件/终态核验前授权变化：按持久断点重建结果记为成功，不停在 running。"""
    guest = make_client()
    world = await _world(client, guest)
    fired, changes = asyncio.Event(), []
    if change == "actor_token":
        async def bump():
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == world.guest["id"]).values(
                    token_version=User.token_version + 1,
                ))
                await db.commit()

        async def revoke():
            work = asyncio.ensure_future(bump())
            changes.append(work)
            await asyncio.shield(work)
    else:
        revoke = _elsewhere(app, client, world, change, changes)
    engine = app.state.task_engine
    if hook == "done_event":
        original_event = type(engine)._event_task

        async def before_done(self, db, execution, current, event):
            if event == "done" and not fired.is_set():
                fired.set()
                await revoke()
            return await original_event(self, db, execution, current, event)

        monkeypatch.setattr(type(engine), "_event_task", before_done)
    else:
        # 完成事件已核验发布、终态写入取锁之前授权变化（撤权必须在终态事务取锁前提交，否则会等待它）。
        original_complete = type(engine).complete_state

        async def before_final(self, task, **kwargs):
            if kwargs.get("status") == "succeeded" and task is not None and not fired.is_set():
                fired.set()
                await revoke()
            return await original_complete(self, task, **kwargs)

        monkeypatch.setattr(type(engine), "complete_state", before_final)
    fake_llm.push("全部完成的第一章")
    fake_llm.push("全部完成的第二章")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired.is_set()
    assert row.status == "succeeded" and row.error == BATCH_NOTE, row.error
    assert row.execution_key is None and row.step_cursor == 2
    assert row.result == batch_service.summarize(row.checkpoint, 2, 2)
    assert row.result["done"] == 2
    for chapter, text in zip(world.chapters, ("全部完成的第一章", "全部完成的第二章"), strict=True):
        await _assert_saved(app, chapter["id"], text, row.id, world.guest["id"])
    assert engine.has_cleanup_failure(world.project["id"]) is False


async def test_batch_checkpoint_completion_rejects_forged_receipt(app, client, make_client, fake_llm, monkeypatch):
    """收尾只认本轮持久收据：篡改完成收据后不收尾，任务保留 running 屏障（交由恢复/管理员核对）。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    engine = app.state.task_engine
    guest = make_client()
    world = await _world(client, guest)
    original = batch_service._sync_completed
    fired = asyncio.Event()

    async def forge_then_fail(ctx, job, claim):
        if not fired.is_set():
            fired.set()
            async with app.state.sessionmaker() as db:
                task = await db.get(Task, job.task_id)
                checkpoint = dict(task.checkpoint)
                checkpoint["_batch_completion"] = {**checkpoint["_batch_completion"], "version_id": 999999}
                await db.execute(update(Task).where(Task.id == job.task_id).values(checkpoint=checkpoint))
                await db.commit()
            raise asyncio.CancelledError
        return await original(ctx, job, claim)

    monkeypatch.setattr(batch_service, "_sync_completed", forge_then_fail)
    fake_llm.push("被篡改收据的第一章")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]
    await wait_until(lambda: fired.is_set() and task_id not in engine._running, within=15)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
    assert row.status == "running" and row.execution_key is not None
    assert engine.has_cleanup_failure(world.project["id"]) is True


# ---------------- 重试、继续与作者链路 ----------------


async def test_editor_retry_keeps_round_and_reinvite_blocks_old_retry(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push("重试前的第一章")
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed" and first.step_cursor == 1
    fake_llm.push("重试补上的第二章")
    retried = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    second = await _terminal(app, retried.json()["id"])
    assert second.status == "succeeded", second.error
    assert second.user_id == world.guest["id"] and second.retry_of_id == first.id
    assert second.input["_collaboration_grant"] == first.input["_collaboration_grant"]
    await _assert_saved(app, world.chapters[1]["id"], "重试补上的第二章", second.id, world.guest["id"])
    # 再发起一次失败的批量，然后撤权并重新邀请：旧轮次任务不能借新邀请重试。
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    third_created = await _start(guest, world, include_written=True)
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


async def test_editor_resume_paused_batch_and_revoked_resume_rejected(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, count=3)
    fake_llm.push("审核点前的第一章")
    created = await _start(guest, world, review_every=1)
    assert created.status_code == 202, created.text
    task_ref = created.json()
    async with asyncio.timeout(15):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_ref["id"])
            if row.status == "paused":
                break
            await asyncio.sleep(0.02)
    fake_llm.push("继续后的第二章")
    resumed = await guest.post(f"/api/v1/tasks/{row.id}/resume", params={"task_key": row.statistics_key})
    assert resumed.status_code == 202, resumed.text
    async with asyncio.timeout(15):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_ref["id"])
            if row.status == "paused" and row.step_cursor == 2:
                break
            await asyncio.sleep(0.02)
    await _assert_saved(app, world.chapters[1]["id"], "继续后的第二章", row.id, world.guest["id"])
    engine = app.state.task_engine
    await _change(app, client, world, "viewer")
    blocked = await guest.post(f"/api/v1/tasks/{row.id}/resume", params={"task_key": row.statistics_key})
    assert blocked.status_code in (403, 404), blocked.text
    # E5-a：降级通知后系统按原轮次把待审核任务受限收尾为 failed（已保存的两章保留），不再永久停在 paused。
    while engine._collaboration_checks:
        await asyncio.gather(*list(engine._collaboration_checks), return_exceptions=True)
    async with app.state.sessionmaker() as db:
        stopped = await db.get(Task, row.id)
        assert (stopped.status, stopped.error) == ("failed", MESSAGES["collaboration_revoked"])
        assert stopped.step_cursor == 2
    # 收尾后降级成员仍不能继续 / 结束（不能借终态重新排队或改写结果），也不触发新调用。
    for op in ("resume", "finish"):
        again = await guest.post(f"/api/v1/tasks/{row.id}/{op}", params={"task_key": row.statistics_key})
        assert again.status_code in (403, 404, 409), (op, again.text)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, row.id)).status == "failed"
    await _assert_untouched(app, world.chapters[2]["id"])
    assert len(fake_llm.calls) == 2


async def test_author_batch_unchanged_alongside_editor(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push("作者第一章")
    fake_llm.push("作者第二章")
    created = await _start(client, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded" and row.user_id == world.owner["id"]
    assert "_collaboration_grant" not in row.input
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY, OWNER_KEY]
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.task_id == row.id))
        assert (log.actor_user_id, log.model_source, log.model_config_owner_id) == (
            world.owner["id"], "actor", world.owner["id"],
        )
    # 编辑者的批量与作者批量共用作品级资源键：作者进行中时编辑者 409（反之亦然）。
    await app.state.task_engine.stop()
    fake_llm.push("作者重写一")
    fake_llm.push("作者重写二")
    queued = await _start(client, world, include_written=True)
    assert queued.status_code == 202, queued.text
    conflict = await _start(guest, world, include_written=True)
    assert conflict.status_code == 409, conflict.text
    await app.state.task_engine.start()
    assert (await _terminal(app, queued.json()["id"])).status == "succeeded"
    assert await _task_count(app) == 2


async def test_author_cancel_after_chapter_commit_before_checkpoint_sync(app, client, make_client, fake_llm,
                                                                         monkeypatch):
    """作者用户取消落在某章保存提交之后、断点同步之前：修复前任务永久 running；现按持久断点收尾为 cancelled，
    第一章与完成收据保留，重试从断点继续且不重写第一章。"""
    from tests.helpers import task_params

    world = await _world(client, make_client())
    original = batch_service._sync_completed
    fired, requests = asyncio.Event(), []

    async def after_commit(ctx, job, claim):
        if not fired.is_set():
            fired.set()
            requests.append(asyncio.ensure_future(
                client.post(f"/api/v1/tasks/{job.task_id}/cancel", params=task_params((job.task_id, job.task_key))),
            ))
            await asyncio.sleep(30)  # 由用户取消打断
        return await original(ctx, job, claim)

    monkeypatch.setattr(batch_service, "_sync_completed", after_commit)
    fake_llm.push("作者取消前的第一章")
    created = await _start(client, world)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    cancelled = await asyncio.gather(*requests)
    assert cancelled[0].status_code in (200, 202), cancelled[0].text
    assert row.status == "cancelled" and row.execution_key is None, (row.status, row.error)
    assert len(fake_llm.calls) == 1
    await _assert_saved(app, world.chapters[0]["id"], "作者取消前的第一章", row.id, world.owner["id"])
    await _assert_untouched(app, world.chapters[1]["id"])
    assert row.checkpoint["current"] is None and row.step_cursor == 0
    assert row.checkpoint["phase"] == "analyze" and "_batch_completion" in row.checkpoint
    assert app.state.task_engine.has_cleanup_failure(world.project["id"]) is False

    fake_llm.push("作者重试的第二章")
    retried = await client.post(f"/api/v1/tasks/{row.id}/retry", params={"task_key": row.statistics_key})
    assert retried.status_code == 202, retried.text
    done = await _terminal(app, retried.json()["id"])
    assert done.status == "succeeded" and done.step_cursor == 2, (done.status, done.error)
    assert len(fake_llm.calls) == 2
    await _assert_saved(app, world.chapters[0]["id"], "作者取消前的第一章", row.id, world.owner["id"])
    await _assert_saved(app, world.chapters[1]["id"], "作者重试的第二章", done.id, world.owner["id"])


async def test_author_agent_batch_confirmed_through_signed_proposal(app, client, fake_llm):
    """作者 Agent 的批量只能经签名提案确认（execute_admitted）受理，行为保持：确认后真实执行并保存。"""
    from tests.helpers import wait_task
    from tests.test_agent_runtime import call, send_task, setup_agent, turn_action

    project, base, url = await setup_agent(client, auto=True)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "空章"})).json()
    fake_llm.push_tools([call("start_task", {"kind": "chapter.batch", "params": {
        "start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"], "auto_continue": False,
    }}, "start")])
    task_ref = await send_task(client, url)
    task = await wait_task(client, task_ref, statuses=("paused", "failed"))
    assert task["status"] == "paused", task
    fake_llm.push("智能体发起的批量正文")
    response = await client.post(base + f"/turns/{task_ref['id']}/confirm-all",
                                 json=await turn_action(client, url, task_ref, "confirm-all"))
    assert response.status_code == 200, response.text
    change = response.json()["changes"][-1]
    assert change["status"] == "executed", response.text
    child_id = change["result"]["task_id"]
    children = [item for item in (await client.get("/api/v1/tasks", params={"kind": "chapter.batch"})).json()
                if item["id"] == child_id]
    assert len(children) == 1
    done = await wait_task(client, children[0])
    assert done["status"] == "succeeded", done
    assert (await _chapter(app, chapter["id"])).content == "智能体发起的批量正文"
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, child_id)
        assert row.user_id == (await db.get(Project, project["id"])).user_id
        assert "_collaboration_grant" not in row.input and row.input["_project_model"]["source"] == "actor"
