"""实例单执行备份运行器：持久队列、北京时间计划、独立留存与显式关停。"""

import asyncio
import logging
import shutil
import threading
import time
from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from sqlalchemy import select

from app.models import BackupArtifact, BackupRun
from app.schemas.backup import BackupConfiguration
from app.services.backups import crypto, database, mail
from app.services.backups.network import NetworkPolicy
from app.services.backups.process import BackupAdapterError, run_blocking
from app.services.backups.service import BackupGoneError, BackupManager, BackupUnavailableError, notification_ready
from app.services.backups.storage import (
    CHUNK_SIZE,
    LocalStore,
    S3Credentials,
    S3Location,
    S3Store,
    artifact_filename,
    file_digest,
)

logger = logging.getLogger(__name__)
_Row = TypeVar("_Row", BackupRun, BackupArtifact)


def _equal(actual, expected) -> bool:
    if isinstance(actual, datetime) and isinstance(expected, datetime):
        left = actual.replace(tzinfo=UTC) if actual.tzinfo is None else actual.astimezone(UTC)
        right = expected.replace(tzinfo=UTC) if expected.tzinfo is None else expected.astimezone(UTC)
        return left == right
    return actual == expected


class DownloadResource:
    """已打开的私有附件；HTTP层负责打开前后鉴权，结束/断开时close。"""

    def __init__(self, filename: str, size_bytes: int, source):
        self.filename, self.size_bytes, self._source = filename, size_bytes, source
        self._closed = False

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        try:
            while not self._closed:
                block = await run_blocking(lambda _stop: self._source.read(CHUNK_SIZE))
                if not block:
                    break
                yield block
        finally:
            await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await run_blocking(lambda _stop: self._source.close())


