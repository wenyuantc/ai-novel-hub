"""从真实 0054 升级，检查同作者外键、级联与降级保护。"""


import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations


def test_0055_existing_projects_cross_owner_fk_and_safe_downgrade(tmp_path, monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)
    path = tmp_path / "pen_names.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        command.upgrade(config, "0054")
        with engine.begin() as db:
            db.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash'),(2,'other','hash')"))
            db.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'旧作品','','','','planning')"))
        command.upgrade(config, "0055")
        with engine.begin() as db:
            assert db.scalar(text("SELECT title FROM projects WHERE id=1")) == "旧作品"
            assert db.scalar(text("SELECT count(*) FROM project_pen_names")) == 0
            db.execute(text("INSERT INTO pen_names(id,owner_id,name) VALUES(1,1,'甲'),(2,2,'乙')"))
            db.execute(text("INSERT INTO project_pen_names(project_id,owner_id,pen_name_id) VALUES(1,1,1)"))
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(text("UPDATE project_pen_names SET pen_name_id=2 WHERE project_id=1"))
        with pytest.raises(RuntimeError, match="拒绝有损降级"):
            command.downgrade(config, "0054")
        with engine.begin() as db:
            db.execute(text("DELETE FROM pen_names WHERE id=1"))
            assert db.scalar(text("SELECT count(*) FROM project_pen_names")) == 0
            assert db.scalar(text("SELECT count(*) FROM projects")) == 1
            db.execute(text("DELETE FROM pen_names"))
            db.execute(text("INSERT INTO pen_names(owner_id,name) VALUES(1,'后建')"))
            assert db.scalar(text("SELECT id FROM pen_names WHERE name='后建'")) > 2
            db.execute(text("DELETE FROM pen_names"))
            assert not db.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0054")
        with engine.connect() as db:
            assert "pen_names" not in inspect(db).get_table_names()
            assert db.scalar(text("SELECT title FROM projects WHERE id=1")) == "旧作品"
    finally:
        engine.dispose()
