"""容器内真实数据库/文件探针；stdin 接收秘密，只输出计数、校验和和布尔结果。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path("/acceptance-data")
sys.path.insert(0, "/app/backend")


def canonical(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [canonical(item) for item in value]
    return value


async def database_fingerprint(data: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import MetaData, select, text

    from app.core.database import create_engine
    from app.core.security import SecretCipher

    engine = create_engine(data.get("database_url", os.environ["DATABASE_URL"]))
    try:
        async with engine.connect() as connection:
            metadata = MetaData()
            await connection.run_sync(metadata.reflect)
            wanted = data.get("tables", [
                "users", "roles", "user_roles", "authorization_state", "user_quotas", "system_settings",
                "ai_configs", "projects", "project_covers", "world_settings", "characters", "outlines",
                "chapters", "chapter_versions", "chapter_characters", "acceptance_wal_marker",
            ])
            fingerprints = {}
            for name in wanted:
                if name not in metadata.tables:
                    continue
                table = metadata.tables[name]
                rows = (await connection.execute(select(table))).mappings().all()
                values = sorted(json.dumps(canonical(dict(row)), ensure_ascii=False, sort_keys=True) for row in rows)
                fingerprints[name] = {
                    "rows": len(rows), "sha256": hashlib.sha256("\n".join(values).encode()).hexdigest(),
                }
            config = metadata.tables["ai_configs"]
            encrypted = (await connection.execute(select(config.c.api_key_enc).limit(1))).scalar_one()
            decrypts = SecretCipher(os.environ["ENCRYPTION_KEY"]).decrypt(encrypted) == data["provider_secret"]
            if engine.dialect.name == "sqlite":
                integrity = (await connection.execute(text("PRAGMA integrity_check"))).scalar_one()
                foreign_keys = (await connection.execute(text("PRAGMA foreign_key_check"))).all()
                valid = integrity == "ok" and not foreign_keys
            else:
                valid = True  # PostgreSQL restore 默认启用外键约束；pg_restore --exit-on-error 必须退出0。
            schema = (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
            return {"tables": fingerprints, "credential_decrypts": decrypts, "integrity": valid, "schema": schema}
    finally:
        await engine.dispose()


def gpg_decrypt(data: dict[str, Any]) -> dict[str, Any]:
    source = ROOT / data["source"]
    destination = ROOT / data["destination"]
    password = data["password"]
    # 先完整解密、检查返回码，再由调用者恢复；密码不放 argv、env、磁盘文件。
    with tempfile.TemporaryDirectory(prefix="acceptance-gpg-", dir=ROOT) as home:
        os.chmod(home, 0o700)
        try:
            process = subprocess.run(
                ["gpg", "--batch", "--yes", "--no-symkey-cache", "--pinentry-mode", "loopback",
                 "--homedir", home, "--passphrase-fd", "0", "--output", str(destination), "--decrypt", str(source)],
                input=(password + "\n").encode(), capture_output=True, timeout=45, check=False,
            )
            if process.returncode:
                destination.unlink(missing_ok=True)
                return {"accepted": False}
            destination.chmod(0o600)
            return {"accepted": True, "size": destination.stat().st_size}
        finally:
            subprocess.run(["gpgconf", "--homedir", home, "--kill", "all"], capture_output=True, check=False)


def s3(data: dict[str, Any]) -> dict[str, Any]:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3", endpoint_url="https://s3.acceptance.test:9000", region_name="us-east-1",
        aws_access_key_id=data["access_key"], aws_secret_access_key=data["secret_key"],
        verify=str(ROOT / "tls/ca.pem"),
        config=Config(s3={"addressing_style": "path"}, proxies={}, connect_timeout=5, read_timeout=10),
    )
    try:
        operation = data["operation"]
        if operation == "create":
            for bucket in data["buckets"]:
                client.create_bucket(Bucket=bucket)
                client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
            return {"created": True}
        if operation == "versions":
            result = client.list_object_versions(Bucket=data["bucket"], Prefix=data.get("prefix", ""))
            return {
                "versions": len(result.get("Versions", [])),
                "delete_markers": len(result.get("DeleteMarkers", [])),
                "version_ids_present": all(item.get("VersionId") for item in result.get("Versions", [])),
                "multipart_versions": sum("-" in item.get("ETag", "") for item in result.get("Versions", [])),
            }
        if operation == "download":
            client.download_file(data["bucket"], data["key"], str(ROOT / data["destination"]))
            return {"downloaded": True}
        raise ValueError("unknown action")
    finally:
        client.close()


def wal_marker(data: dict[str, Any]) -> dict[str, Any]:
    source = ROOT / "sqlite/source.sqlite3"
    with sqlite3.connect(source) as connection:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("CREATE TABLE acceptance_wal_marker (id INTEGER PRIMARY KEY, marker TEXT NOT NULL)")
        connection.execute("INSERT INTO acceptance_wal_marker VALUES (1, ?)", (data["marker"],))
        connection.commit()
    return {"wal_mode": mode == "wal"}


async def schedule_due() -> dict[str, Any]:
    from sqlalchemy import update

    from app.core.database import create_engine
    from app.models.backup import BackupSettings

    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        async with engine.begin() as connection:
            result = await connection.execute(update(BackupSettings).where(BackupSettings.id == 1).values(
                next_scheduled_at=datetime.now(UTC) - timedelta(days=3),
            ))
            return {"due_fixture_written": result.rowcount == 1}
    finally:
        await engine.dispose()


async def terminal_work_fixture(data: dict[str, Any], *, verify: bool) -> dict[str, Any]:
    """离线合成“终态但清理失败”事实，恢复只处理该真实run身份，不扫未知目录。"""
    from app.core.database import create_engine, create_sessionmaker
    from app.models.backup import BackupRun

    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        async with create_sessionmaker(engine)() as session:
            row = await session.get(BackupRun, data["run_id"])
            if row is None or row.status not in ("succeeded", "partial", "failed", "interrupted"):
                raise ValueError("terminal run required")
            instance = Path(os.environ["BACKUP_DIR"]) / row.instance_key
            known = instance / f".work-{row.execution_key}"
            marker = instance / "acceptance-unrelated-marker"
            unknown = instance / ".work-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
            if verify:
                return {
                    "known_work_removed": not known.exists(),
                    "unrelated_file_preserved": marker.read_bytes() == b"unrelated synthetic file",
                    "unknown_identity_preserved": (unknown / "synthetic.txt").read_bytes() == b"unknown run identity",
                    "original_cleanup_failure_preserved": (
                        row.cleanup_status == "failed" and row.cleanup_error == "验收模拟的原始私有临时文件清理失败"
                    ),
                }
            known.mkdir(mode=0o700)
            (known / "snapshot.plaintext").write_bytes(secrets.token_bytes(4096))
            (known / "snapshot.plaintext").chmod(0o600)
            marker.write_bytes(b"unrelated synthetic file")
            marker.chmod(0o600)
            unknown.mkdir(mode=0o700)
            (unknown / "synthetic.txt").write_bytes(b"unknown run identity")
            (unknown / "synthetic.txt").chmod(0o600)
            row.cleanup_status = "failed"
            row.cleanup_error = "验收模拟的原始私有临时文件清理失败"
            await session.commit()
            return {"terminal_failure_fixture_written": True}
    finally:
        await engine.dispose()


async def secret_storage(data: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import select

    from app.core.database import create_engine, create_sessionmaker
    from app.core.security import SecretCipher
    from app.models.backup import BackupArtifact, BackupRun, BackupSettings
    from app.schemas.backup import SECRET_KEYS

    engine = create_engine(data.get("database_url", os.environ["DATABASE_URL"]))
    cipher = SecretCipher(os.environ["ENCRYPTION_KEY"])
    expected = data["secrets"]
    try:
        async with create_sessionmaker(engine)() as session:
            settings = await session.get(BackupSettings, 1)
            valid_settings = settings is not None and not (set(settings.configuration) & SECRET_KEYS)
            if settings is not None:
                valid_settings = valid_settings and all(
                    key in settings.secrets_enc and settings.secrets_enc[key] != plaintext
                    and cipher.decrypt(settings.secrets_enc[key]) == plaintext
                    for key, plaintext in expected.items()
                )
            terminal = list(await session.scalars(select(BackupRun).where(BackupRun.active_slot.is_(None))))
            artifacts = list(await session.scalars(select(BackupArtifact)))
            return {
                "settings_ciphertext_only": valid_settings,
                "terminal_run_secrets_removed": all(not row.config_snapshot.get("secrets_enc") for row in terminal),
                "artifact_access_ciphertext_only": all(
                    encrypted not in expected.values() and cipher.decrypt(encrypted) in expected.values()
                    for row in artifacts for encrypted in row.access_snapshot_enc.values()
                ),
            }
    finally:
        await engine.dispose()


def main() -> None:
    try:
        data = json.load(sys.stdin)
        action = sys.argv[1]
        if action == "fingerprint":
            result = asyncio.run(database_fingerprint(data))
        elif action == "decrypt":
            result = gpg_decrypt(data)
        elif action == "s3":
            result = s3(data)
        elif action == "wal_marker":
            result = wal_marker(data)
        elif action == "schedule_due":
            result = asyncio.run(schedule_due())
        elif action == "tools":
            result = {}
            for name in ("pg_dump", "pg_restore", "gpg", "gpgconf"):
                output = subprocess.run([name, "--version"], capture_output=True, timeout=10, check=True).stdout
                result[name] = output.decode(errors="replace").splitlines()[0][:200]
        elif action == "private_homes":
            result = {"remaining": len(list(Path("/tmp").glob(".ainovel-gpg-*")))}
        elif action == "terminal_work_fixture":
            result = asyncio.run(terminal_work_fixture(data, verify=False))
        elif action == "terminal_work_verify":
            result = asyncio.run(terminal_work_fixture(data, verify=True))
        elif action == "secret_storage":
            result = asyncio.run(secret_storage(data))
        else:
            raise ValueError("unknown action")
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        # 永不把 SQL URL、响应、密码或第三方 traceback 打到 Docker logs / 验收证据。
        frames = traceback.extract_tb(error.__traceback__)
        own = [entry for entry in frames if Path(entry.filename).name == "probe.py"]
        print(json.dumps({
            "probe_failed": True, "error_type": type(error).__name__,
            "probe_line": own[-1].lineno if own else None,
        }))


if __name__ == "__main__":
    main()
