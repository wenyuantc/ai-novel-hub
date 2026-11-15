"""临时定位 PG 模型预检失败位置；不打印模型配置、凭证或其他数据库内容。"""

import traceback
from pathlib import Path

import httpx
import pytest

from app.services import project_ai_config
from tests.mcp_fakes import serve
from tests.test_mcp_settings_gate_postgresql import _admin, _proposal, _server

pytest_plugins = ("tests.test_mcp_settings_gate_postgresql",)


async def test_model_admission_debug(postgres_mcp_apps, fake_llm, monkeypatch):
    app, admin_app = postgres_mcp_apps
    caught = []
    original = project_ai_config._invalid_binding

    def observed():
        caught.append([(Path(frame.filename).name, frame.name, frame.lineno) for frame in traceback.extract_stack()
                       if Path(frame.filename).name in ("project_ai_config.py", "admission.py", "task_service.py")])
        return original()

    monkeypatch.setattr(project_ai_config, "_invalid_binding", observed)
    remote, *_ = _server("disabled_first")
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=admin_app), base_url="http://testserver") as admin,
        serve(remote) as remote_url,
    ):
        await _admin(admin_app, admin)
        try:
            await _proposal(app, owner, fake_llm, remote_url)
        except AssertionError:
            pytest.fail(f"预检拒绝位置（仅文件/函数/行号）：{caught}", pytrace=False)
