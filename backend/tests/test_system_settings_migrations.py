"""0027仅新增实例表，兼容现存业务数据及完整迁移链。"""

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def test_0027_upgrade_downgrade_preserves_business_and_restricts_singleton(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    db_file = tmp_path / "system-settings-migration.db"
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "0026")
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.begin() as db:
            db.execute(text("INSERT INTO users (id,username,password_hash) VALUES (1,'保留用户','x')"))
        original_tables = set(inspect(engine).get_table_names())
        command.upgrade(config, "0027")
        assert set(inspect(engine).get_table_names()) == original_tables | {"system_settings"}
        assert inspect(engine).get_foreign_keys("system_settings") == []
        with engine.begin() as db:
            assert db.execute(text("SELECT count(*) FROM system_settings")).scalar_one() == 0
            db.execute(text(
                "INSERT INTO system_settings (id,revision,overrides,last_operation_id,updated_at) "
                "VALUES (1,1,:overrides,'00000000-0000-0000-0000-000000000001',CURRENT_TIMESTAMP)"
            ), {"overrides": json.dumps({"registration_enabled": False})})
            assert db.execute(text("SELECT username FROM users WHERE id=1")).scalar_one() == "保留用户"
            assert db.execute(text("PRAGMA foreign_key_check")).all() == []
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(text(
                "INSERT INTO system_settings VALUES (2,1,'{}','00000000-0000-0000-0000-000000000002',CURRENT_TIMESTAMP)"
            ))
        with pytest.raises(IntegrityError), engine.begin() as db:
            db.execute(text("UPDATE system_settings SET revision=0"))
        command.downgrade(config, "0026")
        assert set(inspect(engine).get_table_names()) == original_tables
        with engine.connect() as db:
            assert db.execute(text("SELECT username FROM users WHERE id=1")).scalar_one() == "保留用户"
    finally:
        engine.dispose()
