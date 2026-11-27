"""外部任务的恢复契约：原执行者消失或正常关停时，从持久断点交给下一执行者。"""

import asyncio
import multiprocessing

import pytest
from sqlalchemy import select

from app import plugins
from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.main import create_app
from app.models import Task, TaskExecutionSlot, TaskStatistic
from app.services import authorization_service, task_service
from app.services.task_engine.registry import TaskSpec
from tests.helpers import make_settings, register, wait_task, wait_until


class ExternalHandler:
    kinds = ("sample.external",)
    resume_after_interrupt = True

    def __init__(self, directory):
        self.events = directory / "provider-events"

    def record(self, event):
        with self.events.open("a") as log:
            print(event, file=log)

    async def validate_and_claim(self, db, ctx, *, user_id, kind, params):
        return TaskSpec(None, None, None, "测试外部任务", {"request": params})

    async def build_job(self, ctx, task):
        return task

    async def run(self, task, ctx):
        if not task.checkpoint:
            self.record("submit")
            async with ctx.sessionmaker() as db:
                (await db.get(Task, task.id)).checkpoint = {"external_id": "provider-42"}
                await db.commit()
            self.record("wait")
            await asyncio.Event().wait()
        self.record(f"poll {task.checkpoint['external_id']} {task.worker_key} {task.execution_key}")
        yield "done", {"external_id": task.checkpoint["external_id"]}

    def progress(self, task, text):
        return 0

    async def on_abort(self, ctx, task):
        self.record("abort")


def manifest(directory):
    return plugins.PluginManifest("sample", "测试外部任务", "", "1", task_handlers=(ExternalHandler(directory),))


@pytest.fixture
def settings_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "REGISTERED", (manifest(tmp_path),))
    return {}


async def enqueue(app, client, admin):
    @app.post("/test/external")
    async def new_task(user: CurrentUser, db: DbDep, engine: TaskEngineDep):
        return await task_service.create_task(db, engine, user.id, "sample.external", {})

    await register(client)
    reply = await admin.patch("/api/v1/admin/plugins/sample", json={"revision": 0, "enabled": True})
    assert reply.status_code == 200, reply.text
    created = await client.post("/test/external")
    assert created.status_code == 200, created.text
    return created.json()


async def waiting(directory):
    log = directory / "provider-events"
    await wait_until(lambda: log.exists() and "wait" in log.read_text().splitlines(), within=30)
    return log


def run_original_process(directory):
    async def run():
        app = create_app(make_settings(directory), extra_plugins=(manifest(directory),), start_task_engine=False)
        await authorization_service.initialize(app.state.sessionmaker)
        await app.state.system_settings.load()
        await app.state.task_engine.start()
        await asyncio.Event().wait()

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["shutdown", "killed"])
async def test_original_round_is_reclaimed_by_new_execution(
    tmp_path, app, second_app, client, system_admin, stop,
):
    first = app.state.task_engine
    peer = second_app.state.task_engine
    if stop == "killed":
        await first.stop()
    task = await enqueue(app, client, system_admin)
    process = None
    try:
        if stop == "killed":
            process = multiprocessing.get_context("spawn").Process(target=run_original_process, args=(tmp_path,))
            process.start()
        log = await waiting(tmp_path)
        async with app.state.sessionmaker() as db:
            old = await db.get(Task, task["id"])
            assert old.status == "running" and old.execution_key and old.worker_key
        if process:
            process.kill()
            await asyncio.to_thread(process.join, 10)
            assert process.exitcode
            # 测试实例缩短发现已结束进程的租约窗口，使用正常围栏扫描和领取路径。
            peer.lease_seconds = 3
            peer.lease_safety_seconds = 0.5
            peer.heartbeat_interval = 0.3
            peer.poll_interval = 0.1
        else:
            await first.stop()
        await peer.start()
        result = await wait_task(client, task, within=20)
        assert result["status"] == "succeeded", result
        submit, wait, poll = log.read_text().splitlines()
        assert (submit, wait) == ("submit", "wait")
        verb, external_id, worker, execution = poll.split()
        assert (verb, external_id, worker) == ("poll", "provider-42", peer.lease.worker_key)
        assert worker != old.worker_key and execution != old.execution_key
        await wait_until(lambda: task["id"] not in peer._running)
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task["id"])
            assert row.result == {"external_id": "provider-42"}
            assert row.queued_at == old.queued_at and row.statistics_key == old.statistics_key
            assert (await db.get(TaskStatistic, row.statistics_key)).status == "succeeded"
            slot = select(TaskExecutionSlot.execution_key).where(TaskExecutionSlot.task_id == row.id)
            assert await db.scalar(slot) is None
    finally:
        if process:
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 10)
            process.close()


@pytest.mark.parametrize("action", ["cancel", "disable"])
async def test_cancellation_takes_precedence_over_requeue(tmp_path, app, second_app, client, system_admin, action):
    task = await enqueue(app, client, system_admin)
    log = await waiting(tmp_path)
    await app.state.task_engine.stop()
    if action == "cancel":
        reply = await client.post(f"/api/v1/tasks/{task['id']}/cancel", params={"task_key": task["task_key"]})
    else:
        reply = await system_admin.patch("/api/v1/admin/plugins/sample", json={"revision": 1, "enabled": False})
    assert reply.status_code == 200, reply.text
    await second_app.state.task_engine.start()
    assert (await wait_task(client, task, within=15))["status"] == "cancelled"
    assert log.read_text().splitlines() == ["submit", "wait", "abort"]


async def test_stage_events_accept_optional_bounded_progress(app, monkeypatch):
    from types import SimpleNamespace

    from app.services.task_engine.hub import TaskHub

    engine = app.state.task_engine
    hub = TaskHub(123, status="running")
    task = Task(id=123, kind="sample.external", worker_key="source")
    observed = []

    class Handler:
        async def run(self, job, ctx):
            yield "stage", {"stage": "正在提交"}
            observed.append(hub.progress)
            for progress in (58, True, 100, -1, "70"):
                yield "stage", {"stage": "等待生成", "progress": progress}
                observed.append(hub.progress)
            yield "done", {}

    async def owns_round(*args, **kwargs):
        return True

    monkeypatch.setattr(engine, "_owns_task_round", owns_round)
    assert await engine._consume(task, Handler(), hub, None, execution=SimpleNamespace(worker_key="source")) == ""
    assert observed == [0, 58, 58, 58, 58, 58]
    assert hub.stage == "等待生成"


@pytest.mark.parametrize("status_aware", [False, True])
async def test_execution_abort_reason_is_opt_in(app, client, system_admin, monkeypatch, status_aware):
    handler = app.state.task_engine.feature_plugins.handler("sample.external")
    calls = []

    async def failing(job, ctx):
        yield "error", {"detail": "测试失败"}

    async def with_status(ctx, task, *, terminal_status):
        calls.append(terminal_status)

    async def legacy(ctx, task):
        calls.append("legacy")

    monkeypatch.setattr(handler, "run", failing)
    monkeypatch.setattr(handler, "abort_status_aware", status_aware, raising=False)
    monkeypatch.setattr(handler, "on_abort", with_status if status_aware else legacy)
    task = await enqueue(app, client, system_admin)
    result = await wait_task(client, task)
    assert result["status"] == "failed" and result["error"] == "测试失败"
    assert calls == (["failed"] if status_aware else ["legacy"])
