import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.models import Base, Character, Project, Series, SeriesBook, SeriesEntry, SeriesEntryUse, User, WorldSetting
from tests.test_setting_fields_migration import _config, _schema_contract

_TABLES = ("series", "series_books", "series_entries", "series_entry_uses")


def test_0038_sqlite_upgrade_preserves_stored_books_and_safe_downgrade(tmp_path):
    file = tmp_path / "series.db"
    config = _config(file)
    migrated = create_engine(f"sqlite:///{file}")
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0037")
        with migrated.begin() as conn:
            conn.execute(User.__table__.insert().values(id=10, username="owner", password_hash="hash"))
            conn.execute(Project.__table__.insert().values(id=50, user_id=10, title="旧作品"))
            conn.execute(Character.__table__.insert().values(id=90, project_id=50, name="旧角色"))
            conn.execute(WorldSetting.__table__.insert().values(id=100, project_id=50, title="旧世界观"))
            before = conn.exec_driver_sql("SELECT name,seq FROM sqlite_sequence ORDER BY name").all()
        command.upgrade(config, "0038")
        Base.metadata.create_all(expected)
        for table in (*_TABLES, "projects", "characters", "world_settings"):
            assert _schema_contract(inspect(migrated), table) == _schema_contract(inspect(expected), table), table
        with migrated.connect() as conn:
            assert conn.scalar(select(Character.name).where(Character.id == 90)) == "旧角色"
            assert conn.scalar(select(WorldSetting.title).where(WorldSetting.id == 100)) == "旧世界观"
            assert not conn.execute(select(SeriesBook.project_id)).all()
            assert conn.exec_driver_sql("SELECT name,seq FROM sqlite_sequence ORDER BY name").all() == before
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0037")
        assert all(not inspect(migrated).has_table(table) for table in _TABLES)
        with migrated.connect() as conn:
            assert conn.scalar(select(Character.name)) == "旧角色"
            assert conn.scalar(select(WorldSetting.title)) == "旧世界观"
    finally:
        migrated.dispose()
        expected.dispose()


def test_0038_binding_constraints_and_lossy_downgrade(tmp_path):
    file = tmp_path / "series.db"
    config = _config(file)
    migrated = create_engine(f"sqlite:///{file}")
    try:
        command.upgrade(config, "0038")
        with migrated.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.execute(User.__table__.insert(), [
                {"id": owner, "username": f"user-{owner}", "password_hash": "hash"} for owner in (1, 2)
            ])
            conn.execute(Project.__table__.insert(), [
                {"id": pid, "user_id": owner, "title": f"book-{pid}"} for pid, owner in ((1, 1), (2, 1), (3, 2))
            ])
            conn.execute(Character.__table__.insert(), [
                {"id": pid * 10, "project_id": pid, "name": f"char-{pid}"} for pid in (1, 2, 3)
            ])
            conn.execute(Series.__table__.insert().values(id=101, owner_id=1, name="系列"))
            conn.execute(SeriesBook.__table__.insert(), [
                {"project_id": pid, "series_id": 101, "owner_id": 1} for pid in (1, 2)
            ])
            conn.execute(SeriesEntry.__table__.insert().values(
                id=1001, series_id=101, kind="character", display_name="共享角色",
                data={"name": "共享角色"}, ai_settings={"ai_mode": "detected"},
            ))
            conn.execute(SeriesEntryUse.__table__.insert(), [
                {"project_id": pid, "series_id": 101, "series_entry_id": 1001,
                 "kind": "character", "character_id": pid * 10}
                for pid in (1, 2)
            ])
        for sql in (
            "INSERT INTO series_books(project_id,series_id,owner_id) VALUES(3,101,2)",
            "UPDATE series_entry_uses SET character_id=30 WHERE project_id=1",
            "DELETE FROM characters WHERE id=10",
            "DELETE FROM series_entries WHERE id=1001",
        ):
            with pytest.raises(IntegrityError), migrated.begin() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.exec_driver_sql(sql)
        with pytest.raises(RuntimeError, match="不能无损降级"):
            command.downgrade(config, "0037")
        with migrated.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0038"
            conn.execute(Project.__table__.delete().where(Project.id == 1))
            assert conn.scalars(select(SeriesEntryUse.project_id)).all() == [2]
            assert conn.scalar(select(SeriesEntry.id)) == 1001
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        migrated.dispose()
