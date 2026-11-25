"""0046 原位扩列保留章节子行，并拒绝丢弃可能仍在使用的占用。"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.models import Base, Chapter
from app.services import scene_service
from app.services.backups.database import snapshot_database
from app.services.project_transfer.registry import RULES_BY_TABLE
from tests.helpers import create_project, register
from tests.test_setting_fields_migration import _migration_fk_on_connect, _schema_contract


def _config(path):
    # 内存配置避免系统的 C locale 用 ASCII 解码含中文注释的 alembic.ini。
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


@pytest.fixture(autouse=True)
def _migration_connection_starts_with_fk_on(monkeypatch):
    _migration_fk_on_connect(monkeypatch)


def test_0046_sqlite_upgrade_preserves_children_and_refuses_lossy_downgrade(tmp_path):
    path = tmp_path / "chapter-claim.db"
    config = _config(path)
    engine = create_engine(f"sqlite:///{path}")
    expected = create_engine("sqlite://")
    try:
        command.upgrade(config, "0045")
        with engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
            conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                              "VALUES(1,1,'旧书','','','','writing')"))
            conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                              "VALUES(1,1,1,'第一章','原文','draft',2),(2,1,2,'旧占用','旧正文','generating',3)"))
            conn.execute(text("INSERT INTO scenes(id,chapter_id,title,content,sort) VALUES(1,1,'旧景','原文',0)"))
            conn.execute(text("INSERT INTO chapter_versions(id,chapter_id,seq,source,content,word_count) "
                              "VALUES(1,1,1,'manual','原文',2)"))
        command.upgrade(config, "0046")
        Base.metadata.create_all(expected)
        migrated = _schema_contract(inspect(engine), "chapters")
        metadata = _schema_contract(inspect(expected), "chapters")
        # 旧 content_revision 的 SQLite 默认值文字在既有迁移中为 0，ORM 为 '0'；不属于本次变更。
        migrated["columns"].pop("content_revision")
        metadata["columns"].pop("content_revision")
        assert migrated == metadata
        with engine.connect() as conn:
            assert tuple(conn.execute(text("SELECT generation_claim_key,generation_claim_task_key,"
                                           "generation_claim_prev_status FROM chapters WHERE id=2")).one()) \
                == (None, None, None)
            assert conn.scalar(text("SELECT content FROM scenes WHERE id=1")) == "原文"
            assert conn.scalar(text("SELECT content FROM chapter_versions WHERE id=1")) == "原文"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        key, task = str(uuid4()), str(uuid4())
        for assignment in (
            "generation_claim_key=:key", "generation_claim_task_key=:task",
            "generation_claim_prev_status='draft'",
            "generation_claim_key=:key,generation_claim_task_key=:task,generation_claim_prev_status='draft'",
        ):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text(f"UPDATE chapters SET {assignment} WHERE id=1"), {"key": key, "task": task})
        with engine.begin() as conn:
            conn.execute(text("UPDATE chapters SET status='generating',generation_claim_key=:key,"
                              "generation_claim_task_key=:task,generation_claim_prev_status='draft' WHERE id=1"),
                         {"key": key, "task": task})
        # 整库备份是持久事实快照，不能像单书迁移一样抹去当前运行身份。
        snapshot = tmp_path / "system-snapshot.sqlite3"
        info = snapshot_database(f"sqlite+aiosqlite:///{path}", snapshot,
                                 deadline=time.monotonic() + 10, stop_event=threading.Event())
        assert info.schema_revision == "0046"
        with sqlite3.connect(snapshot) as restored:
            assert restored.execute(
                "SELECT status,content,generation_claim_key,generation_claim_task_key,"
                "generation_claim_prev_status FROM chapters WHERE id=1"
            ).fetchone() == ("generating", "原文", key, task, "draft")
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0045")
        with engine.begin() as conn:
            conn.execute(text("UPDATE chapters SET status='draft',generation_claim_key=NULL,"
                              "generation_claim_task_key=NULL,generation_claim_prev_status=NULL WHERE id=1"))
        # 旧 generating 即使没有 claim 也不能假定执行体已排空。
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0045")
        with engine.begin() as conn:
            conn.execute(text("UPDATE chapters SET status='done' WHERE id=2"))
        command.downgrade(config, "0045")
        with engine.connect() as conn:
            columns = {column["name"] for column in inspect(conn).get_columns("chapters")}
            assert not {"generation_claim_key", "generation_claim_task_key", "generation_claim_prev_status"} & columns
            assert conn.scalar(text("SELECT content FROM scenes WHERE id=1")) == "原文"
            assert conn.scalar(text("SELECT content FROM chapter_versions WHERE id=1")) == "原文"
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()
        expected.dispose()


async def test_single_book_backup_never_transfers_or_accepts_chapter_claim(app, client):
    await app.state.task_engine.stop()
    await register(client, "chapter-backup-owner")
    project = await create_project(client)
    key, task = str(uuid4()), str(uuid4())
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=project["id"], title="备份章", content="原正文", status="generating",
                          generation_claim_key=key, generation_claim_task_key=task,
                          generation_claim_prev_status="draft")
        db.add(chapter)
        await db.flush()
        await scene_service.initialize_chapter(db, chapter)
        await db.commit()
    response = await client.get(f"/api/v1/projects/{project['id']}/backup")
    assert response.status_code == 200, response.text
    backup = response.json()
    assert backup["app_schema"] == "0074"
    row = backup["tables"]["chapters"][0]
    fields = {"generation_claim_key", "generation_claim_task_key", "generation_claim_prev_status"}
    assert fields <= set(RULES_BY_TABLE["chapters"].exclude)
    assert not fields.intersection(row)
    row.update(generation_claim_key=key, generation_claim_task_key=task, generation_claim_prev_status="draft")
    preview = await client.post("/api/v1/projects/import/preview", files={
        "file": ("backup.json", json.dumps(backup, ensure_ascii=False).encode()),
    })
    assert preview.status_code == 200, preview.text
    imported = await client.post("/api/v1/projects/import", json={"token": preview.json()["token"]})
    assert imported.status_code == 201, imported.text
    async with app.state.sessionmaker() as db:
        copied = await db.scalar(select(Chapter).where(Chapter.project_id == imported.json()["project"]["id"]))
        assert copied is not None and (copied.status, copied.content) == ("draft", "原正文")
        assert (copied.generation_claim_key, copied.generation_claim_task_key,
                copied.generation_claim_prev_status) == (None, None, None)
