"""PG的ALTER约束建表不能改变后续SQLite的FK与模型预置。"""

from sqlalchemy import create_engine, create_mock_engine, func, inspect, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

from app.models import Base
from tests.schema_helpers import create_isolated_schema

SEED_TABLES = ("relationship_types", "writing_styles", "skills")


def _sqlite_ddl():
    return {
        name: str(CreateTable(table).compile(dialect=sqlite.dialect()))
        for name, table in Base.metadata.tables.items()
    }


def _seed_data(connection):
    result = {}
    for name in SEED_TABLES:
        table = Base.metadata.tables[name]
        columns = [column for column in table.c if column.name not in {"created_at", "updated_at"}]
        result[name] = list(connection.execute(select(*columns).order_by(*table.primary_key.columns)))
    return result


def test_postgresql_schema_build_keeps_sqlite_foreign_keys_and_model_seeds():
    original = create_engine("sqlite://")
    isolated = create_engine("sqlite://")
    try:
        with original.begin() as connection:
            Base.metadata.create_all(connection)
            expected_seeds = _seed_data(connection)
        before = _sqlite_ddl()
        create_isolated_schema(create_mock_engine("postgresql://", lambda *_args, **_kwargs: None))
        after = _sqlite_ddl()
        assert after["chapters"] == before["chapters"]
        assert after == before

        with isolated.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            create_isolated_schema(connection)
            actual_seeds = _seed_data(connection)
            assert all(expected_seeds.values())
            assert actual_seeds == expected_seeds
            keys = inspect(connection).get_foreign_keys("chapters")
            assert any(key["constrained_columns"] == ["plan_id"]
                       and key["options"]["ondelete"] == "SET NULL" for key in keys)

            tables = Base.metadata.tables
            user_id = connection.execute(tables["users"].insert().values(
                username="schema-isolation", password_hash="synthetic",
            )).inserted_primary_key[0]
            project_id = connection.execute(tables["projects"].insert().values(
                user_id=user_id, title="跨方言建库验证",
            )).inserted_primary_key[0]
            outline_id = connection.execute(tables["outlines"].insert().values(
                project_id=project_id, title="删除的大纲",
            )).inserted_primary_key[0]
            plan_id = connection.execute(tables["chapter_plans"].insert().values(
                project_id=project_id, outline_id=outline_id, seq=1, title="级联删除的细纲",
            )).inserted_primary_key[0]
            chapter_id = connection.execute(tables["chapters"].insert().values(
                project_id=project_id, outline_id=outline_id, plan_id=plan_id, title="正文保留", content="原始正文",
            )).inserted_primary_key[0]
            connection.execute(tables["outlines"].delete().where(tables["outlines"].c.id == outline_id))
            chapter = tables["chapters"]
            assert tuple(connection.execute(select(
                chapter.c.outline_id, chapter.c.plan_id, chapter.c.content,
            ).where(chapter.c.id == chapter_id)).one()) == (None, None, "原始正文")
            assert connection.scalar(select(func.count()).select_from(tables["chapter_plans"])) == 0
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        original.dispose()
        isolated.dispose()
