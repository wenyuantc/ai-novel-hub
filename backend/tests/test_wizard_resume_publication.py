"""续写向导替换已有条目后的结果发布（2026-11-13 修复，方案 A）。

续写角色/世界观/职业向导并整体替换已有条目（或引起候选竞争）时，保存本身会推进 AI 历史代次（epoch）。
保存后的核验（done 事件、终态、提交确认、done 帧仅发布 guard）不能再拿续写来源 epoch 与当前 epoch 比较，
而是与构建戳 epoch 比较（保存事务内已核对二者相等），并另行要求保存事务内读取的新戳仍成立。
发送前路径仍与当前 epoch 比较；他人在保存后收紧可见性、或在构建后保存前改变 epoch，仍不能发布。
"""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.core.errors import ConflictError
from app.models import Career, Character, Task, WorldSetting
from app.services import setting_ai_visibility, wizard_service
from app.services.ai_dispatch import read_visibility_stamp
from app.services.collaboration_ai_guard import CollaborationSendBlocked, for_task
from app.services.task_engine import engine as engine_module
from app.services.task_engine.handlers.wizard import WizardHandler
from tests.helpers import Gate, wait_until
from tests.test_collaboration_entity_entry_e4g3b import _terminal, _world

NOTE = engine_module.WIZARD_RECEIPT_FINALIZED_NOTE
KINDS = ("characters", "world", "careers")
# 部分输出（失败来源）与续写尾部；拼接后为合法 JSON。
CASES = {
    "characters": ('[{"name":"续甲"},{"name":"续', ',{"name":"续乙"},{"name":"续丙"}]', ["续丙", "续乙", "续甲"]),
    "world": ('[{"title":"续山","content":"甲"},{"title":"续', ',{"title":"续海","content":"乙"}]', ["续山", "续海"]),
    "careers": ('{"careers":[{"name":"续剑修","kind":"main","stages":[{"name":"练气"}]},{"name":"续',
                ',{"name":"续丹修","kind":"main","stages":[{"name":"筑基"}]}]}', ["续丹修", "续剑修"]),
}
_MODELS = {"characters": (Character, Character.name), "world": (WorldSetting, WorldSetting.title),
           "careers": (Career, Career.name)}
OLD_NAMES = {"characters": ["乙", "甲"], "world": ["旧山门"], "careers": ["旧修"]}


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def _names(app, world, kind):
    model, column = _MODELS[kind]
    async with app.state.sessionmaker() as db:
        return sorted(await db.scalars(select(column).where(model.project_id == world.project["id"])))


async def _epoch(app, world) -> int:
    async with app.state.sessionmaker() as db:
        return (await read_visibility_stamp(db, world.project["id"])).epoch


