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
from app.models import Base, User  # noqa: E402
from app.services import authorization_service, retrieval_service  # noqa: E402
from app.services.mentions.matcher import cache as mention_cache  # noqa: E402
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


@pytest.fixture(autouse=True)
def isolated_conversation_locks(monkeypatch):
    """会话进程锁按会话 id 缓存；每个测试换新事件循环且 id 从 1 复用，须隔离注册表。"""
    from weakref import WeakValueDictionary

    from app.services.agent import service as agent_service

    monkeypatch.setattr(agent_service, "_locks", WeakValueDictionary())


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    """仅本测试的纯内存 provider 可走测试门禁；生产工厂仍按原规则验证。"""
    from app.services import ai_dispatch, collaboration_ai_guard

    fake = FakeLLM()
    original_factory_allowed = ai_dispatch._chapter_factory_allowed
    original_provider_allowed = ai_dispatch._chapter_provider_allowed
    original_guard_factory = collaboration_ai_guard._trusted_factory
    original_guard_provider = collaboration_ai_guard._trusted_provider

    def is_fake_factory(factory):
        return getattr(factory, "__self__", None) is fake and getattr(factory, "__func__", None) is FakeLLM.factory

    def factory_allowed(factory):
        return is_fake_factory(factory) or original_factory_allowed(factory)

    def provider_allowed(factory, provider_name, provider):
        if is_fake_factory(factory):
            return any(provider is item for item in fake._providers) and provider.uses_dispatch_hooks is True
        return original_provider_allowed(factory, provider_name, provider)

    def guard_factory_allowed(factory):
        return is_fake_factory(factory) or original_guard_factory(factory)

    def guard_provider_allowed(factory, provider, provider_name):
        if is_fake_factory(factory):
            return (any(provider is item for item in fake._providers)
                    and fake._provider_names.get(provider) == provider_name and provider.uses_dispatch_hooks is True)
        return original_guard_provider(factory, provider, provider_name)

    monkeypatch.setattr(ai_dispatch, "_chapter_factory_allowed", factory_allowed)
    monkeypatch.setattr(ai_dispatch, "_chapter_provider_allowed", provider_allowed)
    monkeypatch.setattr(collaboration_ai_guard, "_trusted_factory", guard_factory_allowed)
    monkeypatch.setattr(collaboration_ai_guard, "_trusted_provider", guard_provider_allowed)
    return fake


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "pg_isolated_schema: 该测试会改 DDL，PostgreSQL 夹具为其单独建删 schema，不使用进程共享 schema",
    )


@pytest.fixture(scope="session")
def pg_shared():
    """进程级共享 PostgreSQL schema：每个测试库只 create_all 一次，会话结束时 DROP。"""
    from tests.pg_support import SharedPostgres

    shared = SharedPostgres()
    try:
        yield shared
    finally:
        shared.close()


@pytest.fixture
async def pg_schema_lease(pg_shared, request):
    """本测试对共享 schema 的租用；收尾核对 DDL 指纹并 TRUNCATE 清空（标记 pg_isolated_schema 则独立建删）。"""
    from tests.pg_support import SchemaLease

    lease = SchemaLease(pg_shared, isolated=request.node.get_closest_marker("pg_isolated_schema") is not None)
    try:
        yield lease
    finally:
        await lease.release()


@pytest.fixture
def settings_overrides() -> dict:
    return {}


@pytest.fixture
async def app(tmp_path, fake_llm, settings_overrides):
    settings = make_settings(tmp_path, **settings_overrides)
    # 检索矩阵缓存是进程级的、按 (project_id, source) 区分：每个测试是新库、项目 id 从 1 重新开始，
    # 签名可能与上一个测试的缓存相同，所以每个测试前后都清空
    retrieval_service.invalidate()
    mention_cache.clear()
    application = create_app(settings, provider_factory=fake_llm.factory, start_task_engine=False)
    async with application.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 测试不触发 lifespan：显式启停任务引擎，结束前 stop，避免任务泄漏到下一个测试
    task_engine = application.state.task_engine
    await authorization_service.initialize(application.state.sessionmaker)
    await application.state.system_settings.load()
    await task_engine.start()
    try:
        yield application
    finally:
        await application.state.system_settings.close()
        await task_engine.stop()
        await application.state.engine.dispose()
        retrieval_service.invalidate()
        mention_cache.clear()


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


@pytest.fixture
async def recycle_admin(app, make_client):
    from tests.helpers import register

    client = make_client()
    user = await register(client, "recycle_manager")
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    return client


@pytest.fixture
async def system_admin(app, make_client):
    """独立系统设置管理员，不依赖自主注册开关。"""
    from app.schemas.auth import Credentials
    from app.services import auth_service

    async with app.state.sessionmaker() as db:
        await auth_service.create_user(
            db, Credentials(username="settings_admin", password="secret123"), is_admin=True,
        )
    client = make_client()
    result = await client.post(
        "/api/v1/auth/login", json={"username": "settings_admin", "password": "secret123"},
    )
    assert result.status_code == 200, result.text
    return client
