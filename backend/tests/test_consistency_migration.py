"""迁移 0058：一致性检查三张表的结构契约、级联规则与有损降级防护。"""

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text

from app.models import Base
from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations, _schema_contract

TABLES = ("consistency_ledgers", "consistency_runs", "consistency_issues")
HASH = "a" * 64


def _engine(path):
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    return engine


def test_0058_schema_matches_models(tmp_path):
    """冻结结构契约：迁移建出的三张表与 ORM create_all 的列、约束、索引逐项一致。"""
    path = tmp_path / "schema.db"
    command.upgrade(_config(path), "0058")
    migrated = create_engine(f"sqlite:///{path}")
    expected = create_engine("sqlite://")
    try:
        Base.metadata.create_all(expected)
        actual_inspector, expected_inspector = inspect(migrated), inspect(expected)
        for table in TABLES:
            expected_contract = _schema_contract(expected_inspector, table)
            if table == "consistency_ledgers":
                # 0059 给台账加的「上一版」三列不属于 0058 的冻结结构。
                for column in ("prev_facts", "prev_source_hash", "prev_text_hash"):
                    expected_contract["columns"].pop(column)
            assert _schema_contract(actual_inspector, table) == expected_contract, table
        with migrated.connect() as db:
            ddl = db.scalar(text("SELECT sql FROM sqlite_master WHERE name='consistency_runs'"))
            assert "AUTOINCREMENT" in ddl.upper()
            # 三张表都不存章节号（不登记 CHAPTER_NO_REFS 的前提）。
            for table in TABLES:
                assert not {c["name"] for c in inspect(db).get_columns(table)} & {"chapter_no", "chapter_nos"}
    finally:
        migrated.dispose()
        expected.dispose()


def test_0058_upgrade_cascades_and_guarded_downgrade(tmp_path, monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)
    path = tmp_path / "consistency.sqlite3"
    config = _config(path)
    engine = _engine(path)
    try:
        command.upgrade(config, "0057")
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'原书','','','','planning')"))
            db.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                            "VALUES(1,1,1,'第一章','佩剑已毁','draft',4),(2,1,2,'第二章','拔剑','draft',2)"))
        command.upgrade(config, "0058")
        with engine.begin() as db:
            assert set(TABLES) <= set(inspect(db).get_table_names())
            assert db.scalar(text("SELECT content FROM chapters WHERE id=1")) == "佩剑已毁"
            db.execute(text("INSERT INTO consistency_ledgers(project_id,chapter_id,source_hash,text_hash,facts) "
                            "VALUES(1,1,:h,:h,'[]'),(1,2,:h,:h,'[]')"), {"h": HASH})
            ledger = db.execute(text("SELECT model,schema_version,task_id FROM consistency_ledgers")).first()
            assert tuple(ledger) == ("", 1, None)
            db.execute(text("INSERT INTO consistency_runs(project_id,actor_id,scope,stats) VALUES(1,1,'{}','{}')"))
            run_id = db.scalar(text("SELECT id FROM consistency_runs"))
            assert db.scalar(text("SELECT status FROM consistency_runs")) == "running"
            db.execute(text(
                "INSERT INTO consistency_issues(project_id,category,title,severity,identity_key,evidence,"
                "first_run_id,last_seen_run_id,last_checked_run_id,status_changed_by) "
                "VALUES(1,'item','佩剑','high',:k,'[]',:r,:r,:r,1)"), {"k": HASH, "r": run_id})
            issue = db.execute(text(
                "SELECT status,note,revision,reappeared,description,suggestion FROM consistency_issues")).first()
            assert tuple(issue) == ("open", "", 1, 0, "", "")
        # 每章至多一份台账；同一作品内问题身份唯一；修订号为正。
        for statement, params in (
            ("INSERT INTO consistency_ledgers(project_id,chapter_id,source_hash,text_hash,facts) "
             "VALUES(1,1,:h,:h,'[]')", {"h": HASH}),
            ("INSERT INTO consistency_issues(project_id,category,title,severity,identity_key,evidence) "
             "VALUES(1,'item','重复','low',:h,'[]')", {"h": HASH}),
            ("INSERT INTO consistency_issues(project_id,category,title,severity,identity_key,evidence,revision) "
             "VALUES(1,'item','零修订','low',:h,'[]',0)", {"h": "b" * 64}),
        ):
            with pytest.raises(Exception, match="(?i)unique|check"), engine.begin() as db:
                db.execute(text(statement), params)
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0057")
        with engine.begin() as db:
            # 删章：台账级联删除，问题保留。
            db.execute(text("DELETE FROM chapters WHERE id=1"))
            assert db.scalars(text("SELECT chapter_id FROM consistency_ledgers")).all() == [2]
            assert db.scalar(text("SELECT count(*) FROM consistency_issues")) == 1
            # 删运行记录：问题的三个引用置空；运行 id 不复用。
            db.execute(text("DELETE FROM consistency_runs"))
            refs = db.execute(text(
                "SELECT first_run_id,last_seen_run_id,last_checked_run_id FROM consistency_issues")).first()
            assert tuple(refs) == (None, None, None)
            db.execute(text("INSERT INTO consistency_runs(project_id,scope,stats) VALUES(1,'{}','{}')"))
            assert db.scalar(text("SELECT id FROM consistency_runs")) > run_id
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0057")
        with engine.begin() as db:
            # 删作品：三张表全部级联清空。
            db.execute(text("DELETE FROM projects WHERE id=1"))
            for table in TABLES:
                assert db.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0057")
        with engine.connect() as db:
            names = inspect(db).get_table_names()
            assert not set(TABLES) & set(names) and "novel_templates" in names
        command.upgrade(config, "0058")
    finally:
        engine.dispose()
