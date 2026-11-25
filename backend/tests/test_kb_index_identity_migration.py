"""0045 文档索引占用身份必须无损迁移且成对保存。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base
from app.services.project_transfer.registry import RULES_BY_TABLE
from tests.test_setting_fields_migration import _config, _migration_fk_on_connect, _schema_contract


@pytest.fixture(autouse=True)
def _migration_connection_starts_with_fk_on(monkeypatch):
    _migration_fk_on_connect(monkeypatch)


def test_0045_sqlite_identity_constraints_and_lossless_downgrade(tmp_path):
    path = tmp_path / "kb-index.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0044")
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
            conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                              "VALUES(1,1,'旧书','','','','writing')"))
            conn.execute(text("INSERT INTO kb_documents(id,project_id,filename,sha256,text) "
                              "VALUES(1,1,'旧文件',:sha,'文档原文')"), {"sha": "a" * 64})
            conn.execute(text("INSERT INTO kb_chunks(id,document_id,seq,content) VALUES(1,1,1,'分块原文')"))
            conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                              "VALUES(1,1,1,'kb.index','queued','{}')"))
        command.upgrade(config, "0045")
        Base.metadata.create_all(expected)
        assert _schema_contract(inspect(engine), "kb_documents") == _schema_contract(inspect(expected), "kb_documents")
        expected_tasks = _schema_contract(inspect(expected), "tasks")
        # 0066 才引入执行者归属与待核对时刻，0045 的任务表按当时的结构比较。
        expected_tasks["columns"].pop("worker_key")
        expected_tasks["columns"].pop("recovered_at")
        # 本人跨进程取消意图由 0068 引入。
        expected_tasks["columns"].pop("cancel_requested_at")
        assert _schema_contract(inspect(engine), "tasks") == expected_tasks
        with engine.connect() as conn:
            assert conn.execute(text("SELECT index_task_key,index_task_queued_at,index_claim_key "
                                     "FROM kb_documents")).one() == (None, None, None)
            assert conn.scalar(text("SELECT execution_key FROM tasks WHERE id=1")) is None
            assert conn.scalar(text("SELECT content FROM kb_chunks WHERE id=1")) == "分块原文"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        key = str(uuid4())
        for assignment in ("index_task_key=:key", "index_task_queued_at='2026-01-01 00:00:00'",
                           "index_claim_key=:key", "index_task_key=:key,index_claim_key=:key"):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text(f"UPDATE kb_documents SET {assignment} WHERE id=1"), {"key": key})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET execution_key='short' WHERE id=1"))
        with engine.begin() as conn:
            conn.execute(text("UPDATE kb_documents SET index_task_key=:key, "
                              "index_task_queued_at='2026-01-01 00:00:00',index_claim_key=:claim WHERE id=1"),
                         {"key": key, "claim": str(uuid4())})
            conn.execute(text("UPDATE tasks SET execution_key=:key WHERE id=1"), {"key": str(uuid4())})
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0044")
        with engine.begin() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0045"
            conn.execute(text("UPDATE kb_documents SET index_task_key=NULL,index_task_queued_at=NULL,"
                              "index_claim_key=NULL WHERE id=1"))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0044")
        with engine.begin() as conn:
            conn.execute(text("UPDATE tasks SET execution_key=NULL WHERE id=1"))
        command.downgrade(config, "0044")
        with engine.connect() as conn:
            columns = {col["name"] for col in inspect(conn).get_columns("kb_documents")}
            assert not {"index_task_key", "index_task_queued_at", "index_claim_key"} & columns
            assert "execution_key" not in {col["name"] for col in inspect(conn).get_columns("tasks")}
            assert conn.scalar(text("SELECT text FROM kb_documents WHERE id=1")) == "文档原文"
            assert conn.scalar(text("SELECT content FROM kb_chunks WHERE id=1")) == "分块原文"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()
        expected.dispose()


def test_document_index_ownership_is_not_in_single_book_backup():
    rule = RULES_BY_TABLE["kb_documents"]
    assert {"index_task_key", "index_task_queued_at", "index_claim_key"} <= set(rule.exclude)
    assert not {"index_task_key", "index_task_queued_at", "index_claim_key"} & set(rule.columns)
