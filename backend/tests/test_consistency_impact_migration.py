"""迁移 0059：台账的上一版三列与 consistency_impacts 表的结构契约、级联规则与有损降级防护。"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.models import Base
from tests.test_consistency_migration import HASH, _engine
from tests.test_setting_fields_migration import _config, _migration_fk_on_connect, _schema_contract

TABLES = ("consistency_ledgers", "consistency_impacts")
PREV = {"prev_facts", "prev_source_hash", "prev_text_hash"}


def test_0059_schema_matches_models(tmp_path):
    """冻结结构契约：迁移后的台账与影响表和 ORM create_all 的列、约束、索引逐项一致。"""
    path = tmp_path / "schema.db"
    command.upgrade(_config(path), "0059")
    migrated = create_engine(f"sqlite:///{path}")
    expected = create_engine("sqlite://")
    try:
        Base.metadata.create_all(expected)
        actual_inspector, expected_inspector = inspect(migrated), inspect(expected)
        for table in TABLES:
            assert _schema_contract(actual_inspector, table) == _schema_contract(expected_inspector, table), table
        with migrated.connect() as db:
            # 不存章节号（不登记 CHAPTER_NO_REFS 的前提）。
            columns = {c["name"] for c in inspect(db).get_columns("consistency_impacts")}
            assert not columns & {"chapter_no", "chapter_nos"}
    finally:
        migrated.dispose()
        expected.dispose()


def test_0059_upgrade_keeps_ledgers_and_guards_downgrade(tmp_path, monkeypatch):
    _migration_fk_on_connect(monkeypatch)
    path = tmp_path / "impact.sqlite3"
    config = _config(path)
    engine = _engine(path)
    try:
        command.upgrade(config, "0058")
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'原书','','','','planning')"))
            db.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                            "VALUES(1,1,1,'第一章','佩剑已毁','draft',4),(2,1,2,'第二章','拔剑','draft',2)"))
            db.execute(text("INSERT INTO consistency_ledgers(project_id,chapter_id,source_hash,text_hash,facts) "
                            "VALUES(1,1,:h,:h,'[{\"id\":\"1\"}]')"), {"h": HASH})
        command.upgrade(config, "0059")
        with engine.begin() as db:
            # 存量台账原样保留，上一版为空。
            row = db.execute(text(
                "SELECT facts,source_hash,prev_facts,prev_source_hash,prev_text_hash FROM consistency_ledgers")).first()
            assert tuple(row) == ('[{"id":"1"}]', HASH, None, None, None)
        # 空的新表与空的上一版：可以无损降级，再升级回来。
        command.downgrade(config, "0058")
        with engine.connect() as db:
            assert "consistency_impacts" not in inspect(db).get_table_names()
            assert not PREV & {c["name"] for c in inspect(db).get_columns("consistency_ledgers")}
            assert db.scalar(text("SELECT facts FROM consistency_ledgers")) == '[{"id":"1"}]'
        command.upgrade(config, "0059")
        with engine.begin() as db:
            db.execute(text(
                "INSERT INTO consistency_impacts(project_id,chapter_id,changes,items,uncovered,acknowledged) "
                "VALUES(1,1,'[]','[]','[]','[]')"))
            row = db.execute(text(
                "SELECT revision,model,source_hash,assessed_at,actor_id,task_id FROM consistency_impacts")).first()
            assert tuple(row) == (1, "", None, None, None, None)
        # 每章至多一行影响记录；修订号为正。
        for statement in (
            "INSERT INTO consistency_impacts(project_id,chapter_id,changes,items,uncovered,acknowledged) "
            "VALUES(1,1,'[]','[]','[]','[]')",
            "INSERT INTO consistency_impacts(project_id,chapter_id,changes,items,uncovered,acknowledged,revision) "
            "VALUES(1,2,'[]','[]','[]','[]',0)",
        ):
            with pytest.raises(Exception, match="(?i)unique|check"), engine.begin() as db:
                db.execute(text(statement))
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0058")
        with engine.begin() as db:
            # 删章：影响记录级联删除。
            db.execute(text("DELETE FROM chapters WHERE id=1"))
            assert db.scalar(text("SELECT count(*) FROM consistency_impacts")) == 0
            # 只剩上一版台账也拒绝降级。
            db.execute(text("INSERT INTO consistency_ledgers(project_id,chapter_id,source_hash,text_hash,facts,"
                            "prev_facts,prev_source_hash,prev_text_hash) VALUES(1,2,:h,:h,'[]','[]',:h,:h)"),
                       {"h": HASH})
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0058")
        with engine.begin() as db:
            # 删作品：全部级联清空。
            db.execute(text("DELETE FROM projects WHERE id=1"))
            for table in TABLES:
                assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0058")
        with engine.connect() as db:
            names = inspect(db).get_table_names()
            assert "consistency_impacts" not in names and "consistency_ledgers" in names
        command.upgrade(config, "0059")
    finally:
        engine.dispose()
