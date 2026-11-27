from dataclasses import replace

import pytest
from sqlalchemy import ForeignKey, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app import plugins
from app.models import Project
from app.plugins import PluginManifest
from app.services.project_transfer.registry import TableRule
from tests.helpers import create_project, register
from tests.test_project_transfer import _backup, _import


class SampleBase(DeclarativeBase):
    pass


class SampleNote(SampleBase):
    __tablename__ = "example_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey(Project.id))
    text: Mapped[str]


RULE = TableRule("example_notes", SampleNote, lambda pid: SampleNote.project_id == pid, fill={"project_id": "project"})
MANIFEST = PluginManifest("example", "示例插件", "", "1")


@pytest.fixture
def settings_overrides(monkeypatch):
    monkeypatch.setattr("app.plugins.builtins.REGISTERED", (replace(MANIFEST, transfer_rules=(RULE,)),))
    return {}


async def test_disabled_plugin_backup_restores_only_selected_project(app, client):
    async with app.state.engine.begin() as conn:
        await conn.run_sync(SampleBase.metadata.create_all)
    await register(client)
    target = await create_project(client, title="原作")
    other = await create_project(client, title="另一作品")
    async with app.state.sessionmaker.begin() as db:
        db.add_all([SampleNote(project_id=target["id"], text="带走"), SampleNote(project_id=other["id"], text="隔离")])
    backup = await _backup(client, target["id"])
    assert [item["text"] for item in backup["tables"]["example_notes"]] == ["带走"]
    await _import(client, backup, "恢复作品")
    async with app.state.sessionmaker() as db:
        restored = await db.scalar(select(Project.id).where(Project.title == "恢复作品"))
        assert restored and restored != target["id"]
        assert list(await db.scalars(select(SampleNote.text).where(SampleNote.project_id == restored))) == ["带走"]


@pytest.mark.parametrize("rules, excluded, problem", [
    ((RULE, RULE), {}, "example_notes"),
    ((RULE,), {"example_notes": "重复处理"}, "example_notes"),
    ((replace(RULE, table="characters"),), {}, "characters"),
    ((replace(RULE, table="example_wrong"),), {}, "example_wrong"),
    ((), {"example_notes": " "}, "example_notes"),
])
def test_invalid_plugin_table_declaration_fails_at_registration(rules, excluded, problem):
    with pytest.raises(ValueError, match=problem):
        plugins.load((replace(MANIFEST, transfer_rules=rules, not_exported=excluded),))


def test_app_registries_keep_independent_transfer_tables():
    from app.plugins.registry import PluginRegistry, use_registry
    from app.services.project_transfer.registry import NOT_EXPORTED, RULES, transfer_catalog

    empty = PluginRegistry(())
    sample = PluginRegistry((replace(MANIFEST, transfer_rules=(RULE,), not_exported={"example_jobs": "运行态"}),))
    with use_registry(sample):
        assert transfer_catalog().rules == (*RULES, RULE)
        assert transfer_catalog().not_exported["example_jobs"] == "运行态"
        with use_registry(empty):
            assert transfer_catalog().rules == RULES
            assert transfer_catalog().not_exported == NOT_EXPORTED
        assert transfer_catalog().by_table["example_notes"] == RULE
