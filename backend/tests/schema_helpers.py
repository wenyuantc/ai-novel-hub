"""测试建库入口，避免数据库方言的DDL状态影响后续隔离库。"""

from sqlalchemy import DDL, CheckConstraint, MetaData, create_engine, event, inspect, select

from app.models import Base
from app.models.entities import _seed_preset_styles, _seed_relationship_types
from app.models.skills_mcp import _seed_builtin_skills


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
    for table, callback in (
        ("relationship_types", _seed_relationship_types),
        ("writing_styles", _seed_preset_styles),
        ("skills", _seed_builtin_skills),
    ):
        event.listen(metadata.tables[table], "after_create", callback)
    metadata.create_all(connection)


def seed_isolated_schema(connection):
    """重放 create_all 的预置数据（共享 schema 每次 TRUNCATE 后调用），与新建 schema 的初始数据一致。"""
    for table, callback in (
        ("relationship_types", _seed_relationship_types),
        ("writing_styles", _seed_preset_styles),
        ("skills", _seed_builtin_skills),
    ):
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
