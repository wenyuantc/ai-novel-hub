"""在旧库和冷恢复中保留作品数据，拦截仍有 C5 操作时的降级。"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import delete, inspect, select, text

from app.models import (
    AgentConversation,
    AgentConversationOperation,
    BackupRun,
    InspirationGenerationClaim,
    InspirationSession,
    Project,
    TaskWorker,
    User,
)
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from tests.test_setting_fields_migration import _config
from tests.test_task_worker_migration import migration_db as migration_db


async def test_upgrade_preserves_existing_state_and_active_claim_protects_downgrade(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0068")
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO users(id,username,password_hash) VALUES (1,'c5-migration','x')"))
        await db.execute(text("INSERT INTO inspiration_sessions(id,user_id,title,state) VALUES (1,1,'旧会话','{}')"))
        await db.execute(text(
            "INSERT INTO projects (id,user_id,title,genre,premise,style,status) "
            "VALUES (1,1,'旧作品','','','','planning')"
        ))
        await db.execute(text("INSERT INTO agent_conversations(id,user_id,project_id) VALUES (1,1,1),(2,1,1)"))
        await db.execute(text("INSERT INTO agent_messages(conversation_id,role,content) VALUES (1,'user','旧消息')"))

    await asyncio.to_thread(command.upgrade, cfg, "0069")
    async with engine.begin() as db:
        assert (await db.execute(text(
            "SELECT id,title,state_revision FROM inspiration_sessions"
        ))).one() == (1, "旧会话", 0)
        columns = await db.run_sync(lambda conn: {c["name"] for c in inspect(conn).get_columns("backup_runs")})
        assert {"worker_key", "recovery_worker_key", "execution_host"} <= columns
        keys = (await db.execute(text("SELECT instance_key FROM agent_conversations ORDER BY id"))).scalars().all()
        assert len(set(keys)) == 2
        assert all(UUID(key).version == 4 for key in keys)
        await db.execute(text(
            "INSERT INTO inspiration_generation_claims(session_id,step,operation_key,worker_key) "
            "VALUES (1,'title',:operation,:worker)"
        ), {"operation": str(uuid4()), "worker": str(uuid4())})

    with pytest.raises(RuntimeError, match="inspiration_generation_claims"):
        await asyncio.to_thread(command.downgrade, cfg, "0068")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0069"
        await db.execute(text("DELETE FROM inspiration_generation_claims WHERE session_id=1"))
    await asyncio.to_thread(command.downgrade, cfg, "0068")
    async with engine.connect() as db:
        assert await db.scalar(text("SELECT title FROM inspiration_sessions")) == "旧会话"
        assert await db.scalar(text("SELECT content FROM agent_messages WHERE conversation_id=1")) == "旧消息"
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("project_import_previews"))
    await asyncio.to_thread(command.upgrade, cfg, "0069")
    async with engine.connect() as db:
        assert await db.scalar(text("SELECT state_revision FROM inspiration_sessions")) == 0


def test_sqlite_cold_restore_discards_previews_without_changing_source(tmp_path):
    source, restored = tmp_path / "source.db", tmp_path / "restored.db"
    command.upgrade(_config(source), "0069")
    key = "a" * 64
    with closing(sqlite3.connect(source)) as db:
        db.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'c5-restore','x')")
        db.execute(
            "INSERT INTO project_import_previews(token_hash,user_id,expires_at,revision,size_bytes) "
            "VALUES (?,1,datetime('now','+10 minutes'),1,1)",
            (key,),
        )
        db.execute(
            "INSERT INTO project_import_preview_chunks(token_hash,chunk_index,ciphertext) VALUES (?,0,?)",
            (key, b"synthetic chunk"),
        )
        db.commit()

    assert restore_sqlite_snapshot(source, restored) == "0069"
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("SELECT count(*) FROM project_import_previews").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM project_import_preview_chunks").fetchone() == (0,)
        assert db.execute("SELECT username FROM users").fetchone() == ("c5-restore",)
    with closing(sqlite3.connect(source)) as db:
        assert db.execute("SELECT token_hash FROM project_import_previews").fetchone() == (key,)
        assert db.execute("SELECT ciphertext FROM project_import_preview_chunks").fetchone() == (b"synthetic chunk",)


def test_sqlite_cold_restore_rejects_incomplete_preview_table(tmp_path):
    source, restored = tmp_path / "broken.db", tmp_path / "restored.db"
    command.upgrade(_config(source), "0069")
    with closing(sqlite3.connect(source)) as db:
        db.execute("ALTER TABLE project_import_preview_chunks DROP COLUMN ciphertext")

    with pytest.raises(ValueError, match="引擎外状态结构不完整"):
        restore_sqlite_snapshot(source, restored)
    assert not restored.exists()


async def test_worker_registration_preserves_terminal_evidence_until_c5_work_is_released(app, peer_app_factory):
    terminal = datetime.now(UTC) - timedelta(days=8)
    backup, recovery, inspiration, agent, unused = (str(uuid4()) for _ in range(5))
    keys = {backup, recovery, inspiration, agent, unused}
    async with app.state.sessionmaker() as db:
        db.add_all(TaskWorker(worker_key=key, hostname="old-worker", pid=1, stopped_at=terminal) for key in keys)
        user = User(username="c5-worker-retention", password_hash="fixture")
        db.add(user)
        await db.flush()
        project = Project(user_id=user.id, title="占用归属")
        session = InspirationSession(user_id=user.id)
        db.add_all([project, session])
        await db.flush()
        conversation = AgentConversation(project_id=project.id, user_id=user.id)
        db.add(conversation)
        await db.flush()
        run = BackupRun(
            execution_key=str(uuid4()), instance_key=str(uuid4()), source="manual", settings_revision=1,
            config_snapshot={}, database_engine="sqlite", encrypted=False, file_format="sqlite3", status="running",
            active_slot=1, worker_key=backup, recovery_worker_key=recovery, created_at=terminal,
        )
        db.add_all([
            run,
            InspirationGenerationClaim(
                session_id=session.id, step="title", operation_key=str(uuid4()), worker_key=inspiration,
            ),
            AgentConversationOperation(
                conversation_id=conversation.id, operation_key=str(uuid4()), worker_key=agent,
            ),
        ])
        await db.commit()
        run_id = run.id

    await peer_app_factory()
    async with app.state.sessionmaker() as db:
        assert set(await db.scalars(select(TaskWorker.worker_key).where(TaskWorker.worker_key.in_(keys)))) == (
            keys - {unused}
        )
        await db.execute(delete(BackupRun).where(BackupRun.id == run_id))
        await db.execute(delete(InspirationGenerationClaim).where(InspirationGenerationClaim.worker_key == inspiration))
        await db.execute(delete(AgentConversationOperation).where(AgentConversationOperation.worker_key == agent))
        await db.commit()

    await peer_app_factory()
    async with app.state.sessionmaker() as db:
        assert not list(await db.scalars(select(TaskWorker.worker_key).where(TaskWorker.worker_key.in_(keys))))
