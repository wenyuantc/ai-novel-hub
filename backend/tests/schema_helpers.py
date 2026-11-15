"""测试建库入口，避免数据库方言的DDL状态影响后续隔离库。"""

from sqlalchemy import DDL, CheckConstraint, MetaData, event

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
