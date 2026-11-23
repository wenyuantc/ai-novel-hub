import os

from tests.helpers import TEST_FERNET_KEY, TEST_JWT_SECRET

# 必须在导入 app.main 之前设置：app.main 在导入时会按环境变量构建默认应用
os.environ["JWT_SECRET"] = TEST_JWT_SECRET
os.environ["ENCRYPTION_KEY"] = TEST_FERNET_KEY
os.environ["FRONTEND_DIST_DIR"] = "/nonexistent-frontend-dist"

from collections.abc import AsyncIterator  # noqa: E402
from contextlib import AsyncExitStack  # noqa: E402

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
    deployment = settings.model_copy(deep=True)
    application = create_app(settings, provider_factory=fake_llm.factory, start_task_engine=False)
    application.state.test_deployment = deployment
    async with application.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 测试不触发 lifespan：显式启停任务引擎，结束前 stop，避免任务泄漏到下一个测试
    task_engine = application.state.task_engine
    await authorization_service.initialize(application.state.sessionmaker)
    await application.state.system_settings.load()
    await task_engine.request_worker.start()
    try:
        await task_engine.start()
        yield application
    finally:
        await task_engine.stop()
        await task_engine.request_worker.stop()
        await application.state.system_settings.close()
        await application.state.engine.dispose()
        retrieval_service.invalidate()
        mention_cache.clear()
        _drop_mapper_statement_caches()


def _drop_mapper_statement_caches() -> None:
    """清空 ORM 映射器的写语句编译缓存：它是进程级的，键含方言对象。

    每个测试新建引擎（新方言），flush 编译出的 INSERT/UPDATE 连同方言、类型副本会留在映射器缓存里，
    引擎销毁后也不释放。全量运行中堆随用例数线性增长（约 1700 对象/用例），全量 GC 单次暂停
    从亚秒涨到数十秒，带固定等待预算的用例随机超时。生产进程只有一个引擎，不受影响。
    """
    for mapper in Base.registry.mappers:
        mapper._compiled_cache.clear()


@pytest.fixture
async def peer_app_factory(app, fake_llm):
    """共享已建好的数据库，每个对端拥有独立的连接池、任务引擎、设置和运行时。"""
    async with AsyncExitStack() as stack:
        async def new_peer():
            peer = create_app(
                app.state.test_deployment.model_copy(deep=True), provider_factory=fake_llm.factory,
                start_task_engine=False,
            )
            stack.push_async_callback(peer.state.engine.dispose)
            stack.push_async_callback(peer.state.system_settings.close)
            stack.push_async_callback(peer.state.task_engine.request_worker.stop)
            stack.push_async_callback(peer.state.task_engine.stop)
            await authorization_service.initialize(peer.state.sessionmaker)
            await peer.state.system_settings.load()
            await peer.state.task_engine.request_worker.start()
            return peer

        yield new_peer


@pytest.fixture
async def second_app(peer_app_factory):
    """调用方自行启动任务引擎，以便在真实任务已经运行之后再启动对端。"""
    return await peer_app_factory()


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
