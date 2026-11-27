"""AC8：两个独立进程共享临时数据库，B 的下一次请求反映 A 已提交的开关。"""

import asyncio
import multiprocessing

import pytest
from fastapi import APIRouter

from app import plugins
from app.main import create_app
from tests.conftest import _new_client
from tests.helpers import make_settings

router = APIRouter(prefix="/plugins/example")


@router.get("/probe")
async def probe():
    return {"available": True}


SAMPLE = plugins.PluginManifest("example", "示例插件", "双实例开关验证", "1", (router,))


@pytest.fixture
def settings_overrides(monkeypatch):
    monkeypatch.setattr(plugins, "REGISTERED", (SAMPLE,))
    return {}


def _peer_process(tmp_path, pipe):
    async def serve():
        app = create_app(make_settings(tmp_path), extra_plugins=(SAMPLE,), start_task_engine=False)
        try:
            async with _new_client(app) as client:
                while await asyncio.to_thread(pipe.recv):
                    result = await client.get("/api/v1/plugins/example/probe")
                    pipe.send((result.status_code, result.json()))
        finally:
            await app.state.engine.dispose()

    try:
        asyncio.run(serve())
    finally:
        pipe.close()


async def test_next_request_on_peer_observes_enable_and_disable(tmp_path, system_admin):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    peer = context.Process(target=_peer_process, args=(tmp_path, child))
    peer.start()
    child.close()

    async def probe_peer():
        parent.send(True)
        assert await asyncio.to_thread(parent.poll, 30), "对端未返回插件请求结果"
        return parent.recv()

    try:
        status, detail = await probe_peer()
        assert status == 403, detail
        url = "/api/v1/admin/plugins/example"
        enabled = await system_admin.patch(url, json={"revision": 0, "enabled": True})
        assert enabled.status_code == 200, enabled.text
        assert await probe_peer() == (200, {"available": True})
        disabled = await system_admin.patch(url, json={"revision": enabled.json()["revision"], "enabled": False})
        assert disabled.status_code == 200, disabled.text
        status, detail = await probe_peer()
        assert status == 403, detail
        parent.send(False)
        await asyncio.to_thread(peer.join, 10)
        assert peer.exitcode == 0
    finally:
        if peer.is_alive():
            peer.kill()
            await asyncio.to_thread(peer.join)
        parent.close()
        peer.close()
