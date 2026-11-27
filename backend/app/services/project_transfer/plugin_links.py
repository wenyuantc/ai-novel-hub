"""按插件备份登记的作品归属审查核心实体的可空外键。"""

from dataclasses import dataclass

from sqlalchemy import select

from app.services.project_transfer.registry import RULES_BY_TABLE, transfer_catalog
from app.services.project_transfer.rules import TableRule


@dataclass(frozen=True)
class PluginLink:
    target: str
    rule: TableRule
    column: str

    @property
    def edge(self) -> tuple[str, str, str, str, str]:
        return (self.target, self.rule.table, self.column, "id", "SET NULL")

    def foreign(self, project_id: int, ids):
        reference = getattr(self.rule.model, self.column)
        return select(reference).where(reference.in_(ids), self.rule.scope(project_id).is_not(True))


def nullable_links(targets: set[str]) -> tuple[PluginLink, ...]:
    """实际图必须与批准的 SET NULL / 单列 id 完全匹配；新级联路径仍需单独审查。"""
    return tuple(
        PluginLink(target, rule, column)
        for rule in transfer_catalog().rules
        if rule.table not in RULES_BY_TABLE
        for column, target in rule.fk.items()
        if target in targets and rule.model.__table__.c[column].nullable
    )
