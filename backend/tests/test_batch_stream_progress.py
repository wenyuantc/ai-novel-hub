"""批量下一章占用已提交、ChapterJob 未构建时，进度刷新仍须通过原身份与权限门禁。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models import Chapter, ProjectCollaborator, Task
from app.services import batch_service
from app.services.ai_dispatch import VisibilityPolicyError
from tests.helpers import Gate, analysis_json
from tests.test_collaboration_batch_entry_e4g2 import _start, _terminal, _world
from tests.test_collaboration_stream_batch_e5c2 import _segments, _subscribe_mid_chapter
from tests.test_collaboration_stream_running_e5c1 import _mount, _serve, _Stream


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


class _PrebuildFlush:
    """只控制刷新时点：前序事件保留 dirty，第二章占用提交后才到达刷新间隔。"""

    def __init__(self, app, monkeypatch):
        self.reached, self.release = asyncio.Event(), asyncio.Event()
        self.hub, self.job = None, None
        self.kwargs = None
        self.persisted: list[tuple[str, str, int]] = []
        engine = app.state.task_engine
        monkeypatch.setattr(engine, "flush_interval", 3600)
        refresh_stage, flush = batch_service._refresh_stage, engine._flush
        self.flush = flush

        def refresh(job):
            if job.cursor == 1:
                engine.flush_interval = 0
            return refresh_stage(job)

        async def held_flush(hub, **kwargs):
            job = kwargs.get("job")
            selected = (isinstance(job, batch_service.BatchJob) and job.cursor == 1
                        and job.active_claim is not None and job.active_chapter_job is None)
            if selected:
                self.hub, self.job = hub, job
                self.kwargs = kwargs
                self.reached.set()
                await self.release.wait()
            await flush(hub, **kwargs)
            if selected:
                async with app.state.sessionmaker() as db:
                    row = await db.get(Task, hub.task_id)
                    self.persisted.append((row.output_text, row.stage, row.step_cursor))

        monkeypatch.setattr(batch_service, "_refresh_stage", refresh)
        monkeypatch.setattr(engine, "_flush", held_flush)

    async def wait(self):
        await asyncio.wait_for(self.reached.wait(), 10)
        assert self.job.active_attempt is not None
        assert self.hub.text == self.job.verified_text == ""


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True, "auto_analyze_enabled": True}])
@pytest.mark.parametrize("actor", ["editor", "owner"])
async def test_batch_prebuild_progress_flush_keeps_chapter_and_analysis_stream(
    app, client, make_client, fake_llm, monkeypatch, actor,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    probe = _PrebuildFlush(app, monkeypatch)
    probe.release.set()
    _mount(app)
    gate = Gate()
    fake_llm.push(["第一章开头", gate, "第一章结尾"])
    fake_llm.push(analysis_json(summary="第一章分析"))
    fake_llm.push("第二章完整正文")
    fake_llm.push(analysis_json(summary="第二章分析"))
    starter = guest if actor == "editor" else client
    created = await _start(starter, world)
    assert created.status_code == 202, created.text
    task = created.json()
    try:
        await gate.wait_reached()
        await _subscribe_mid_chapter(app, starter, task, "第一章开头")
        async with _serve(app) as base:
            stream = _Stream(base, starter, task)
            try:
                await stream.wait_for(lambda events: any(event == "snapshot" for event, _ in events))
                gate.release()
                events = await stream.finish(within=30)
            finally:
                await stream.aclose()
    finally:
        gate.release()
    assert events[-1][0] == "done", events
    assert probe.persisted == [("", "第 2/2 章 · 生成中", 1)]
    assert [data["index"] for event, data in events if event == "chapter_end"] == [0, 1]
    assert sum("分析中" in data.get("stage", "") for event, data in events if event == "progress") == 2
    async with app.state.sessionmaker() as db:
        contents = [(await db.get(Chapter, chapter["id"])).content for chapter in world.chapters]
    assert _segments(events) == contents == ["第一章开头第一章结尾", "第二章完整正文"]
    assert events[-1][1]["task"]["status"] == "succeeded"


async def _start_at_second_flush(guest, world, fake_llm, probe):
    fake_llm.push("第一章已完成")
    fake_llm.push("第二章不得生成")
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    await probe.wait()
    return created.json()


async def _assert_second_chapter_untouched(app, world, fake_llm, task, probe):
    row = await _terminal(app, task["id"])
    assert row.status == "failed", (row.status, row.error)
    assert row.output_text == "" and probe.persisted == []
    assert len(fake_llm.calls) == 1
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, world.chapters[0]["id"])).content == "第一章已完成"
        assert (await db.get(Chapter, world.chapters[1]["id"])).content == ""


@pytest.mark.parametrize("matching_verified_text", [False, True])
async def test_batch_prebuild_flush_rejects_nonempty_text(
    app, client, make_client, fake_llm, monkeypatch, matching_verified_text,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    probe = _PrebuildFlush(app, monkeypatch)
    try:
        task = await _start_at_second_flush(guest, world, fake_llm, probe)
        probe.hub.text = "未构建章节不得刷写正文"
        if matching_verified_text:
            probe.job.verified_text = probe.hub.text
    finally:
        probe.release.set()
    await _assert_second_chapter_untouched(app, world, fake_llm, task, probe)


@pytest.mark.parametrize("change", ["member", "owner_ai"])
async def test_batch_prebuild_empty_flush_rechecks_permission(
    app, client, make_client, fake_llm, monkeypatch, change,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    probe = _PrebuildFlush(app, monkeypatch)
    try:
        task = await _start_at_second_flush(guest, world, fake_llm, probe)
        # 不发送撤权通知；正在等待的真实 _flush 必须靠持久事实拒绝。
        async with app.state.sessionmaker() as db:
            values = {"role": "viewer"} if change == "member" else {"allow_owner_ai": False}
            changed = await db.execute(update(ProjectCollaborator).where(
                ProjectCollaborator.project_id == world.project["id"],
                ProjectCollaborator.user_id == world.guest["id"],
            ).values(**values))
            assert changed.rowcount == 1
            await db.commit()
    finally:
        probe.release.set()
    await _assert_second_chapter_untouched(app, world, fake_llm, task, probe)


async def test_batch_prebuild_empty_flush_rejects_changed_claim(
    app, client, make_client, fake_llm, monkeypatch,
):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    probe = _PrebuildFlush(app, monkeypatch)
    replacement = str(uuid4())
    try:
        task = await _start_at_second_flush(guest, world, fake_llm, probe)
        chapter_id = world.chapters[1]["id"]
        async with app.state.sessionmaker() as db:
            await db.execute(update(Chapter).where(Chapter.id == chapter_id).values(generation_claim_key=replacement))
            await db.commit()
        # 调用同一个真实刷新路径；失败不得刷库，也不能释放不属于原执行的章节占用。
        with pytest.raises(VisibilityPolicyError):
            await probe.flush(probe.hub, **probe.kwargs)
        async with app.state.sessionmaker() as db:
            chapter = await db.get(Chapter, chapter_id)
            assert chapter.generation_claim_key == replacement and chapter.content == ""
            assert (await db.get(Task, task["id"])).output_text == ""
            # 恢复本测试篡改后再放行原刷新，避免把未知 claim 的清理策略混入这个门禁测试。
            await db.execute(update(Chapter).where(
                Chapter.id == chapter_id, Chapter.generation_claim_key == replacement,
            ).values(generation_claim_key=probe.job.active_claim.claim_key))
            await db.commit()
    finally:
        probe.release.set()
    assert (await _terminal(app, task["id"])).status == "succeeded"
    assert probe.persisted == [("", "第 2/2 章 · 生成中", 1)]