class BackupRuntime:
    def __init__(self, manager: BackupManager):
        self.manager = manager
        self.settings = manager.settings
        self._wake = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._stop_event = threading.Event()
        self._closing = False
        self._uncertain = False
        self._policy = NetworkPolicy(self.settings.allow_private_backup_endpoints, self.settings.backup_ca_bundle)

    def notify(self) -> None:
        self._wake.set()

    def tool_readiness(self, configuration: BackupConfiguration) -> list[str]:
        errors = []
        try:
            engine = database.database_engine(self.settings.database_url)
            if engine == "postgresql" and shutil.which(self.settings.backup_pg_dump_path) is None:
                errors.append("PostgreSQL 备份工具不可用，请检查部署配置")
        except BackupAdapterError:
            errors.append("仅支持 PostgreSQL 和文件 SQLite 数据库")
        if configuration.encryption_enabled and any(shutil.which(path) is None for path in (
            self.settings.backup_gpg_path, self.settings.backup_gpgconf_path,
        )):
            errors.append("文件加密工具不可用，请检查部署配置")
        if self._uncertain:
            errors.append("上次备份持久状态待核实，暂不受理新执行")
        return errors

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        self._closing = False
        self._stop_event.clear()
        try:
            await self.manager.load()
            await self._recover()
        except Exception:
            self._uncertain = True
            logger.error("备份启动状态待核实，备份模块暂不受理新执行")
        self._runner = asyncio.create_task(self._loop(), name="database-backup-runtime")
        self.notify()

    async def stop(self) -> None:
        self._closing = True
        self.manager.closing = True
        self._stop_event.set()
        self.notify()
        if self._runner is not None:
            # 不cancel：底层线程/工具必须已停止才写终态并释放active_slot。
            runner = self._runner
            cancelled = False
            while not runner.done():
                try:
                    await asyncio.shield(runner)
                except asyncio.CancelledError:
                    cancelled = True
            self._runner = None
            if not runner.cancelled():
                runner.result()
            if cancelled:
                raise asyncio.CancelledError

    async def _loop(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                if self._uncertain:
                    await self._recover()
                await self.manager.accept_scheduled()
                async with self.manager.sessionmaker() as db:
                    queued = await db.scalar(select(BackupRun.id).where(
                        BackupRun.status == "queued", BackupRun.active_slot == 1,
                    ).order_by(BackupRun.id).limit(1))
                if queued is not None:
                    await self._execute(queued)
                    continue
            except asyncio.CancelledError:
                self._stop_event.set()
                raise
            except Exception:
                # SQL参数/连接配置可能含秘密；该边界只记录固定运维摘要。
                logger.error("备份调度暂时不可用，将重新核实持久状态")
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except TimeoutError:
                pass

    async def _read(self, model: type[_Row], row_id: int) -> _Row | None:
        async with self.manager.sessionmaker() as db:
            return await db.get(model, row_id)

    async def _write(self, model: type[_Row], row_id: int, **values) -> None:
        async with self.manager.lock:
            try:
                async with self.manager.sessionmaker() as db:
                    row = await db.get(model, row_id)
                    if row is None:
                        raise ValueError
                    for key, value in values.items():
                        setattr(row, key, value)
                    await db.commit()
                return
            except Exception:
                # 提交确认丢失时先重读事实；不可重放外部副作用或宣称失败回滚。
                try:
                    async with self.manager.sessionmaker() as db:
                        row = await db.get(model, row_id)
                        if row is not None and all(_equal(getattr(row, key), value) for key, value in values.items()):
                            return
                except Exception:
                    pass
                self._uncertain = True
                raise BackupUnavailableError("备份执行持久状态暂时无法确认") from None

    def _local(self, instance_key: str) -> LocalStore:
        return LocalStore(Path(self.settings.backup_dir), instance_key)

    def _decrypt(self, secrets_enc: dict, key: str) -> str | None:
        value = secrets_enc.get(key)
        if not value:
            return None
        try:
            return self.manager.cipher.decrypt(value)
        except Exception:
            raise BackupAdapterError("storage_failed") from None

    def _credentials(self, encrypted: dict) -> S3Credentials:
        access, secret = self._decrypt(encrypted, "s3_access_key"), self._decrypt(encrypted, "s3_secret_key")
        if not access or not secret:
            raise BackupAdapterError("storage_failed")
        return S3Credentials(access, secret)

    async def _s3_candidates(self, artifact: BackupArtifact) -> list[S3Store]:
        location = S3Location.from_locator(artifact.locator)
        snapshots = []
        # 不用新bucket/key重定位旧对象；同一位置优先最新凭据，旧加密快照作为回退。
        try:
            current = await self.manager.snapshot()
            config = current.configuration
            if (config.s3_endpoint == location.endpoint and config.s3_bucket == location.bucket
                    and config.s3_region == location.region):
                snapshots.append(current.secrets_enc)
        except Exception:
            pass
        snapshots.append(artifact.access_snapshot_enc)
        stores, seen = [], set()
        for snapshot in snapshots:
            try:
                credentials = self._credentials(snapshot)
                identity = (credentials.access_key, credentials.secret_key)
                if identity not in seen:
                    stores.append(S3Store(location, credentials, self._policy))
                    seen.add(identity)
            except BackupAdapterError:
                pass
        if not stores:
            raise BackupAdapterError("storage_failed")
        return stores

    async def _on_s3(self, artifact: BackupArtifact, action):
        last = BackupAdapterError("storage_failed")
        for store in await self._s3_candidates(artifact):
            try:
                return await run_blocking(lambda stop, store=store: action(store, stop))
            except BackupAdapterError as exc:
                if exc.code in ("storage_missing", "storage_identity", "network_blocked", "interrupted"):
                    raise
                last = exc
        raise last

    async def _execute(self, run_id: int) -> None:
        row = await self._read(BackupRun, run_id)
        if row is None or row.status != "queued" or row.active_slot != 1:
            return
        local = self._local(row.instance_key)
        temporary = None
        result, error = "failed", None
        cleanup_failed = False
        deadline = time.monotonic() + self.settings.backup_timeout_seconds
        try:
            await self._write(BackupRun, row.id, status="running", started_at=datetime.now(UTC))
            configuration = BackupConfiguration.model_validate(row.config_snapshot["configuration"])
            secrets_enc = row.config_snapshot["secrets_enc"]
            temporary = await run_blocking(lambda _stop: local.temporary_directory(row.execution_key))
            native_extension = ".dump" if row.database_engine == "postgresql" else ".sqlite3"
            native = temporary / ("snapshot" + native_extension)
            snapshot = await run_blocking(lambda stop: database.snapshot_database(
                self.settings.database_url, native, pg_dump_path=self.settings.backup_pg_dump_path,
                deadline=deadline, stop_event=stop,
            ), self._stop_event)
            if snapshot.schema_revision is not None:
                await self._write(BackupRun, row.id, schema_revision=snapshot.schema_revision)
            output = native
            extension = native_extension
            if row.encrypted:
                password = self._decrypt(secrets_enc, "backup_password")
                if not password:
                    raise BackupAdapterError("encryption_failed")
                output = native.with_name(native.name + ".gpg")
                await run_blocking(lambda stop: crypto.encrypt_file(
                    native, output, password, gpg_path=self.settings.backup_gpg_path,
                    gpgconf_path=self.settings.backup_gpgconf_path, deadline=deadline, stop_event=stop,
                    instance_key=row.instance_key, execution_key=row.execution_key,
                ), self._stop_event)
                password = None
                extension += ".gpg"
            size, checksum = await run_blocking(lambda stop: file_digest(
                output, deadline=deadline, stop_event=stop,
            ), self._stop_event)
            filename = artifact_filename(row.execution_key, extension)
            async with self.manager.sessionmaker() as db:
                artifacts = list(await db.scalars(select(BackupArtifact).where(
                    BackupArtifact.run_id == row.id,
                ).order_by(BackupArtifact.id)))
            for artifact in artifacts:
                try:
                    await self._store_artifact(row, artifact, configuration, secrets_enc,
                                               output, filename, size, checksum, deadline)
                except BackupAdapterError as exc:
                    # 产物身份已在写入前持久化。超时/确认丢失不能证明外部副作用未发生，
                    # 保留unknown供精确核验；不能标failed后永久排除恢复及留存清理。
                    await self._write(BackupArtifact, artifact.id, status="unknown",
                                      error_code=exc.code, error_message=exc.detail)
                    if exc.code == "interrupted":
                        raise
            async with self.manager.sessionmaker() as db:
                completed = list(await db.scalars(select(BackupArtifact).where(BackupArtifact.run_id == row.id)))
            ready = [item for item in completed if item.status == "ready"]
            result = "succeeded" if len(ready) == len(completed) else "partial" if ready else "failed"
            if result != "succeeded":
                error = BackupAdapterError("storage_failed")
            if ready:
                await self._write(BackupRun, row.id, cleanup_status="pending")
                for artifact in ready:
                    if not await self._retain(row.instance_key, artifact.destination,
                                              getattr(configuration, artifact.destination + "_keep")):
                        cleanup_failed = True
                await self._write(
                    BackupRun, row.id, cleanup_status="failed" if cleanup_failed else "succeeded",
                    cleanup_error=BackupAdapterError("cleanup_failed").detail if cleanup_failed else None,
                )
        except asyncio.CancelledError:
            self._stop_event.set()
            result, error = "interrupted", BackupAdapterError("interrupted")
            raise
        except BackupAdapterError as exc:
            result, error = ("interrupted" if exc.code == "interrupted" else "failed"), exc
        except BackupUnavailableError:
            self._uncertain = True
            result, error = "interrupted", BackupAdapterError("interrupted")
        except Exception:
            result, error = "failed", BackupAdapterError("snapshot_failed")
        finally:
            # 即使生成失败也清理原始明文；此时每个run_blocking都已真正结束。
            if row.encrypted:
                try:
                    await self._clean_encryption_home(row)
                except Exception:
                    cleanup_failed = True
            if temporary is not None:
                try:
                    await run_blocking(lambda _stop: local.clean_temporary(row.execution_key))
                except Exception:
                    cleanup_failed = True
            if not self._uncertain:
                try:
                    await self._finish(row, result, error, cleanup_failed)
                except Exception:
                    self._uncertain = True
                    logger.error("备份结束状态待核实，暂停新执行")

    async def _clean_encryption_home(self, row: BackupRun) -> None:
        # helper按两项不可复用身份核对marker、uid和目录，再回收独占GPG agent。
        # 清理用独立短截止和stop信号，即使执行已中断也必须尝试回收自己的工具。
        await run_blocking(lambda stop: crypto.cleanup_encryption_home(
            row.instance_key, row.execution_key, gpgconf_path=self.settings.backup_gpgconf_path,
            deadline=time.monotonic() + 5, stop_event=stop,
        ))

    async def _store_artifact(
        self, run: BackupRun, artifact: BackupArtifact, configuration: BackupConfiguration, secrets_enc: dict,
        source: Path, filename: str, size: int, checksum: str, deadline: float,
    ) -> None:
        if artifact.destination == "local":
            local = self._local(run.instance_key)
            locator = local.intended_locator(run.execution_key, filename)
            await self._write(BackupArtifact, artifact.id, filename=filename, size_bytes=size,
                              checksum_sha256=checksum, locator=locator)
            locator = await run_blocking(lambda stop: local.save(
                source, run.execution_key, filename, deadline=deadline, stop_event=stop,
            ), self._stop_event)
            await self._write(BackupArtifact, artifact.id, status="ready", locator=locator)
            return
        prefix = configuration.s3_prefix
        location = S3Location(
            configuration.s3_endpoint, configuration.s3_region, configuration.s3_bucket,
            "/".join(part for part in (prefix, run.instance_key, run.execution_key, filename) if part),
            run.instance_key, run.execution_key,
        )
        encrypted_access = {key: secrets_enc[key] for key in ("s3_access_key", "s3_secret_key") if key in secrets_enc}
        await self._write(BackupArtifact, artifact.id, filename=filename, size_bytes=size, checksum_sha256=checksum,
                          locator=location.locator(), access_snapshot_enc=encrypted_access)
        store = S3Store(location, self._credentials(secrets_enc), self._policy)
        loop = asyncio.get_running_loop()

        def upload_started(upload_id):
            future = asyncio.run_coroutine_threadsafe(
                self._write(BackupArtifact, artifact.id, multipart_upload_id=upload_id), loop,
            )
            try:
                future.result(max(0.1, deadline - time.monotonic()))
            except Exception:
                raise BackupAdapterError("storage_failed") from None

        version = await run_blocking(lambda stop: store.save(
            source, size, checksum, deadline=deadline, stop_event=stop, upload_started=upload_started,
        ), self._stop_event)
        await self._write(BackupArtifact, artifact.id, status="ready", version_id=version, multipart_upload_id=None)

    async def _retain(self, instance_key: str, destination: str, keep: int) -> bool:
        # 先核实上次删除未知的精确位置，才能重新计数/重试；不忽略可能仍存在的旧副本。
        async with self.manager.sessionmaker() as db:
            uncertain = list(await db.scalars(select(BackupArtifact).join(BackupRun).where(
                BackupRun.instance_key == instance_key, BackupArtifact.destination == destination,
                BackupArtifact.status.in_(("unknown", "deleting")),
            )))
        success = True
        for artifact in uncertain:
            deleting = artifact.status == "deleting" or artifact.error_code == "cleanup_failed"
            await self._recover_artifact(artifact, was_deleting=deleting)
            checked = await self._read(BackupArtifact, artifact.id)
            if checked is not None and checked.status == "unknown":
                success = False
        async with self.manager.sessionmaker() as db:
            candidates = list(await db.scalars(select(BackupArtifact).join(BackupRun).where(
                BackupRun.instance_key == instance_key, BackupArtifact.destination == destination,
                BackupArtifact.status == "ready",
            ).order_by(BackupRun.created_at.desc(), BackupRun.id.desc(), BackupArtifact.id.desc()).offset(keep)))
        for artifact in candidates:
            await self._write(BackupArtifact, artifact.id, status="deleting")
            try:
                if destination == "local":
                    await run_blocking(
                        lambda _stop, artifact=artifact: self._local(instance_key).delete(artifact.locator),
                    )
                else:
                    await self._on_s3(artifact, lambda store, stop, artifact=artifact: store.delete(
                        artifact.version_id, deadline=time.monotonic() + 30, stop_event=stop,
                    ))
                await self._write(BackupArtifact, artifact.id, status="deleted", deleted_at=datetime.now(UTC),
                                  access_snapshot_enc={}, multipart_upload_id=None, error_code=None, error_message=None)
            except BackupAdapterError:
                # 副本仍保留；未知的删除不能记成功，未来新备份清理可精确重核位置。
                await self._write(
                    BackupArtifact, artifact.id, status="unknown", error_code="cleanup_failed",
                    error_message=BackupAdapterError("cleanup_failed").detail,
                )
                success = False
        return success

    async def _finish(
        self, row: BackupRun, result: str, error: BackupAdapterError | None, cleanup_failed: bool,
    ) -> None:
        async with self.manager.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).where(BackupArtifact.run_id == row.id)))
        for artifact in artifacts:
            if artifact.status == "pending":
                status = "unknown" if result == "interrupted" else "failed"
                await self._write(BackupArtifact, artifact.id, status=status,
                                  error_code=error.code if error else "storage_failed",
                                  error_message=error.detail if error else BackupAdapterError("storage_failed").detail)
        if cleanup_failed:
            await self._write(BackupRun, row.id, cleanup_status="failed",
                              cleanup_error=BackupAdapterError("cleanup_failed").detail)
        configuration = BackupConfiguration.model_validate(row.config_snapshot["configuration"])
        secrets_enc = row.config_snapshot.get("secrets_enc", {})
        if result != "succeeded" or cleanup_failed:
            await self._notify_failure(row, configuration, secrets_enc, result, artifacts, cleanup_failed)
        # SMTP/密码不再需要；S3访问凭据仅保存在仍可用产物的私有加密快照。
        snapshot = deepcopy(row.config_snapshot)
        snapshot["secrets_enc"] = {}
        await self._write(
            BackupRun, row.id, status=result, active_slot=None, finished_at=datetime.now(UTC),
            error_code=error.code if error else None, error_message=error.detail if error else None,
            config_snapshot=snapshot,
        )

    async def _notify_failure(
        self, row: BackupRun, configuration: BackupConfiguration, secrets_enc: dict, result: str,
        artifacts: list[BackupArtifact], cleanup_failed: bool,
    ) -> None:
        if not notification_ready(configuration, secrets_enc):
            return
        if self._closing:
            await self._write(BackupRun, row.id, notification_status="unknown",
                              notification_error="服务关停，通知受理结果待核实")
            return
        await self._write(BackupRun, row.id, notification_status="pending")
        try:
            config = mail.MailConfig(
                configuration.smtp_host, configuration.smtp_port, configuration.smtp_security,
                str(configuration.smtp_from_address), tuple(str(item) for item in configuration.smtp_recipients),
                self._decrypt(secrets_enc, "smtp_username"), self._decrypt(secrets_enc, "smtp_password"),
            )
            notice = mail.FailureNotice(
                row.execution_key, row.id, result, datetime.now(UTC),
                tuple(item.destination for item in artifacts if item.status != "ready"), cleanup_failed,
            )
            # 发出SMTP副作用前持久标记；崩溃后不会盲重发。
            await self._write(BackupRun, row.id, notification_status="sending")
            outcome = await run_blocking(lambda stop: mail.send_failure_notification(
                config, notice, self._policy, deadline=time.monotonic() + 60, stop_event=stop,
            ), self._stop_event)
            await self._write(BackupRun, row.id, notification_status=outcome.status,
                              notification_error=outcome.error_message)
        except BackupAdapterError:
            await self._write(BackupRun, row.id, notification_status="failed",
                              notification_error=BackupAdapterError("mail_failed").detail)

    async def prepare_download(self, run_id: int, destination: str) -> DownloadResource:
        async with self.manager.sessionmaker() as db:
            row = await db.get(BackupRun, run_id)
            artifact = await db.scalar(select(BackupArtifact).where(
                BackupArtifact.run_id == run_id, BackupArtifact.destination == destination,
            ))
        if row is None or artifact is None or artifact.status != "ready" or not artifact.filename:
            raise BackupGoneError("备份副本已过期、缺失或不可用")
        try:
            if destination == "local":
                source = await run_blocking(lambda _stop: self._local(row.instance_key).open(
                    artifact.locator, artifact.size_bytes,
                ))
            else:
                source = await self._on_s3(artifact, lambda store, stop: store.open(
                    artifact.size_bytes, artifact.checksum_sha256, artifact.version_id,
                    deadline=time.monotonic() + self.settings.backup_timeout_seconds, stop_event=stop,
                ))
            return DownloadResource(artifact.filename, artifact.size_bytes, source)
        except BackupAdapterError as exc:
            if exc.code == "storage_missing":
                await self._write(BackupArtifact, artifact.id, status="missing", error_code=exc.code,
                                  error_message=exc.detail)
                raise BackupGoneError("备份副本已不存在") from None
            raise BackupUnavailableError("备份副本暂时无法访问或身份校验失败") from None

    async def _recover(self) -> None:
        """底层已排空后才能调用；启动不重放已受理的外部写或SMTP发送。"""
        async with self.manager.sessionmaker() as db:
            rows = list(await db.scalars(select(BackupRun).where(BackupRun.active_slot == 1)))
            uncertain = list(await db.scalars(select(BackupArtifact).join(BackupRun).where(
                BackupRun.active_slot.is_(None), BackupArtifact.status.in_(("deleting", "unknown")),
            )))
            cleanup_recovery = list(await db.scalars(select(BackupRun).where(
                BackupRun.active_slot.is_(None),
                BackupRun.cleanup_status.in_(("failed", "unknown", "pending")),
            )))
        for row in cleanup_recovery:
            cleanup_failed = False
            if row.encrypted:
                try:
                    await self._clean_encryption_home(row)
                except BackupAdapterError:
                    cleanup_failed = True
            try:
                # 正常执行可能已写终态但finally清理失败；明文也须在重启时精确重试。
                # 不扫描其他工作目录，也不把临时文件清除冒充原保留清理已成功。
                await run_blocking(
                    lambda _stop, row=row: self._local(row.instance_key).clean_temporary(row.execution_key),
                )
            except BackupAdapterError:
                cleanup_failed = True
            if cleanup_failed:
                await self._write(BackupRun, row.id, cleanup_status="failed",
                                  cleanup_error=BackupAdapterError("cleanup_failed").detail)
        for artifact in uncertain:
            await self._recover_artifact(
                artifact, was_deleting=artifact.status == "deleting" or artifact.error_code == "cleanup_failed",
            )
        for row in rows:
            async with self.manager.sessionmaker() as db:
                artifacts = list(await db.scalars(select(BackupArtifact).where(BackupArtifact.run_id == row.id)))
            for artifact in artifacts:
                await self._recover_artifact(
                    artifact, was_deleting=artifact.status == "deleting" or artifact.error_code == "cleanup_failed",
                )
            cleanup_failed = False
            if row.encrypted:
                try:
                    await self._clean_encryption_home(row)
                except BackupAdapterError:
                    cleanup_failed = True
            try:
                await run_blocking(
                    lambda _stop, row=row: self._local(row.instance_key).clean_temporary(row.execution_key),
                )
            except BackupAdapterError:
                cleanup_failed = True
            snapshot = deepcopy(row.config_snapshot)
            snapshot["secrets_enc"] = {}
            values = dict(
                status="interrupted", active_slot=None, finished_at=datetime.now(UTC),
                error_code="interrupted", error_message=BackupAdapterError("interrupted").detail,
                config_snapshot=snapshot,
            )
            if row.notification_status in ("pending", "sending"):
                values.update(notification_status="unknown",
                              notification_error=BackupAdapterError("mail_unknown").detail)
            if row.cleanup_status == "pending":
                values.update(cleanup_status="unknown", cleanup_error="上次清理结果待核实")
            if cleanup_failed:
                values.update(cleanup_status="failed", cleanup_error=BackupAdapterError("cleanup_failed").detail)
            await self._write(BackupRun, row.id, **values)
        self._uncertain = False

    async def _recover_artifact(self, artifact: BackupArtifact, *, was_deleting: bool = False) -> None:
        if artifact.status in ("deleted", "failed", "missing"):
            return
        status, version, locator = "unknown", artifact.version_id, artifact.locator
        try:
            if not artifact.locator or artifact.size_bytes is None or not artifact.checksum_sha256:
                raise BackupAdapterError("storage_identity")
            if artifact.destination == "local":
                local = self._local(artifact.locator["instance_key"])
                verified = await run_blocking(lambda _stop: local.verified_locator(
                    artifact.locator, artifact.size_bytes, artifact.checksum_sha256,
                ))
                if not verified:
                    raise BackupAdapterError("storage_missing")
                locator = verified
            else:
                version = await self._on_s3(artifact, lambda store, stop: store.head(
                    artifact.size_bytes, artifact.checksum_sha256, artifact.version_id,
                    deadline=time.monotonic() + 30, stop_event=stop,
                ))
            status = "ready"
        except BackupAdapterError as exc:
            if exc.code == "storage_missing":
                status = "deleted" if was_deleting else "missing" if artifact.status == "ready" else "unknown"
        except (KeyError, TypeError, ValueError):
            status = "unknown"
        upload_aborted = False
        if artifact.multipart_upload_id and artifact.destination == "s3":
            try:
                await self._on_s3(artifact, lambda store, stop: store.abort(
                    artifact.multipart_upload_id, deadline=time.monotonic() + 30, stop_event=stop,
                ))
                upload_aborted = True
            except BackupAdapterError:
                pass
        values = dict(status=status, version_id=version, locator=locator)
        if upload_aborted:
            values["multipart_upload_id"] = None
        if status == "deleted":
            values.update(deleted_at=datetime.now(UTC), access_snapshot_enc={}, multipart_upload_id=None)
        elif status == "ready":
            values.update(multipart_upload_id=None, error_code=None, error_message=None)
        await self._write(BackupArtifact, artifact.id, **values)
