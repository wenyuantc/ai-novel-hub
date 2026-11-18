"""工作台独立迁移，旧章保持未定稿，已写入的排期和历史均受降级保护。"""

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.models import Base
from tests.test_consistency_migration import _engine
from tests.test_setting_fields_migration import _config, _schema_contract

TABLES = ("serialization_goals", "serialization_entries", "serialization_editions", "serialization_publications")


def test_0064_workbench_schema_and_guarded_roundtrip(tmp_path):
    path = tmp_path / "serialization.db"
    config = _config(path)
    command.upgrade(config, "0063")
    engine = _engine(path)
    reference = create_engine("sqlite://")
    try:
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'author','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'旧作品','','','','planning')"))
            db.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                            "VALUES(1,1,7,'旧章','原文','done',2)"))
        command.upgrade(config, "0064")
        Base.metadata.create_all(reference)
        for table in TABLES:
            assert _schema_contract(inspect(engine), table) == _schema_contract(inspect(reference), table)
        with engine.begin() as db:
            assert db.execute(text("SELECT title,content,status FROM chapters")).one() == ("旧章", "原文", "done")
            assert all(db.scalar(text(f"SELECT count(*) FROM {table}")) == 0 for table in TABLES)
            db.execute(text("INSERT INTO serialization_goals(project_id,daily_goal) VALUES(1,2)"))
        with pytest.raises(RuntimeError, match="先备份"):
            command.downgrade(config, "0063")
        with engine.begin() as db:
            db.execute(text("DELETE FROM serialization_goals"))
            db.execute(text("INSERT INTO serialization_entries(id,project_id,chapter_id,stage) VALUES(1,1,1,'final')"))
            db.execute(text("INSERT INTO serialization_editions(id,entry_id,seq,chapter_no_at_confirmation,title,"
                            "content,fingerprint) VALUES(1,1,1,7,'旧章','定稿',:digest)"), {"digest": "a" * 64})
            db.execute(text("INSERT INTO serialization_publications(project_id,edition_id,channel,published_at) "
                            "VALUES(1,1,'甲',CURRENT_TIMESTAMP),(1,1,'乙',CURRENT_TIMESTAMP)"))
            db.execute(text("DELETE FROM chapters WHERE id=1"))
            assert db.execute(text("SELECT chapter_id,revision FROM serialization_entries")).one() == (None, 0)
            assert db.scalar(text("SELECT count(*) FROM serialization_publications")) == 2
        with pytest.raises(RuntimeError, match="先备份"):
            command.downgrade(config, "0063")
        with engine.begin() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0064"
            db.execute(text("DELETE FROM projects WHERE id=1"))
            assert all(db.scalar(text(f"SELECT count(*) FROM {table}")) == 0 for table in TABLES)
        command.downgrade(config, "0063")
        assert set(TABLES).isdisjoint(inspect(engine).get_table_names())
        command.upgrade(config, "0064")
        assert set(TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
        reference.dispose()
