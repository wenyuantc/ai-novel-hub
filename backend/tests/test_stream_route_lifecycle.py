"""动态测试路由不能让框架的 callable 缓存持有已关闭应用。"""

import gc
import weakref
from contextlib import AsyncExitStack

import httpx
import pytest
from fastapi.responses import PlainTextResponse

from app.main import create_app
from tests.helpers import make_settings
from tests.stream_helpers import mount_stream_response
from tests.test_collaboration_stream_replay_e5b import _mount as mount_replay
from tests.test_collaboration_stream_running_e5c1 import _mount as mount_running
from tests.test_entity_ai_execution_identity import _install_guarded_entity_stream as mount_entity


@pytest.mark.parametrize("kind", ["running", "replay", "entity"])
async def test_stream_routes_release_closed_applications(tmp_path, kind):
    async def closed_application(label):
        application = create_app(make_settings(tmp_path / label), start_task_engine=False)
        references = {name: weakref.ref(value) for name, value in (
            (f"{label}.app", application),
            (f"{label}.task_engine", application.state.task_engine),
            (f"{label}.engine", application.state.engine),
        )}
        try:
            if kind == "running":
                mount_running(application)
            elif kind == "replay":
                mount_replay(application)
            else:
                mount_entity(application, 1, {"id": 1, "task_key": label})
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://testserver",
            ) as client:
                assert (await client.get("/healthz")).status_code == 200
        finally:
            await application.state.system_settings.close()
            await application.state.task_engine.stop()
            await application.state.engine.dispose()
        return references

    references = {**await closed_application("first"), **await closed_application("second")}
    # 仅在所有客户端及资源关闭后回收；不清 FastAPI 缓存来掩盖强引用。
    gc.collect()
    assert [name for name, reference in references.items() if reference() is not None] == []


async def test_stream_response_factories_are_isolated_and_released(tmp_path):
    def response_factory(application, suffix):
        async def response():
            return PlainTextResponse(f"{application.state.test_label}:{suffix}")
        return response

    async def closed_applications():
        references, clients = [], []
        async with AsyncExitStack() as cleanup:
            for label in ("first", "second"):
                application = create_app(make_settings(tmp_path / label), start_task_engine=False)
                application.state.test_label = label
                references.extend(weakref.ref(value) for value in (
                    application, application.state.task_engine, application.state.engine,
                ))
                cleanup.push_async_callback(application.state.engine.dispose)
                cleanup.push_async_callback(application.state.task_engine.stop)
                cleanup.push_async_callback(application.state.system_settings.close)
                for suffix in ("one", "two"):
                    mount_stream_response(application, f"/_test/{suffix}", response_factory(application, suffix))
                clients.append(await cleanup.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application, root_path=f"/{label}"),
                    base_url=f"http://testserver/{label}/",
                )))
            # 两应用共用路由处理器，仍须按当前 app 和匹配的路由取到各自响应工厂。
            for label, client in zip(("first", "second"), clients, strict=True):
                for suffix in ("two", "one"):
                    response = await client.get(f"_test/{suffix}")
                    assert response.status_code == 200
                    assert response.text == f"{label}:{suffix}"
        return references

    references = await closed_applications()
    gc.collect()
    assert all(reference() is None for reference in references)
