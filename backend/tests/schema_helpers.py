"""测试建库入口，避免数据库方言的DDL状态影响后续隔离库。"""

from sqlalchemy import MetaData, event

from app.models import Base
from app.models.entities import _seed_preset_styles, _seed_relationship_types
from app.models.skills_mcp import _seed_builtin_skills


def create_isolated_schema(connection):
    # PG AddConstraint会改变Constraint自身的创建规则，不能复用ORM全局metadata。
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    # to_metadata不传播未声明propagate的事件；复用现有预置回调，不复制预置内容。
    for table, callback in (
        ("relationship_types", _seed_relationship_types),
        ("writing_styles", _seed_preset_styles),
        ("skills", _seed_builtin_skills),
    ):
        event.listen(metadata.tables[table], "after_create", callback)
    metadata.create_all(connection)
