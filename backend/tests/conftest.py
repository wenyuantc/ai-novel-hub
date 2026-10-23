import os

from tests.helpers import TEST_FERNET_KEY, TEST_JWT_SECRET

# 必须在导入 app.main 之前设置：app.main 在导入时会按环境变量构建默认应用
os.environ["JWT_SECRET"] = TEST_JWT_SECRET
os.environ["ENCRYPTION_KEY"] = TEST_FERNET_KEY
os.environ["FRONTEND_DIST_DIR"] = "/nonexistent-frontend-dist"

from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.ai import endpoint_guard  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402
from tests.helpers import FakeLLM, make_settings  # noqa: E402

FAKE_DNS = {
    "internal.example.test": ["10.0.0.5"],
    "rebind.example.test": ["93.184.216.34", "127.0.0.1"],
    "metadata.example.test": ["169.254.169.254"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """测试不依赖真实 DNS：未列出的域名一律解析为公网地址，无法解析的域名抛 OSError。"""

    async def _resolve(host: str, port: int) -> list[str]:
        if host == "nxdomain.example.test":
            raise OSError("Name or service not known")
        return FAKE_DNS.get(host, ["93.184.216.34"])

    monkeypatch.setattr(endpoint_guard, "_resolve", _resolve)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def settings_overrides() -> dict:
    return {}


@pytest.fixture
async def app(tmp_path, fake_llm, settings_overrides):
    settings = make_settings(tmp_path, **settings_overrides)
    application = create_app(settings, provider_factory=fake_llm.factory, start_task_engine=False)
    async with application.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 测试不触发 lifespan：显式启停任务引擎，结束前 stop，避免任务泄漏到下一个测试
    task_engine = application.state.task_engine
    await task_engine.start()
    try:
        yield application
    finally:
        await task_engine.stop()
        await application.state.engine.dispose()


def _new_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with _new_client(app) as c:
        yield c


@pytest.fixture
async def make_client(app):
    """创建额外的独立客户端（独立 Cookie），用于多用户隔离测试。"""
    clients: list[httpx.AsyncClient] = []

    def _make() -> httpx.AsyncClient:
        c = _new_client(app)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()
