"""共享灵感来源迁移保留撤权屏障并拒绝有损降级。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, event, inspect, text

from app.models import Base
from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations, _schema_contract


@pytest.fixture(autouse=True)
def _foreign_keys(monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)


def test_0050_preserves_origin_after_project_purge_and_blocks_lossy_downgrade(tmp_path):
    config = _config(tmp_path / "origin.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'origin.db'}")
    expected = create_engine("sqlite://")
    invitation, identity = str(uuid4()), str(uuid4())

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        command.upgrade(config, "0049")
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash'),(2,'editor','hash')"
            ))
            conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                              "VALUES(1,1,'作品','','','','planning')"))
            conn.execute(text("INSERT INTO inspiration_sessions(id,user_id,project_id,title,state) "
                              "VALUES(1,2,1,'私人会话','{}')"))
        command.upgrade(config, "0050")
        Base.metadata.create_all(expected)
        assert _schema_contract(inspect(engine), "inspiration_origins") == _schema_contract(
            inspect(expected), "inspiration_origins",
        )
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO inspiration_origins(session_id,project_id,invitation_id,origin_key) "
                              "VALUES(1,1,:invitation,:identity)"), {"invitation": invitation, "identity": identity})
            conn.execute(text("DELETE FROM projects WHERE id=1"))
            assert conn.scalar(text("SELECT project_id FROM inspiration_sessions WHERE id=1")) is None
            assert conn.scalar(text("SELECT origin_key FROM inspiration_origins WHERE session_id=1")) == identity
        with pytest.raises(RuntimeError, match="不能安全降级"):
            command.downgrade(config, "0049")
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0050"
            assert conn.scalar(text("SELECT origin_key FROM inspiration_origins WHERE session_id=1")) == identity
            conn.execute(text("DELETE FROM inspiration_sessions WHERE id=1"))
            assert conn.scalar(text("SELECT count(*) FROM inspiration_origins")) == 0
        command.downgrade(config, "0049")
        assert "inspiration_origins" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        expected.dispose()
