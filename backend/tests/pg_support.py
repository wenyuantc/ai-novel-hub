"""PostgreSQL 测试共享环境：每个 pytest 进程每个测试库只建一次 schema，测试之间清空数据。

- schema 名带随机后缀，进程结束时 DROP；表与数据按 schema 隔离。
- **同一数据库不要并发运行多个 pytest 进程**：产品的 PostgreSQL advisory lock（作品可见性发送门禁、
  用户模型选择、MCP 设置门禁）以 ``(命名空间, 项目/用户 id)`` 为键，作用于整个数据库而不区分 schema；
  各进程的 id 都从 1 开始，会互相争抢并表现为偶发 ``ai_visibility_busy`` / ``collaboration_busy`` 等 409。
  需要并行时请为每个进程使用独立的 database。这是测试环境限制，不得为此改变产品锁语义。
- 每个测试开始前与结束后都清空全部业务表（按依赖逆序 DELETE + 序列重置，失败回退
  ``TRUNCATE … RESTART IDENTITY CASCADE``）并重放 create_all 的预置数据，
  id 与原「每测试新建 schema」一致地从 1 开始；create_all 本身不建 alembic_version，这里也不建。
- 引擎按测试新建并在当前事件循环内释放，绝不跨 loop 复用 asyncpg 连接。
- 测试结束后核对 schema 的 DDL 指纹（表/列/约束/索引/触发器/函数），被改动即判该测试失败并丢弃重建。
- 会改 DDL 的测试用 ``@pytest.mark.pg_isolated_schema`` 取得独立 schema（迁移测试本身另建独立 schema）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.schema_helpers import create_isolated_schema, seed_isolated_schema

COLLAB_URL_ENV = "AINOVEL_COLLAB_POSTGRES_TEST_URL"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

_FINGERPRINT_SQL = text("""
SELECT 'rel', c.relname, c.relkind::text, ''
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema
UNION ALL
SELECT 'col', c.relname, a.attname,
       format_type(a.atttypid, a.atttypmod) || '|' || a.attnotnull::text || '|' ||
       coalesce(pg_get_expr(d.adbin, d.adrelid), '')
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
 WHERE n.nspname = :schema AND a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
UNION ALL
SELECT 'con', coalesce(c.relname, ''), con.conname,
       pg_get_constraintdef(con.oid) || '|' || con.condeferrable::text || con.condeferred::text
  FROM pg_constraint con
  JOIN pg_namespace n ON n.oid = con.connamespace
  LEFT JOIN pg_class c ON c.oid = con.conrelid
 WHERE n.nspname = :schema
