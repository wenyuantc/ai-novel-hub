"""测试建库入口，避免数据库方言的DDL状态影响后续隔离库。"""

import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy import DDL, CheckConstraint, MetaData, create_engine, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import Base
from app.models.entities import _seed_preset_styles, _seed_relationship_types
from app.models.media import _seed_local, _seed_settings
from app.models.skills_mcp import _seed_builtin_skills

SCHEMA_SEEDS = (
    ("relationship_types", _seed_relationship_types),
    ("writing_styles", _seed_preset_styles),
    ("skills", _seed_builtin_skills),
    ("media_storage_locations", _seed_local),
    ("media_storage_settings", _seed_settings),
)


def create_isolated_schema(connection):
    # PG AddConstraint会改变Constraint自身的创建规则，不能复用ORM全局metadata。
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.tables.values():
        copied = table.to_metadata(metadata)
        # to_metadata 也不复制 ddl_if；同名方言检查须以 SQL 表达式区分，不能在 PG 执行 SQLite 检查。
        for constraint in table.constraints:
            condition = getattr(constraint, "_ddl_if", None)
            if condition is None:
                continue
            if not isinstance(constraint, CheckConstraint):
                raise RuntimeError(f"未支持的条件约束：{table.name}")
            matches = [item for item in copied.constraints if isinstance(item, CheckConstraint)
                       and item.name == constraint.name and str(item.sqltext) == str(constraint.sqltext)]
            if len(matches) != 1:
                raise RuntimeError(f"无法复制条件约束：{table.name}")
            matches[0].ddl_if(dialect=condition.dialect, callable_=condition.callable_, state=condition.state)
        # 直接复用原模型 DDL（含 execute_if 方言条件），确保隔离表拥有相同身份/不可变触发器。
        for name in ("after_create", "after_drop"):
            for listener in getattr(table.dispatch, name):
                if isinstance(listener, DDL) and not event.contains(copied, name, listener):
                    event.listen(copied, name, listener)
    # to_metadata不传播未声明propagate的事件；复用现有预置回调，不复制预置内容。
    for table, callback in SCHEMA_SEEDS:
        event.listen(metadata.tables[table], "after_create", callback)
    metadata.create_all(connection)


def seed_isolated_schema(connection):
    """重放 create_all 的预置数据（共享 schema 每次 TRUNCATE 后调用），与新建 schema 的初始数据一致。"""
    for table, callback in SCHEMA_SEEDS:
        callback(Base.metadata.tables[table], connection)


def assert_sqlite_chapter_plan_fk_intact():
    """真实 PG 建库后，原 ORM 元数据仍须创建可执行 SET NULL 的 SQLite 外键。"""
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            Base.metadata.create_all(connection)
            keys = inspect(connection).get_foreign_keys("chapters")
            assert any(
                key["constrained_columns"] == ["plan_id"]
                and key["referred_table"] == "chapter_plans"
                and key["referred_columns"] == ["id"]
                and key["options"].get("ondelete") == "SET NULL"
                for key in keys
            ), keys

            tables = Base.metadata.tables
            user_id = connection.execute(tables["users"].insert().values(
                username="cross-dialect", password_hash="synthetic",
            )).inserted_primary_key[0]
            project_id = connection.execute(tables["projects"].insert().values(
                user_id=user_id, title="跨方言测试",
            )).inserted_primary_key[0]
            outline_id = connection.execute(tables["outlines"].insert().values(
                project_id=project_id, title="大纲",
            )).inserted_primary_key[0]
            plan_id = connection.execute(tables["chapter_plans"].insert().values(
                project_id=project_id, outline_id=outline_id, seq=1, title="细纲",
            )).inserted_primary_key[0]
            chapter_id = connection.execute(tables["chapters"].insert().values(
                project_id=project_id, plan_id=plan_id, title="正文", content="保留正文",
            )).inserted_primary_key[0]
            connection.execute(tables["chapter_plans"].delete().where(tables["chapter_plans"].c.id == plan_id))
            chapter = tables["chapters"]
            assert tuple(connection.execute(select(chapter.c.plan_id, chapter.c.content)
                       .where(chapter.c.id == chapter_id)).one()) == (None, "保留正文")
    finally:
        engine.dispose()


@asynccontextmanager
async def isolated_worker_database(monkeypatch, *, required=False):
    """共享隔离库的两个应用，在 SQLite 或本机专用 PostgreSQL schema 验收。"""
    raw = os.getenv("AINOVEL_POSTGRES_TEST_URL")
    if not raw:
        if required:
            pytest.skip("缺少隔离 PostgreSQL 测试库 AINOVEL_POSTGRES_TEST_URL")
        yield {}
        return
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.database != "ainovel_test" or url.query):
        raise ValueError("AINOVEL_POSTGRES_TEST_URL 必须指向本机专用 ainovel_test 测试库")
    schema = f"worker_lease_{uuid4().hex}"
    admin = create_async_engine(url)
    try:
        async with admin.begin() as conn:
            await conn.execute(CreateSchema(schema))
        try:
            def make_scoped_engine(database_url):
                assert make_url(database_url) == url
                return create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})

            monkeypatch.setattr("app.main.create_engine", make_scoped_engine)
            monkeypatch.setattr(Base.metadata, "create_all", create_isolated_schema)
            yield {"database_url": raw}
        finally:
            async with admin.begin() as conn:
                await conn.execute(DropSchema(schema, cascade=True))
    finally:
        await admin.dispose()
