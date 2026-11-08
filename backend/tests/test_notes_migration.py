from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.models import ProjectNote

FLAG_COLUMNS = ("is_favorite", "is_pinned")


def _config(tmp_path, name):
    backend = Path(__file__).resolve().parents[1]
    db_file = tmp_path / name
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    cfg.attributes["configure_logger"] = False
    return cfg, db_file


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
    # 0041 才增加收藏／固定列及其索引。
    assert set(columns) == set(model.columns.keys()) - set(FLAG_COLUMNS)
    assert all(columns[column.name]["nullable"] == column.nullable
               for column in model.columns if column.name not in FLAG_COLUMNS)
    assert {index["name"] for index in inspector.get_indexes("project_notes")} == {
        index.name for index in model.indexes
    } - {"ix_project_notes_project_pinned"}
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


def test_0041_note_flags_backfill_defaults_without_touching_revision(tmp_path):
    cfg, db_file = _config(tmp_path, "notes0041.db")
    command.upgrade(cfg, "0040")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'flags-user','hash')"))
        conn.execute(text(
            "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
            "VALUES(1,1,'作品','','','','planning')"
        ))
        conn.execute(text(
            "INSERT INTO project_notes(id,project_id,title,content,revision,updated_at) "
            "VALUES(1,1,'旧笔记','原文',3,'2026-01-02 03:04:05')"
        ))
    engine.dispose()
    command.upgrade(cfg, "0041")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        model = ProjectNote.__table__
        columns = {column["name"]: column for column in inspector.get_columns("project_notes")}
        assert set(columns) == set(model.columns.keys())
        assert all(columns[name]["nullable"] is False for name in FLAG_COLUMNS)
        indexes = {index["name"]: index for index in inspector.get_indexes("project_notes")}
        assert set(indexes) == {index.name for index in model.indexes}
        assert indexes["ix_project_notes_project_pinned"]["column_names"] == ["project_id", "is_pinned", "updated_at"]
        assert inspector.get_foreign_keys("project_notes")[0]["options"]["ondelete"] == "CASCADE"
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT is_favorite,is_pinned,revision,updated_at,content FROM project_notes"
            )).one()
            assert tuple(row) == (0, 0, 3, "2026-01-02 03:04:05", "原文")
            conn.execute(text("INSERT INTO project_notes(project_id,title) VALUES(1,'新笔记')"))
            assert conn.scalar(text("SELECT is_pinned FROM project_notes WHERE id=2")) == 0
            conn.execute(text("UPDATE project_notes SET is_pinned=1 WHERE id=1"))
            conn.execute(text("INSERT INTO project_notes(project_id,title) VALUES(1,'将删除')"))
            conn.execute(text("DELETE FROM project_notes WHERE id=3"))
    finally:
        engine.dispose()
    # 仍有收藏或固定标记时拒绝有损降级，结构保持 0041。
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(cfg, "0040")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        assert {column["name"] for column in inspect(engine).get_columns("project_notes")} >= set(FLAG_COLUMNS)
        with engine.begin() as conn:
            conn.execute(text("UPDATE project_notes SET is_pinned=0"))
    finally:
        engine.dispose()
    command.downgrade(cfg, "0040")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        inspector = inspect(engine)
        assert not set(FLAG_COLUMNS) & {column["name"] for column in inspector.get_columns("project_notes")}
        indexes = {index["name"] for index in inspector.get_indexes("project_notes")}
        assert "ix_project_notes_project_pinned" not in indexes
        with engine.begin() as conn:
            assert conn.execute(text("SELECT id,revision,content FROM project_notes ORDER BY id")).all() == [
                (1, 3, "原文"), (2, 1, ""),
            ]
            # batch 重建后仍保留 AUTOINCREMENT 与高水位，不复用已删除笔记 ID（2、3）。
            conn.execute(text("DELETE FROM project_notes WHERE id=2"))
            conn.execute(text("INSERT INTO project_notes(project_id,title) VALUES(1,'再建')"))
            assert conn.scalar(text("SELECT max(id) FROM project_notes")) == 4
        assert inspector.get_foreign_keys("project_notes")[0]["options"]["ondelete"] == "CASCADE"
    finally:
        engine.dispose()