UNION ALL
SELECT 'idx', tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = :schema
UNION ALL
SELECT 'trg', c.relname, t.tgname, pg_get_triggerdef(t.oid) || '|' || t.tgenabled::text
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = :schema AND NOT t.tgisinternal
UNION ALL
SELECT 'fn', p.proname, pg_get_function_identity_arguments(p.oid), md5(pg_get_functiondef(p.oid))
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = :schema
""")


def _local_postgres(url: str, purpose: str):
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host not in _LOCAL_HOSTS:
        raise ValueError(f"{purpose}仅允许本机隔离测试库")
    return parsed


def collab_test_url(dedicated_env: str | None = None, dedicated_database: str | None = None, *,
                    purpose: str = "PostgreSQL 协作测试") -> str:
    """协作 PG 测试库：优先模块专用库，否则共用 ``AINOVEL_COLLAB_POSTGRES_TEST_URL``（本机 collab_test）。"""
    dedicated = os.environ.get(dedicated_env) if dedicated_env else None
    url = dedicated or os.environ.get(COLLAB_URL_ENV)
    if not url:
        names = " 或 ".join(name for name in (dedicated_env, COLLAB_URL_ENV) if name)
        pytest.skip(f"设置 {names} 才运行隔离 PostgreSQL 验证")
    parsed = _local_postgres(url, purpose)
    database = dedicated_database if dedicated else "collab_test"
    if (parsed.database, parsed.username) != (database, "collab_test"):
        raise ValueError(f"{purpose}仅允许本机 {database} 专用测试库")
    return url


def _run_in_thread(factory) -> None:
    """会话收尾可能处在事件循环之外或之内，统一在新线程的新 loop 中执行。"""
    errors: list[BaseException] = []

    def target():
        try:
            asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=target, name="pg-shared-schema-cleanup")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


class _SharedSchema:
    def __init__(self, url: str, schema: str, fingerprint: str) -> None:
        self.url = url
        self.schema = schema
        self.fingerprint = fingerprint


class SharedPostgres:
    """进程级共享 schema 注册表（按测试库 URL 区分）；只保存名字和指纹，不保存连接。"""

    def __init__(self) -> None:
        self.tag = f"ainovel_pg_{uuid4().hex[:12]}"
        self._schemas: dict[str, _SharedSchema] = {}
        self._discarded: list[tuple[str, str]] = []

    async def ensure(self, url: str) -> _SharedSchema:
        state = self._schemas.get(url)
        if state is None:
            schema = f"{self.tag}_{uuid4().hex[:8]}"
            fingerprint = await create_schema(url, schema)
            state = self._schemas[url] = _SharedSchema(url, schema, fingerprint)
        return state

    def discard(self, state: _SharedSchema) -> None:
        """schema 已被污染：下一个测试重新建一套，旧的在会话结束时一起删除（这里也尽量当场删）。"""
        if self._schemas.get(state.url) is state:
            del self._schemas[state.url]
        self._discarded.append((state.url, state.schema))

    def close(self) -> None:
        targets = [(item.url, item.schema) for item in self._schemas.values()] + self._discarded
        self._schemas.clear()
        self._discarded.clear()
        if not targets:
            return

        async def drop_all():
            for url, schema in targets:
                await drop_schema(url, schema)

        _run_in_thread(drop_all)


def engine_for(url: str, schema: str, *, application_name: str | None = None):
    settings = {"search_path": schema}
    if application_name:
        settings["application_name"] = application_name
    return create_async_engine(url, connect_args={"server_settings": settings})


async def _fingerprint(conn, schema: str) -> str:
    rows = sorted(tuple(str(value) for value in row) for row in (await conn.execute(
        _FINGERPRINT_SQL, {"schema": schema},
    )).all())
    return hashlib.sha256(repr(rows).encode()).hexdigest()


async def create_schema(url: str, schema: str) -> str:
    admin = create_async_engine(url)
    engine = engine_for(url, schema)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT current_schema()")) == schema
            await conn.run_sync(create_isolated_schema)
            return await _fingerprint(conn, schema)
    finally:
        await engine.dispose()
        await admin.dispose()


async def drop_schema(url: str, schema: str, *, application_name: str | None = None) -> None:
    admin = create_async_engine(url)
    try:
        async with admin.begin() as conn:
            if application_name:
                await _terminate_leaked(conn, application_name)
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await admin.dispose()


async def _terminate_leaked(conn, application_name: str) -> None:
    """引擎已 dispose 后仍带本进程标签的连接只能是测试泄漏的：结束它们，避免锁/会话级 advisory lock 串到下个测试。"""
    await conn.execute(text(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE application_name = :name AND pid <> pg_backend_pid()"
    ), {"name": application_name})
    for _ in range(200):
        if not await conn.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = :name "
            "AND pid <> pg_backend_pid())"
        ), {"name": application_name}):
            return
        await asyncio.sleep(0.01)


def _delete_order() -> list[str]:
    """子表在前、父表在后（模型依赖逆序）；循环外键由 SET NULL/延迟约束兜底，失败时整体回退 TRUNCATE。"""
    import warnings

    from app.models import Base

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return [table.name for table in reversed(Base.metadata.sorted_tables)]


async def _empty_tables(conn, schema: str) -> None:
    tables = set((await conn.scalars(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = :schema"
    ), {"schema": schema})).all())
    order = _delete_order()
    if tables - set(order):
        raise RuntimeError(f"共享 schema 出现模型之外的表：{sorted(tables - set(order))}")
    filled = set((await conn.scalars(text(" UNION ALL ".join(
        f"SELECT '{name}' WHERE EXISTS (SELECT 1 FROM \"{schema}\".\"{name}\")" for name in tables
    )))).all()) if tables else set()
    async with conn.begin_nested():
        # 每测试只有少量行：按依赖逆序 DELETE 有数据的表，比 TRUNCATE 全部 ~100 张表（约 0.7 秒）快一个量级。
        for name in order:
            if name in filled:
                await conn.execute(text(f'DELETE FROM "{schema}"."{name}"'))


async def reset_schema(url: str, schema: str, *, application_name: str) -> str:
    """清空全部业务表、序列归位并重放预置数据；返回清空后的 DDL 指纹。

    等价于 ``TRUNCATE <全部业务表> RESTART IDENTITY CASCADE`` + create_all 预置数据；
    快路径用 DELETE + ``ALTER SEQUENCE … RESTART``，出错时回退为真正的整表 TRUNCATE。
    """
    engine = engine_for(url, schema, application_name=f"{application_name}_admin")
    try:
        async with engine.begin() as conn:
            await _terminate_leaked(conn, application_name)
            await conn.execute(text("SET LOCAL lock_timeout = '15s'"))
            tables = (await conn.scalars(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = :schema ORDER BY tablename"
            ), {"schema": schema})).all()
            try:
                await _empty_tables(conn, schema)
            except Exception:  # noqa: BLE001 —— 快路径失败（如外键环/外来表）时整表截断兜底
                if tables:
                    names = ", ".join(f'"{schema}"."{name}"' for name in tables)
                    await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
            # 回滚的插入也会消耗 id：所有用过的序列归位，保证 id 与新建 schema 一样从 1 开始。
            for name in (await conn.scalars(text(
                "SELECT sequencename FROM pg_sequences WHERE schemaname = :schema AND last_value IS NOT NULL"
            ), {"schema": schema})).all():
                await conn.execute(text(f'ALTER SEQUENCE "{schema}"."{name}" RESTART'))
            await conn.run_sync(seed_isolated_schema)
            return await _fingerprint(conn, schema)
    finally:
        await engine.dispose()


class SchemaLease:
    """单个测试对共享（或独立）schema 的使用权；fixture 收尾时核对 DDL 并清空。"""

    def __init__(self, shared: SharedPostgres, *, isolated: bool) -> None:
        self._shared = shared
        self._isolated = isolated
        self._state: _SharedSchema | None = None
        self._private: tuple[str, str] | None = None
        self.application_name = shared.tag

    async def acquire(self, url: str) -> str:
        if self._isolated:
            if self._private is None:
                schema = f"{self._shared.tag}_iso_{uuid4().hex[:8]}"
                await create_schema(url, schema)
                self._private = (url, schema)
            if self._private[0] != url:
                raise RuntimeError("同一测试不能在两个不同测试库上取得独立 schema")
            return self._private[1]
        if self._state is not None:
            if self._state.url != url:
                raise RuntimeError("同一测试不能同时租用两个不同测试库的共享 schema")
            return self._state.schema
        state = await self._shared.ensure(url)
        # 开始前也清空一次：上一个测试的收尾若被中断，这里兜底。
        try:
            fingerprint = await reset_schema(url, state.schema, application_name=self.application_name)
        except BaseException:
            self._shared.discard(state)
            raise
        if fingerprint != state.fingerprint:
            self._shared.discard(state)
            state = await self._shared.ensure(url)
        self._state = state
        return state.schema

    async def release(self) -> None:
        if self._private is not None:
            url, schema = self._private
            self._private = None
            await drop_schema(url, schema, application_name=self.application_name)
            return
        state, self._state = self._state, None
        if state is None:
            return
        try:
            fingerprint = await reset_schema(state.url, state.schema, application_name=self.application_name)
        except BaseException:
            # 清空失败（例如泄漏连接持锁超时）：丢弃该 schema，下个测试重建；会话收尾再删除。
            self._shared.discard(state)
            raise
        if fingerprint != state.fingerprint:
            self._shared.discard(state)
            pytest.fail(
                "测试修改了共享 PostgreSQL schema 的 DDL（表/列/约束/索引/触发器/函数）；"
                "已丢弃并将为后续测试重建。会改 DDL 的测试须标记 @pytest.mark.pg_isolated_schema",
                pytrace=False,
            )


async def _close_apps(apps) -> None:
    for app in apps:
        await app.state.system_settings.close()
    for app in apps:
        # 未启动的引擎 stop 为空操作；已启动但测试未停的，在清空数据前收尾，避免写入下一个测试。
        await app.state.task_engine.stop()


@asynccontextmanager
async def postgres_apps(lease: SchemaLease, url: str, tmp_path, fake_llm, monkeypatch, *, count: int = 2,
                        separate_settings: bool = False, start_first_engine: bool = False) -> AsyncIterator[list]:
    """在租用的 schema 上创建 ``count`` 个共享同一引擎的应用实例（引擎属于当前事件循环）。"""
    from app import main
    from app.services import authorization_service, retrieval_service
    from app.services.mentions.matcher import cache as mention_cache
    from tests.helpers import make_settings

    schema = await lease.acquire(url)
    engine = engine_for(url, schema, application_name=lease.application_name)
    apps = []
    retrieval_service.invalidate()
    mention_cache.clear()
    try:
        monkeypatch.setattr(main, "create_engine", lambda _url: engine)
        settings = None if separate_settings else make_settings(tmp_path, database_url=url)
        for _ in range(count):
            # separate_settings：分别构造 Settings，模拟多进程而非共享同一 Python 对象。
            app_settings = settings or make_settings(tmp_path, database_url=url)
            apps.append(main.create_app(app_settings, provider_factory=fake_llm.factory, start_task_engine=False))
        await authorization_service.initialize(apps[0].state.sessionmaker)
        for app in apps:
            await app.state.system_settings.load()
        if start_first_engine:
            await apps[0].state.task_engine.start()
        yield apps
    finally:
        try:
            await _close_apps(apps)
        finally:
            await engine.dispose()
            retrieval_service.invalidate()
            mention_cache.clear()