async def _setup(client, make_client, kind):
    """作品已有该类条目（_world 自带角色甲、乙；世界观/职业由作者另建），续写会整体替换它们。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    if kind == "world":
        created = await client.post(f"{world.base}/world", json={"title": "旧山门", "content": "云海"})
        assert created.status_code == 201, created.text
    elif kind == "careers":
        created = await client.post(f"{world.base}/careers", json={"name": "旧修", "stages": [{"name": "一"}]})
        assert created.status_code == 201, created.text
    return guest, world


async def _source(app, actor_client, world, fake_llm, kind):
    partial = CASES[kind][0]
    fake_llm.push([partial, AIProviderError("模型调用失败：上游中断")])
    created = await actor_client.post(f"{world.base}/wizard/{kind}", json={})
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.output_text == partial
    return row


def _capture_jobs(monkeypatch) -> dict[int, wizard_service.WizardJob]:
    jobs: dict[int, wizard_service.WizardJob] = {}
    original = WizardHandler.build_job

    async def build_job(self, ctx, task, *, execution):
        job = await original(self, ctx, task, execution=execution)
        jobs[task.id] = job
        return job

    monkeypatch.setattr(WizardHandler, "build_job", build_job)
    return jobs


async def _run_resume(app, actor_client, world, fake_llm, source, kind, *, before_release=None):
    """在模型流中 Gate 处订阅实时事件，放行后收集到终止帧；返回 (任务, 事件, 终态行)。"""
    gate = Gate()
    fake_llm.push([gate, CASES[kind][1]])
    created = await actor_client.post(f"{world.base}/wizard/{kind}", json={
        "continue_from_task_id": source.id, "continue_from_task_key": source.statistics_key,
    })
    assert created.status_code == 202, created.text
    task = created.json()
    await gate.wait_reached()
    engine = app.state.task_engine
    events: list[tuple[str, dict]] = []

    async def collect():
        async for event, data in engine.subscribe(task["id"]):
            events.append((event, data))

    subscriber = asyncio.create_task(collect())
    try:
        await wait_until(lambda: bool(events) and events[0][0] == "snapshot")
        if before_release is not None:
            await before_release()
        gate.release()
        await asyncio.wait_for(subscriber, 15)
    finally:
        gate.release()
        if not subscriber.done():
            subscriber.cancel()
    return task, events, await _terminal(app, task["id"])


# ---------------------------------------------------------------- 正例


@pytest.mark.parametrize("actor", ["owner", "editor"])
@pytest.mark.parametrize("kind", KINDS)
async def test_resume_replacing_existing_entries_publishes_result(
    app, client, make_client, fake_llm, monkeypatch, kind, actor,
):
    guest, world = await _setup(client, make_client, kind)
    actor_client = client if actor == "owner" else guest
    assert await _names(app, world, kind) == OLD_NAMES[kind]
    source = await _source(app, actor_client, world, fake_llm, kind)
    jobs = _capture_jobs(monkeypatch)
    before = await _epoch(app, world)
    task, events, row = await _run_resume(app, actor_client, world, fake_llm, source, kind)
    assert row.status == "succeeded" and not row.error, row.error
    assert events[-1][0] == "done", events[-1]
    assert events[-1][1]["result"]["kind"] == kind
    assert row.result["kind"] == kind and row.retry_of_id == source.id
    assert await _names(app, world, kind) == CASES[kind][2]
    # 承重：本任务自己的整体替换推进了 epoch，来源 epoch 已不等于当前 epoch，结果仍须发布。
    job = jobs[task["id"]]
    assert source.input["ai_history_epoch"] == before == job.resume_epoch
    assert await _epoch(app, world) == job.visibility_stamp.epoch > before


# ---------------------------------------------------------------- 反例


@pytest.mark.parametrize("actor", ["owner", "editor"])
async def test_resume_tightened_after_save_is_not_published(
    app, client, make_client, fake_llm, monkeypatch, actor,
):
    """保存提交后、done 事件核验前，作者另行收紧可见性：保存后的新戳不再成立 → 结果不发布。

    钩在保存函数返回（已提交、不持锁）之后，而不是 verify_event 内部（那里已持作品/可见性屏障）。"""
    guest, world = await _setup(client, make_client, "characters")
    actor_client = client if actor == "owner" else guest
    source = await _source(app, actor_client, world, fake_llm, "characters")
    reached, proceed = asyncio.Event(), asyncio.Event()
    original = wizard_service.save_wizard_result

    async def save_wizard_result(*args, **kwargs):
        payload = await original(*args, **kwargs)
        reached.set()
        await proceed.wait()
        return payload

    monkeypatch.setattr(wizard_service, "save_wizard_result", save_wizard_result)

    async def tighten_after_save():
        await asyncio.wait_for(reached.wait(), 15)
        async with app.state.sessionmaker() as db:
            await setting_ai_visibility.bump_visibility_revision(db, world.project["id"], tighten=True)
            await db.commit()
        proceed.set()

    runner = asyncio.create_task(tighten_after_save())
    try:
        _task, events, row = await _run_resume(app, actor_client, world, fake_llm, source, "characters")
        await asyncio.wait_for(runner, 15)
    finally:
        proceed.set()
        if not runner.done():
            runner.cancel()
    # E5-d：收据收尾以带任务快照、不带结果的「未发布」done 结束；结果（条目）不进入任何帧。
    assert events[-1][0] == "done", events
    assert events[-1][1]["result"] is None and events[-1][1]["unpublished"] is True, events[-1]
    assert events[-1][1]["detail"] == engine_module.WIZARD_RECEIPT_FINALIZED_DETAIL
    assert events[-1][1]["task"]["status"] == "succeeded" and events[-1][1]["task"]["error"] == NOTE
    assert not any(event == "done" and data.get("result") is not None for event, data in events), events
    assert row.status == "succeeded" and row.error == NOTE
    # 内容已随收据提交（不撤回），只是不发布。
    assert await _names(app, world, "characters") == CASES["characters"][2]


@pytest.mark.parametrize("actor", ["owner", "editor"])
async def test_resume_epoch_changed_before_save_is_rejected(app, client, make_client, fake_llm, actor):
    """构建之后、保存之前他人推进 epoch：保存事务的构建戳核验失败，内容不变、不发布。"""
    guest, world = await _setup(client, make_client, "characters")
    actor_client = client if actor == "owner" else guest
    source = await _source(app, actor_client, world, fake_llm, "characters")

    async def tighten():
        async with app.state.sessionmaker() as db:
            await setting_ai_visibility.bump_visibility_revision(db, world.project["id"], tighten=True)
            await db.commit()

    _task, events, row = await _run_resume(app, actor_client, world, fake_llm, source, "characters",
                                           before_release=tighten)
    assert all(event != "done" for event, _ in events), events
    assert events[-1][0] == "error"
    assert row.status == "failed"
    assert await _names(app, world, "characters") == OLD_NAMES["characters"]


async def test_post_save_checks_bind_build_epoch(app, client, make_client, fake_llm, monkeypatch):
    """保存后的核验只接受构建戳 epoch；改动该 epoch（或改回与当前 epoch 比较）即拒绝。"""
    guest, world = await _setup(client, make_client, "characters")
    source = await _source(app, guest, world, fake_llm, "characters")
    jobs = _capture_jobs(monkeypatch)
    task, _events, row = await _run_resume(app, guest, world, fake_llm, source, "characters")
    assert row.status == "succeeded" and not row.error
    job = jobs[task["id"]]
    binding, epoch = job.resume_binding, job.resume_epoch
    assert job.visibility_stamp.epoch > epoch
    async with app.state.sessionmaker() as db:
        await wizard_service.verify_resume_source(db, binding, expected_epoch=epoch)
        # 发送前语义不变：与当前 epoch 比较，保存后必然不成立。
        # （verify_resume_source 把 epoch 不符统一失败关闭为 wizard_resume_changed。）
        with pytest.raises(wizard_service.WizardResumeChanged):
            await wizard_service.verify_resume_source(db, binding)
        for wrong in (epoch + 1, epoch + 2, job.visibility_stamp.epoch):
            with pytest.raises(wizard_service.WizardResumeChanged):
                await wizard_service.verify_resume_source(db, binding, expected_epoch=wrong)
        # 底层 load_resume_source 保留原错误码。
        with pytest.raises(ConflictError) as raw:
            await wizard_service.load_resume_source(
                db, user_id=binding.actor_id, project_id=binding.project_id, kind="characters",
                task_id=binding.task_id, task_key=binding.task_key, grant_identity=binding.grant_identity,
                expected_epoch=epoch + 1,
            )
        assert raw.value.extra["code"] == "ai_visibility_changed"
        for invalid in (-1, True, "1"):
            with pytest.raises(wizard_service.WizardResumeChanged):
                await wizard_service.verify_resume_source(db, binding, expected_epoch=invalid)
        await db.rollback()
    # done 事件核验：用 job 的构建戳 epoch；被改动则拒绝。
    handler = WizardHandler()
    async with app.state.sessionmaker() as db:
        db_task = await db.get(Task, task["id"])
        done = job.execution.checkpoint["_wizard_completion"]["result"]
        kwargs = {"job": job, "execution": job.pending.original, "current_execution": job.execution,
                  "event": "done", "data": done}
        await handler.verify_event(db, db_task, **kwargs)
        job.resume_epoch = epoch + 1
        with pytest.raises(wizard_service.WizardResumeChanged):
            await handler.verify_event(db, db_task, **kwargs)
        job.resume_epoch = None
        with pytest.raises(wizard_service.WizardResumeChanged):
            await handler.verify_event(db, db_task, **kwargs)
        job.resume_epoch = epoch
        await db.rollback()
    # done 帧仅发布 guard：携带构建戳 epoch；被改动则续写来源核验拒绝。
    guard = handler.stream_guard(job, execution=job.pending.original, current_execution=job.execution,
                                 event="done", data=done)
    assert guard._publication_only and guard._resume_epoch == epoch
    async with app.state.sessionmaker() as db:
        await guard._verify_resume(db)
        with pytest.raises(wizard_service.WizardResumeChanged):
            await replace(guard, _resume_epoch=epoch + 1)._verify_resume(db)
        await db.rollback()
    # 构造约束：续写的仅发布 guard 必须带 epoch，且不晚于新戳；普通 guard 不能带。
    kwargs = {"cipher": guard._cipher, "allow_private": guard._allow_private, "resume": binding}
    for bad in ({"publication_only": True}, {"publication_only": True, "resume_epoch": guard._stamp.epoch + 1},
                {"publication_only": True, "resume_epoch": -1}, {"publication_only": True, "resume_epoch": True},
                {"resume_epoch": epoch}):
        with pytest.raises(CollaborationSendBlocked):
            for_task(job.execution if bad.get("publication_only") else job.pending.active, guard._binding,
                     guard._stamp if bad.get("publication_only") else job.guard._stamp, **kwargs, **bad)
    job.publication_guard = None
    job.resume_epoch = None
    with pytest.raises(CollaborationSendBlocked):
        handler.stream_guard(job, execution=job.pending.original, current_execution=job.execution,
                             event="done", data=done)


@pytest.mark.parametrize("actor", ["owner", "editor"])
async def test_resume_commit_ack_lost_confirms_with_build_epoch(
    app, client, make_client, fake_llm, monkeypatch, actor,
):
    """保存已真实提交但确认丢失：confirm_wizard_commit 以构建戳 epoch 核对续写来源（保存已推进当前 epoch），
    并要求保存事务内新戳仍成立；确认成功后照常发布 done。"""
    from sqlalchemy.ext.asyncio import AsyncSession

    guest, world = await _setup(client, make_client, "characters")
    actor_client = client if actor == "owner" else guest
    source = await _source(app, actor_client, world, fake_llm, "characters")
    jobs = _capture_jobs(monkeypatch)
    original_commit, lost = AsyncSession.commit, []

    async def lose_ack(db):
        if not lost and any(isinstance(row, Character) and row.name == "续丙" for row in db.identity_map.values()):
            lost.append(True)
            await original_commit(db)
            raise RuntimeError("模拟提交确认丢失")
        await original_commit(db)

    confirmed: list[bool] = []
    original_confirm = wizard_service.confirm_wizard_commit

    async def confirm(*args, **kwargs):
        confirmed.append(await original_confirm(*args, **kwargs))
        return confirmed[-1]

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    monkeypatch.setattr(wizard_service, "confirm_wizard_commit", confirm)
    before = await _epoch(app, world)
    task, events, row = await _run_resume(app, actor_client, world, fake_llm, source, "characters")
    assert lost == [True] and confirmed == [True]
    assert row.status == "succeeded" and not row.error, row.error
    assert events[-1][0] == "done", events[-1]
    assert await _names(app, world, "characters") == CASES["characters"][2]
    job = jobs[task["id"]]
    assert job.resume_epoch == before < job.visibility_stamp.epoch == await _epoch(app, world)
