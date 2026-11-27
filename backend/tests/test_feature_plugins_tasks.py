"""插件的通用受理、领取、取消与本人任务接口。

受理调用点核对：task_service 接受可扩展的 kind；analysis_service / kb_service /
book_import.{service,reverse} / rich_document_review 操作固定核心类型；
project_ai_config 和 agent.{service,tools.tasks} 的受理检查不直接插入插件任务。
"""

import asyncio

import pytest

from app import plugins
from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.models import FeaturePluginState, Task
from app.plugins import PluginManifest
from app.services import task_service
from app.services.task_engine.registry import TaskSpec
from tests.conftest import _new_client
from tests.helpers import register, wait_task, wait_until
from tests.schema_helpers import isolated_worker_database


class SampleHandler:
    def __init__(self, kind):
        self.kinds = (kind,)
        self.started = []
        self.finished = []
        self.release = asyncio.Event()

    async def validate_and_claim(self, db, ctx, *, user_id, kind, params):
        return TaskSpec(None, None, None, params["mode"], {"request": params}, serial_key=kind)

    async def build_job(self, ctx, task, **options):
        return task

    async def run(self, task, ctx):
        self.started.append(task.id)
        if task.title == "wait":
            await self.release.wait()
            yield "stage", {"stage": "继续"}
        if task.title == "pause":
            yield "paused", {}
        else:
            self.finished.append(task.id)
            yield "done", {"id": task.id}

    def progress(self, task, text):
        return 0

    async def on_abort(self, ctx, task, **options):
        pass


class IdentityAwareSampleHandler(SampleHandler):
    execution_identity_aware = True

    async def verify_event(self, db, row, *, execution, **options):
        assert row.execution_key == execution.execution_key

    async def verify_finalization(self, db, row, *, execution, **options):
        assert row.execution_key == execution.execution_key


@pytest.fixture
def handlers(request):
    handler_type = getattr(request, "param", SampleHandler)
    return [handler_type("sample_one.work"), SampleHandler("samplexone.work")]


@pytest.fixture
async def settings_overrides(monkeypatch, handlers):
    manifests = tuple(PluginManifest(h.kinds[0].split(".")[0], "示例", "", "1", task_handlers=(h,)) for h in handlers)
    monkeypatch.setattr(plugins, "REGISTERED", manifests)
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


@pytest.fixture
async def create(client, app):
    # 独立受理入口使测试可以观察服务层门禁；插件路由自身的 403 无法证明后台任务也有门禁。
    @app.post("/test/create-plugin-task")
    async def new(kind: str, mode: str, user: CurrentUser, db: DbDep, engine: TaskEngineDep):
        return await task_service.create_task(db, engine, user.id, kind, {"mode": mode})

    await register(client)

    async def submit(mode="done", kind="sample_one.work"):
        return await client.post("/test/create-plugin-task", params={"mode": mode, "kind": kind})

    return submit


async def switch(admin, enabled, revision, plugin_id="sample_one"):
    response = await admin.patch(f"/api/v1/admin/plugins/{plugin_id}", json={"revision": revision, "enabled": enabled})
    assert response.status_code == 200, response.text


async def test_admit_and_read_plugin_tasks(client, create, system_admin):
    closed = await create()
    assert closed.status_code == 403 and closed.json()["code"] == "plugin_disabled"
    await switch(system_admin, True, 0)
    created = await create()
    assert created.status_code == 200, created.text
    task = await wait_task(client, created.json())
    assert task["status"] == "succeeded" and task["kind"] == "sample_one.work"
    listing = await client.get("/api/v1/tasks", params={"kind": task["kind"]})
    assert [row["id"] for row in listing.json()] == [task["id"]]
    assert (await client.get("/api/v1/tasks", params={"kind": "invented.kind"})).status_code == 422


async def test_disable_and_immediately_reenable_cancels_only_original_rounds(
    second_app, client, create, handlers, system_admin,
):
    await switch(system_admin, True, 0)
    await switch(system_admin, True, 0, "samplexone")
    active = (await create("wait")).json()
    await wait_until(lambda: active["id"] in handlers[0].started)
    queued = (await create()).json()
    other = (await create(kind="samplexone.work")).json()
    async with _new_client(second_app) as remote:
        remote.cookies.update(system_admin.cookies)
        await switch(remote, False, 1)
        await switch(remote, True, 2)
    for item in [active, queued]:
        assert (await wait_task(client, item, within=15))["status"] == "cancelled"
    assert queued["id"] not in handlers[0].started
    assert active["id"] not in handlers[0].finished
    assert (await wait_task(client, other))["status"] == "succeeded"
    await switch(system_admin, False, 3)
    retry = await client.post(f"/api/v1/tasks/{active['id']}/retry", params={"task_key": active["task_key"]})
    assert retry.status_code == 403 and retry.json()["code"] == "plugin_disabled"


async def test_paused_plugin_round_is_cancelled(client, create, system_admin):
    await switch(system_admin, True, 0)
    created = (await create("pause")).json()
    await wait_task(client, created, statuses={"paused"})
    await switch(system_admin, False, 1)
    assert (await wait_task(client, created, statuses={"cancelled"}))["status"] == "cancelled"


async def test_claim_and_resume_recheck_persisted_state(
    app, create, client, system_admin, handlers, paused_task_dispatch,
):
    await switch(system_admin, True, 0)
    queued = (await create()).json()
    paused = (await create()).json()
    # 模拟遗留的未取消任务，直接改变持久状态，独立验证领取与继续时的防线。
    async with app.state.sessionmaker() as db:
        (await db.get(FeaturePluginState, "sample_one")).enabled = False
        (await db.get(Task, paused["id"])).status = "paused"
        await db.commit()
    resume = await client.post(f"/api/v1/tasks/{paused['id']}/resume", params={"task_key": paused["task_key"]})
    assert resume.status_code == 403 and resume.json()["code"] == "plugin_disabled"
    paused_task_dispatch()
    assert (await wait_task(client, queued))["status"] == "cancelled"
    assert handlers[0].started == []


@pytest.mark.parametrize("handlers", [SampleHandler, IdentityAwareSampleHandler], indirect=True)
async def test_running_plugin_observes_disable_at_next_event(app, client, create, handlers, system_admin, monkeypatch):
    await switch(system_admin, True, 0)
    active = (await create("wait")).json()
    await wait_until(lambda: active["id"] in handlers[0].started)

    # 模拟跨实例唤醒晚于运行者的下一个事件。停用和重新启用都按真实请求提交。
    async def delayed_scan():
        pass

    engine = app.state.task_engine
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    monkeypatch.setattr(engine, "scan_admin_cancellations", delayed_scan)
    await switch(system_admin, False, 1)
    await switch(system_admin, True, 2)
    handlers[0].release.set()
    assert (await wait_task(client, active, within=15))["status"] == "cancelled"
    assert active["id"] not in handlers[0].finished
