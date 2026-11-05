from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.models import ProjectNote


def test_0032_notes_schema_stable_ids_and_safe_downgrade(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    db_file = tmp_path / "notes0032.db"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0031")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'notes-user','hash')"))
        conn.execute(text(
            "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
            "VALUES(1,1,'保留的作品','','','','planning')"
        ))
    command.upgrade(cfg, "0032")
    inspector = inspect(engine)
    model = ProjectNote.__table__
    columns = {column["name"]: column for column in inspector.get_columns("project_notes")}
    assert set(columns) == set(model.columns.keys())
    assert all(columns[column.name]["nullable"] == column.nullable for column in model.columns)
    assert {index["name"] for index in inspector.get_indexes("project_notes")} == {
        index.name for index in model.indexes
    }
    assert inspector.get_foreign_keys("project_notes")[0]["options"]["ondelete"] == "CASCADE"
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO project_notes(project_id,title,content) VALUES(1,'有原文','😀笔记')"))
        assert conn.scalar(text("SELECT revision FROM project_notes")) == 1
    with pytest.raises(RuntimeError, match="存在作品笔记"):
        command.downgrade(cfg, "0031")
    with engine.begin() as conn:
        assert conn.scalar(text("SELECT content FROM project_notes")) == "😀笔记"
        conn.execute(text("DELETE FROM project_notes"))
        conn.execute(text("INSERT INTO project_notes(project_id,title) VALUES(1,'新笔记')"))
        assert conn.scalar(text("SELECT id FROM project_notes")) == 2
        conn.execute(text("DELETE FROM projects WHERE id=1"))
        assert conn.scalar(text("SELECT count(*) FROM project_notes")) == 0
    engine.dispose()
    command.downgrade(cfg, "0031")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert "project_notes" not in inspect(engine).get_table_names()
        assert "projects" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
