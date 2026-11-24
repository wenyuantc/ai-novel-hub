"""在停机状态将单文件 SQLite 原生备份恢复到最终路径。"""

import argparse
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from app.services.task_engine.sqlite_lease_pair import SQLiteLeasePairError, initialize_cold_copy


def _snapshot_revision(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("快照完整性检查失败")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("快照外键检查失败")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if not {"alembic_version", "projects", "users"} <= tables:
            raise ValueError("文件缺少 aiNovel 数据库表或迁移版本")
        versions = db.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(versions) != 1 or not isinstance(revision := versions[0][0], str) or not revision.isdecimal():
            raise ValueError("无法识别快照的迁移版本")
        has_lease = {"task_workers", "task_lease_store"} & tables
        if (int(revision) >= 66 and len(has_lease) != 2) or (int(revision) < 66 and has_lease):
            raise ValueError("快照的租约表与迁移版本不一致")
        if (int(revision) >= 67) != ("task_execution_slots" in tables):
            raise ValueError("快照的任务占用表与迁移版本不一致")
        work_schema = (
            "task_work_records" in tables,
            any(row[1] == "cancel_requested_at" for row in db.execute("PRAGMA table_info(tasks)")),
            any(row[1] == "deletion_worker_key" for row in db.execute("PRAGMA table_info(projects)")),
            any(row[1] == "external_worker_key" for row in db.execute("PRAGMA table_info(agent_changes)")),
        )
        if any(present != (int(revision) >= 68) for present in work_schema):
            raise ValueError("快照的跨进程取消结构与迁移版本不一致")
        if int(revision) >= 68:
            columns = {row[1] for row in db.execute("PRAGMA table_info(task_work_records)")}
            if not {"work_key", "kind", "project_id", "worker_key", "task_id", "task_key", "queued_at",
                    "execution_key", "operation_id", "state", "evidence"} <= columns:
                raise ValueError("快照的跨进程工作证据不完整")
        outside_engine_tables = {
            "inspiration_generation_claims": {"session_id", "step", "operation_key", "worker_key"},
            "agent_conversation_operations": {"conversation_id", "operation_key", "worker_key"},
            "project_import_preview_quota": {"id", "revision"},
            "project_import_previews": {"token_hash", "user_id", "expires_at", "revision", "size_bytes"},
            "project_import_preview_chunks": {"token_hash", "chunk_index", "ciphertext"},
        }
        outside_engine_columns = {
            "agent_conversations": {"instance_key"},
            "inspiration_sessions": {"state_revision"},
            "backup_runs": {"worker_key", "recovery_worker_key", "execution_host"},
            "backup_artifacts": {"writer_host"},
        }
        expected = int(revision) >= 69
        for table, required in outside_engine_tables.items():
            if (table in tables) != expected:
                raise ValueError("快照的引擎外状态结构与迁移版本不一致")
            if expected and not required <= {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                raise ValueError("快照的引擎外状态结构不完整")
        for table, required in outside_engine_columns.items():
            present = required & {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if present != (required if expected else set()):
                raise ValueError("快照的引擎外状态结构与迁移版本不一致")
        return revision


def restore_sqlite_snapshot(source: Path, destination: Path, *, previous_lock: Path | None = None) -> str:
    """仅创建新副本。旧主库、旧伴随文件和传入的快照由管理员保管。"""
    source = source.resolve(strict=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    journals = ("-journal", "-wal", "-shm")
    if any(os.path.lexists(f"{source}{suffix}") for suffix in journals):
        raise ValueError("来源有 SQLite 日志，请使用完整下载或解密后的单文件快照")
    sidecar = f"{destination}.leases.sqlite3"
    lock = f"{destination}.leases.lock"
    occupied = (sidecar, lock, *(f"{db}{suffix}" for db in (destination, sidecar, lock) for suffix in journals))
    if any(os.path.lexists(name) for name in occupied):
        raise FileExistsError("目标已有租约或 SQLite 伴随文件，请先停机并完整保留原文件")
    with source.open("rb") as original:
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = os.fstat(fd)
        try:
            with os.fdopen(fd, "wb") as copy:
                shutil.copyfileobj(original, copy, length=1024 * 1024)
                copy.flush()
                os.fsync(copy.fileno())
            revision = _snapshot_revision(destination)
            initialize_cold_copy(destination, previous_lock=previous_lock)
            return revision
        except BaseException:
            # 配对若已创建伴随文件，保留整组以便检查及移走重试，避免只删主库而留下旧锁。
            if (not any(os.path.lexists(name) for name in occupied) and destination.exists()
                    and os.path.samestat(created, destination.lstat())):
                destination.unlink()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="完整下载或解密后的 SQLite 快照")
    parser.add_argument("destination", type=Path, help="已停机、尚未占用的最终主库路径")
    parser.add_argument("--previous-lock", type=Path, help="已移至回滚目录的旧租约锁文件")
    args = parser.parse_args()
    try:
        revision = restore_sqlite_snapshot(args.source, args.destination, previous_lock=args.previous_lock)
    except (OSError, ValueError, sqlite3.Error, SQLiteLeasePairError) as exc:
        parser.exit(1, f"SQLite 恢复失败：{exc}\n")
    print(f"已恢复 SQLite 数据库（迁移 {revision}）。请核对 DATABASE_URL 并执行 alembic upgrade head。")


if __name__ == "__main__":
    main()
