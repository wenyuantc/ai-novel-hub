"""The collaboration migration keeps old log references and rejects data loss."""

import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base
from tests.test_setting_fields_migration import _config, _migration_fk_on_connect, _schema_contract


@pytest.fixture(autouse=True)
def _migration_connection_starts_with_fk_on(monkeypatch):
    _migration_fk_on_connect(monkeypatch)


def _seed_legacy(conn):
    conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'owner','hash'),(2,'guest','hash')"))
    conn.execute(text(
        "INSERT INTO projects(id,user_id,title,genre,premise,style,status) VALUES (1,1,'旧作','','','','planning')"
    ))
    conn.execute(text(
        "INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
        "VALUES (1,1,1,'第一章','原文','draft',2)"
    ))
    conn.execute(text(
        "INSERT INTO generation_logs(id,project_id,chapter_id,kind,provider,model,params,messages,status,error,"
        "output_chars,duration_ms) VALUES(1,1,1,'generate','test','m','{}','[]','success','',2,1)"
    ))
    conn.execute(text(
        "INSERT INTO chapter_versions(id,chapter_id,seq,source,content,word_count,generation_log_id) "
        "VALUES(1,1,1,'generate','原文',2,1)"
    ))


def test_0044_sqlite_preserves_backfilled_logs_and_version_foreign_keys(tmp_path):
    file = tmp_path / "collab.db"
    config = _config(file)
    engine = create_engine(f"sqlite:///{file}")
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0043")
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            _seed_legacy(conn)
        command.upgrade(config, "0044")
        assert {"project_collaborators", "series_collaborators", "collaboration_audit_events"} <= set(
            inspect(engine).get_table_names()
        )
        Base.metadata.create_all(expected)
        for table in ("project_collaborators", "series_collaborators", "collaboration_audit_events"):
            assert _schema_contract(inspect(engine), table) == _schema_contract(inspect(expected), table)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT actor_user_id FROM generation_logs WHERE id=1")) == 1
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0043")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
            assert "actor_user_id" not in {column["name"] for column in inspect(conn).get_columns("generation_logs")}
    finally:
        engine.dispose()
        expected.dispose()


@pytest.mark.parametrize("blocked_by", ["project", "series", "audit", "other_actor", "unknown_actor"])
def test_0044_rejects_lossy_downgrade(tmp_path, blocked_by):
    file = tmp_path / f"collab-{blocked_by}.db"
    config = _config(file)
    engine = create_engine(f"sqlite:///{file}")
    try:
        command.upgrade(config, "0043")
        with engine.begin() as conn:
            _seed_legacy(conn)
        command.upgrade(config, "0044")
        with engine.begin() as conn:
            if blocked_by == "project":
                conn.execute(text(
                    "INSERT INTO project_collaborators(project_id,user_id,role,status,invitation_id,revision) "
                    "VALUES(1,2,'editor','accepted',:invitation,1)"
                ), {"invitation": str(uuid.uuid4())})
            elif blocked_by == "series":
                conn.execute(text("INSERT INTO series(id,owner_id,name) VALUES(1,1,'系列')"))
                conn.execute(text(
                    "INSERT INTO series_collaborators(series_id,user_id,role,status,invitation_id,revision) "
                    "VALUES(1,2,'editor','accepted',:invitation,1)"
                ), {"invitation": str(uuid.uuid4())})
            elif blocked_by == "audit":
                conn.execute(text(
                    "INSERT INTO collaboration_audit_events(operation_id,scope,resource_id,action,result,summary) "
                    "VALUES(:operation,'project',1,'invite','success','{}')"
                ), {"operation": str(uuid.uuid4())})
            else:
                conn.execute(text("UPDATE generation_logs SET actor_user_id=:actor WHERE id=1"), {
                    "actor": 2 if blocked_by == "other_actor" else None,
                })
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0043")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0044"
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
    finally:
        engine.dispose()


def test_0044_sqlite_collaborator_constraints_and_user_deletion(tmp_path):
    file = tmp_path / "constraints.db"
    config = _config(file)
    engine = create_engine(f"sqlite:///{file}")
    try:
        command.upgrade(config, "0043")
        with engine.begin() as conn:
            _seed_legacy(conn)
        command.upgrade(config, "0044")
        invitation = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.execute(text(
                "INSERT INTO project_collaborators(project_id,user_id,role,status,invitation_id,revision) "
                "VALUES(1,2,'viewer','pending',:invitation,1)"
            ), {"invitation": invitation})
            conn.execute(text("UPDATE generation_logs SET actor_user_id=2 WHERE id=1"))
        for statement in (
            "UPDATE project_collaborators SET role='owner'",
            "UPDATE project_collaborators SET status='unknown'",
            "UPDATE project_collaborators SET revision=0",
            "UPDATE project_collaborators SET invited_by_user_id=2",
        ):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                conn.execute(text(statement))
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.execute(text("DELETE FROM users WHERE id=2"))
            assert conn.scalar(text("SELECT count(*) FROM project_collaborators")) == 0
            assert conn.scalar(text("SELECT actor_user_id FROM generation_logs WHERE id=1")) is None
            assert conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
    finally:
        engine.dispose()
