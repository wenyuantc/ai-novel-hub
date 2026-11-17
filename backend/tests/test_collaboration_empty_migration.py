"""无用户、作品或生成记录时，0044 可在 SQLite 中无损往返。"""


from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations


def test_0044_empty_sqlite_upgrade_and_downgrade(tmp_path, monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)
    file = tmp_path / "empty-collaboration.db"
    config = _config(file)
    engine = create_engine(f"sqlite:///{file}")
    try:
        command.upgrade(config, "0043")
        with engine.connect() as conn:
            for table in ("users", "projects", "generation_logs"):
                assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        command.upgrade(config, "0044")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0044"
            for table in ("project_collaborators", "series_collaborators", "collaboration_audit_events"):
                assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0043")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0043"
            assert "project_collaborators" not in inspect(conn).get_table_names()
            assert "actor_user_id" not in {col["name"] for col in inspect(conn).get_columns("generation_logs")}
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()
