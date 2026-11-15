"""分析派生索引的去重证据独立于任务生命周期，迁移不得猜测历史证据。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import AnalysisIndexReceipt, Base
from tests.helpers import create_project, register
from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations, _schema_contract


@pytest.fixture(autouse=True)
def _foreign_keys(monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)


def _seed(connection):
    connection.execute(text(
        "INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash'),(2,'editor','hash')"
    ))
    connection.execute(text(
        "INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
        "VALUES(1,1,'旧书','','','','writing')"
    ))
    parent, child = str(uuid4()), str(uuid4())
    connection.execute(text(
        "INSERT INTO tasks(id,statistics_key,user_id,project_id,kind,status,input) VALUES "
        "(1,:parent,2,1,'chapter.analyze','succeeded','{}'),"
        "(2,:child,2,1,'kb.index','succeeded','{}')"
    ), {"parent": parent, "child": child})
    return parent, child


def _receipt(parent, child, **changes):
    values = {
        "receipt_key": str(uuid4()), "source_kind": "single", "source_key": str(uuid4()), "outcome": "queued",
        "source_task_key": parent, "parent_task_key": parent, "parent_execution_key": str(uuid4()),
        "parent_queued_at": datetime.now(UTC), "actor_id": 2, "actor_token_version": 3,
        "project_id": 1, "chapter_id": 4, "analysis_id": 5,
        "grant_invitation_id": str(uuid4()), "grant_revision": 2,
        "analysis_hash": "a" * 64, "content_hash": "b" * 64, "input_hash": "c" * 64,
        "selection_hash": "d" * 64, "memory_hash": "e" * 64,
        "memory_refs": json.dumps([{"ref_id": 13, "analysis_id": 5, "retrieval_id": 21,
                                    "version": 1, "content_hash": "f" * 64}]),
        "child_task_key": child,
    }
    values.update(changes)
    return values


def _insert(connection, values):
    columns = ",".join(values)
    placeholders = ",".join(f":{column}" for column in values)
    connection.execute(text(f"INSERT INTO analysis_index_receipts ({columns}) VALUES ({placeholders})"), values)


def test_0051_migrates_existing_data_without_fabricating_receipts_and_matches_metadata(tmp_path):
    config = _config(tmp_path / "receipt.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'receipt.db'}")
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0050")
        with engine.begin() as conn:
            parent, child = _seed(conn)
        command.upgrade(config, "0051")
        Base.metadata.create_all(expected)
        assert _schema_contract(inspect(engine), "analysis_index_receipts") == _schema_contract(
            inspect(expected), "analysis_index_receipts",
        )
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM analysis_index_receipts")) == 0
            assert list(conn.scalars(text("SELECT statistics_key FROM tasks ORDER BY id"))) == [parent, child]
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0050")
        assert "analysis_index_receipts" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        expected.dispose()


def test_0051_receipts_survive_task_and_actor_deletion_but_follow_project(tmp_path):
    config = _config(tmp_path / "lifecycle.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    try:
        command.upgrade(config, "0051")
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            parent, child = _seed(conn)
            receipt = _receipt(parent, child)
            _insert(conn, receipt)
            conn.execute(text("DELETE FROM tasks WHERE id IN (1,2)"))
            conn.execute(text("DELETE FROM users WHERE id=2"))
            row = conn.execute(text(
                "SELECT actor_id,parent_task_key,child_task_key FROM analysis_index_receipts"
            )).one()
            assert tuple(row) == (2, parent, child)
        with pytest.raises(RuntimeError, match="不能安全降级"):
            command.downgrade(config, "0050")
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0051"
            conn.execute(text("DELETE FROM projects WHERE id=1"))
            assert conn.scalar(text("SELECT count(*) FROM analysis_index_receipts")) == 0
        command.downgrade(config, "0050")
    finally:
        engine.dispose()


def test_0051_unique_source_child_and_immutable_snapshots(tmp_path):
    config = _config(tmp_path / "unique.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'unique.db'}")
    try:
        command.upgrade(config, "0051")
        with engine.begin() as conn:
            parent, child = _seed(conn)
            receipt = _receipt(parent, child)
            _insert(conn, receipt)
        aliased_source = str(uuid4())
        conflicts = (
            _receipt(parent, str(uuid4()), receipt_key=receipt["receipt_key"]),
            _receipt(parent, str(uuid4()), source_key=receipt["source_key"]),
            _receipt(parent, child),
            _receipt(parent, aliased_source, source_key=aliased_source),
            _receipt(parent, str(uuid4()), receipt_key="not-a-uuid"),
            _receipt(parent, str(uuid4()), grant_revision=None),
            _receipt(parent, str(uuid4()), selection_hash="bad"),
            _receipt(parent, str(uuid4()), memory_refs="{}"),
            _receipt(parent, str(uuid4()), memory_refs="[]"),
            _receipt(parent, str(uuid4()), actor_id="not-an-id"),
        )
        for conflict in conflicts:
            with pytest.raises(IntegrityError), engine.begin() as conn:
                _insert(conn, conflict)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE analysis_index_receipts SET child_task_key=:key"), {"key": str(uuid4())})
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT child_task_key FROM analysis_index_receipts")) == child
    finally:
        engine.dispose()


def test_0051_no_work_is_durable_and_excludes_a_child(tmp_path):
    config = _config(tmp_path / "no-work.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'no-work.db'}")
    try:
        command.upgrade(config, "0051")
        with engine.begin() as conn:
            parent, child = _seed(conn)
            no_work = _receipt(parent, None, outcome="no_work", memory_refs="[]")
            _insert(conn, no_work)
            _insert(conn, _receipt(parent, None, outcome="no_work", memory_refs="[]"))
        for invalid in (
            _receipt(parent, None, outcome="queued"),
            _receipt(parent, child, outcome="no_work"),
            _receipt(parent, str(uuid4()), source_key=no_work["source_key"]),
        ):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                _insert(conn, invalid)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM analysis_index_receipts WHERE outcome='no_work'")) == 2
        with pytest.raises(RuntimeError, match="不能安全降级"):
            command.downgrade(config, "0050")
    finally:
        engine.dispose()


def test_0051_empty_receipts_cannot_downgrade_with_an_active_source(tmp_path):
    config = _config(tmp_path / "active.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'active.db'}")
    try:
        command.upgrade(config, "0051")
        with engine.begin() as conn:
            _seed(conn)
        for status, execution in (("running", str(uuid4())), ("succeeded", str(uuid4())), ("interrupted", None)):
            with engine.begin() as conn:
                conn.execute(text("UPDATE tasks SET status=:status,execution_key=:execution WHERE id=1"), {
                    "status": status, "execution": execution,
                })
            with pytest.raises(RuntimeError, match="不能安全降级"):
                command.downgrade(config, "0050")
            with engine.connect() as conn:
                assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0051"
        with engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET status='succeeded',execution_key=NULL WHERE id=1"))
        command.downgrade(config, "0050")
    finally:
        engine.dispose()


def test_create_all_enforces_immutable_receipts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'create-all.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            parent, child = _seed(conn)
            receipt = _receipt(parent, child)
            _insert(conn, receipt)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE analysis_index_receipts SET actor_id=3"))
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT actor_id FROM analysis_index_receipts")) == 2
    finally:
        engine.dispose()


async def test_project_backup_does_not_export_index_receipt(app, client):
    user = await register(client)
    project = await create_project(client)
    parent, child = str(uuid4()), str(uuid4())
    values = _receipt(parent, child, actor_id=user["id"], project_id=project["id"])
    values["memory_refs"] = json.loads(values["memory_refs"])
    async with app.state.sessionmaker() as db:
        db.add(AnalysisIndexReceipt(**values))
        await db.commit()
    response = await client.get(f"/api/v1/projects/{project['id']}/backup", params={"logs": "true"})
    assert response.status_code == 200, response.text
    assert int(response.json()["app_schema"]) >= 51
    assert "analysis_index_receipts" not in response.json()["tables"]
    for key in (values["receipt_key"], values["source_key"], values["child_task_key"]):
        assert key not in response.text


def test_0051_child_receipt_and_statistics_roll_back_together(tmp_path):
    config = _config(tmp_path / "atomic.db")
    engine = create_engine(f"sqlite:///{tmp_path / 'atomic.db'}")
    try:
        command.upgrade(config, "0051")
        with engine.begin() as conn:
            parent, child = _seed(conn)
            conn.execute(text("DELETE FROM tasks WHERE id=2"))
        receipt = _receipt(parent, child)
        with pytest.raises(RuntimeError, match="rollback"), engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO tasks(id,statistics_key,user_id,project_id,kind,status,input) "
                "VALUES(2,:child,2,1,'kb.index','queued','{}')"
            ), {"child": child})
            conn.execute(text(
                "INSERT INTO admin_task_statistics(key,kind,origin,status) VALUES(:child,'kb.index','new','queued')"
            ), {"child": child})
            _insert(conn, receipt)
            raise RuntimeError("rollback")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM tasks WHERE statistics_key=:child"), {"child": child}) == 0
            assert conn.scalar(
                text("SELECT count(*) FROM admin_task_statistics WHERE key=:child"), {"child": child},
            ) == 0
            assert conn.scalar(text("SELECT count(*) FROM analysis_index_receipts")) == 0
    finally:
        engine.dispose()
