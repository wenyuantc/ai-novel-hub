"""实例单执行备份运行器：持久队列、北京时间计划、独立留存与显式关停。"""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import BackupArtifact, BackupRun
from app.schemas.backup import BackupConfiguration
from app.services.backups import crypto, database, mail
from app.services.backups.network import NetworkPolicy
from app.services.backups.process import BackupAdapterError, backup_lock_fd, run_blocking
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
from app.services.task_engine import workers

if TYPE_CHECKING:
    from app.services.task_engine.request_worker import RequestWorker

logger = logging.getLogger(__name__)
_Row = TypeVar("_Row", BackupRun, BackupArtifact)
_RECOVERY_SWEEP_SECONDS = 60.0


@dataclass(frozen=True)
class _Claim:
    run_id: int
    execution_key: str
    worker_key: str | None
    recovery: bool = False


class _ExecutionLost(BackupUnavailableError):
    def __init__(self) -> None:
        super().__init__("备份执行归属已变化")


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
    def __init__(self, manager: BackupManager, *, worker: RequestWorker | None = None):
        self.manager = manager
        self.settings = manager.settings
        self.worker = worker
        self.hostname = socket.gethostname()[:255]
        self.manager.admission_worker = self._admission_worker
        self._wake = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._stop_event = threading.Event()
        self._closing = False
        self._uncertain = False
        self._drained: _Claim | None = None
        self._policy = NetworkPolicy(self.settings.allow_private_backup_endpoints, self.settings.backup_ca_bundle)

    async def _admission_worker(self, db: AsyncSession) -> str | None:
        if self.worker is None or self._runner is None or self._runner.done():
            return None
        try:
            return await self.worker.require(db)
        except ConflictError:
            raise BackupUnavailableError("备份执行身份暂时无法确认") from None

    def _worker_matches(self, key: str | None) -> bool:
        return self.worker is None or (self.worker.valid and self.worker.worker_key == key)

    async def _require_worker(self, db: AsyncSession, key: str | None) -> None:
        if self.worker is not None:
            if key is None:
                raise _ExecutionLost
            try:
                await self.worker.require(db, key)
            except ConflictError:
                raise _ExecutionLost from None

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
        next_sweep = time.monotonic() + _RECOVERY_SWEEP_SECONDS
        while not self._closing:
            self._wake.clear()
            try:
                sweep = self._uncertain or time.monotonic() >= next_sweep
                await self._recover(include_legacy_queued=False, include_terminal=sweep)
                if sweep:
                    next_sweep = time.monotonic() + _RECOVERY_SWEEP_SECONDS
                await self.manager.accept_scheduled()
                async with self.manager.sessionmaker() as db:
                    queued = await db.scalar(select(BackupRun.id).where(
                        BackupRun.status == "queued", BackupRun.active_slot == 1,
                    ).order_by(BackupRun.id).limit(1))
                if queued is not None and await self._execute(queued):
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

    async def _check_claim(self, db: AsyncSession, claim: _Claim) -> None:
        await self._require_worker(db, claim.worker_key)
        if claim.recovery:
            owned = (BackupRun.recovery_worker_key == claim.worker_key,)
        else:
            owned = (BackupRun.worker_key == claim.worker_key, BackupRun.recovery_worker_key.is_(None),
                     BackupRun.status == "running", BackupRun.active_slot == 1)
        run_id = await db.scalar(select(BackupRun.id).where(
            BackupRun.id == claim.run_id, BackupRun.execution_key == claim.execution_key, *owned,
        ).with_for_update())
        if run_id is None:
            raise _ExecutionLost

    async def _write(
        self, model: type[_Row], row_id: int, *, claim: _Claim | None = None,
        expected_status: str | None = None, **values,
    ) -> bool:
        async with self.manager.lock:
            try:
                async with self.manager.sessionmaker() as db:
                    if claim is not None:
                        await self._check_claim(db, claim)
                    statement = update(model).where(model.id == row_id)
                    if expected_status is not None:
                        statement = statement.where(model.status == expected_status)
                    changed = await db.execute(statement.values(**values).execution_options(synchronize_session=False))
                    if changed.rowcount != 1:
                        if expected_status is not None:
                            return False
                        raise _ExecutionLost
                    await db.commit()
                return True
            except _ExecutionLost:
                raise
            except Exception:
                # 提交确认丢失时先重读事实；不可重放外部副作用或宣称失败回滚。
                try:
                    async with self.manager.sessionmaker() as db:
                        row = await db.get(model, row_id)
                        if row is not None and all(_equal(getattr(row, key), value) for key, value in values.items()):
                            if claim is not None and not values.keys() & {"active_slot", "recovery_worker_key"}:
                                await self._check_claim(db, claim)
                            return True
                except Exception:
                    pass
                self._uncertain = True
                raise BackupUnavailableError("备份执行持久状态暂时无法确认") from None

    def _local(self, instance_key: str) -> LocalStore:
        return LocalStore(Path(self.settings.backup_dir), instance_key)

    @asynccontextmanager
    async def _filesystem_scope(self, local: LocalStore) -> AsyncIterator[tuple[bool, BackupAdapterError | None]]:
        try:
            handle = await run_blocking(lambda _stop: local.try_runtime_lock())
        except BackupAdapterError as exc:
            yield False, exc
            return
        if handle is None:
            yield False, None
            return
        with handle:
            token = backup_lock_fd.set(handle.fileno())
            try:
                yield True, None
            finally:
                backup_lock_fd.reset(token)

    @asynccontextmanager
    async def _watch_worker(self, claim: _Claim) -> AsyncIterator[None]:
        finished = asyncio.Event()

        async def monitor() -> None:
            while self._worker_matches(claim.worker_key):
                try:
                    await asyncio.wait_for(finished.wait(), 0.2)
                    return
                except TimeoutError:
                    pass
            self._stop_event.set()

        watcher = asyncio.create_task(monitor()) if self.worker is not None else None
        try:
            yield
        finally:
            if watcher is not None:
                finished.set()
                with suppress(asyncio.CancelledError):
                    await watcher

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

    async def _on_s3(self, artifact: BackupArtifact, action, *, stop_event: threading.Event | None = None):
        last = BackupAdapterError("storage_failed")
        for store in await self._s3_candidates(artifact):
            try:
                return await run_blocking(lambda stop, store=store: action(store, stop), stop_event)
            except BackupAdapterError as exc:
                if exc.code in ("storage_missing", "storage_identity", "network_blocked", "interrupted"):
                    raise
                last = exc
        raise last

    async def _reserve(self, row: BackupRun, claim: _Claim, *conditions, **values) -> BackupRun | None:
        try:
            async with self.manager.sessionmaker() as db:
                await self._require_worker(db, claim.worker_key)
                statement = update(BackupRun).where(
                    BackupRun.id == row.id, BackupRun.execution_key == row.execution_key, *conditions,
                ).values(**values).returning(BackupRun)
                claimed = (await db.scalars(statement)).one_or_none()
                if claimed is not None:
                    await db.commit()
                return claimed
        except _ExecutionLost:
            return None
        except Exception:
            try:
                async with self.manager.sessionmaker() as db:
                    await self._check_claim(db, claim)
                    claimed = await db.get(BackupRun, row.id)
                    if claimed is not None and all(
                        _equal(getattr(claimed, key), value) for key, value in values.items()
                    ):
                        return claimed
            except _ExecutionLost:
                return None
            except Exception:
                pass
            self._uncertain = True
            if not claim.recovery:
                # 领取可能已提交。还没开始快照，后续可按同一身份中断并释放活动槽。
                self._drained = claim
            raise BackupUnavailableError("备份执行持久状态暂时无法确认") from None

    async def _execute(self, run_id: int) -> bool:
        row = await self._read(BackupRun, run_id)
        if row is None or row.status != "queued" or row.active_slot != 1 or self._closing:
            return False
        local = self._local(row.instance_key)
        key = self.worker.worker_key if self.worker is not None else None
        claim = _Claim(row.id, row.execution_key, key)
        async with self._filesystem_scope(local) as (available, storage_error):
            if self._closing or (not available and storage_error is None):
                return False
            row = await self._reserve(
                row, claim,
                BackupRun.status == "queued", BackupRun.active_slot == 1, BackupRun.worker_key == row.worker_key,
                BackupRun.recovery_worker_key.is_(None),
                or_(BackupRun.worker_key.is_(None), workers.registered_worker_exists(BackupRun.worker_key)),
                status="running", started_at=datetime.now(UTC), worker_key=key, execution_host=self.hostname,
            )
            if row is None:
                return False
            if not self._closing:
                self._stop_event.clear()
            async with self._watch_worker(claim):
                await self._execute_claimed(row, local, claim, storage_error)
            return True

    async def _execute_claimed(
        self, row: BackupRun, local: LocalStore, claim: _Claim, storage_error: BackupAdapterError | None,
    ) -> None:
        temporary = None
        result, error = "failed", None
        cleanup_failed = False
        deadline = time.monotonic() + self.settings.backup_timeout_seconds
        try:
            if self._stop_event.is_set() or not self._worker_matches(claim.worker_key):
                raise BackupAdapterError("interrupted")
            if storage_error is not None:
                raise storage_error
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
                await self._write(BackupRun, row.id, claim=claim, schema_revision=snapshot.schema_revision)
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
                    await self._store_artifact(row, artifact, claim, configuration, secrets_enc,
                                               output, filename, size, checksum, deadline)
                except BackupAdapterError as exc:
                    # 产物身份已在写入前持久化。超时/确认丢失不能证明外部副作用未发生，
                    # 保留unknown供精确核验；不能标failed后永久排除恢复及留存清理。
                    await self._write(BackupArtifact, artifact.id, claim=claim, status="unknown",
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
                await self._write(BackupRun, row.id, claim=claim, cleanup_status="pending")
                for artifact in ready:
                    if not await self._retain(row.instance_key, artifact.destination, claim,
                                              getattr(configuration, artifact.destination + "_keep")):
                        cleanup_failed = True
                await self._write(
                    BackupRun, row.id, claim=claim, cleanup_status="failed" if cleanup_failed else "succeeded",
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
            if row.encrypted and storage_error is None:
                try:
                    await self._clean_encryption_home(row)
                except Exception:
                    cleanup_failed = True
            if temporary is not None:
                try:
                    await run_blocking(lambda _stop: local.clean_temporary(row.execution_key))
                except Exception:
                    cleanup_failed = True
            if not self._uncertain and self._worker_matches(claim.worker_key):
                try:
                    await self._finish(row, claim, result, error, cleanup_failed)
                except _ExecutionLost:
                    self._uncertain = True
                except Exception:
                    self._uncertain = True
                    logger.error("备份结束状态待核实，暂停新执行")
            if self._uncertain or not self._worker_matches(claim.worker_key):
                # 原执行者可核对自己已经排空但丢失提交回执的一轮。对端仍须等待永久围栏。
                self._drained = claim

    async def _clean_encryption_home(self, row: BackupRun) -> None:
        # helper按两项不可复用身份核对marker、uid和目录，再回收独占GPG agent。
        # 清理用独立短截止和stop信号，即使执行已中断也必须尝试回收自己的工具。
        await run_blocking(lambda stop: crypto.cleanup_encryption_home(
            row.instance_key, row.execution_key, gpgconf_path=self.settings.backup_gpgconf_path,
            deadline=time.monotonic() + 5, stop_event=stop,
        ))

    async def _store_artifact(
        self, run: BackupRun, artifact: BackupArtifact, claim: _Claim,
        configuration: BackupConfiguration, secrets_enc: dict,
        source: Path, filename: str, size: int, checksum: str, deadline: float,
    ) -> None:
        if artifact.destination == "local":
            local = self._local(run.instance_key)
            locator = local.intended_locator(run.execution_key, filename)
            await self._write(BackupArtifact, artifact.id, claim=claim, filename=filename, size_bytes=size,
                              checksum_sha256=checksum, locator=locator, writer_host=self.hostname)
            locator = await run_blocking(lambda stop: local.save(
                source, run.execution_key, filename, deadline=deadline, stop_event=stop,
            ), self._stop_event)
            await self._write(BackupArtifact, artifact.id, claim=claim, status="ready", locator=locator)
            return
        prefix = configuration.s3_prefix
        location = S3Location(
            configuration.s3_endpoint, configuration.s3_region, configuration.s3_bucket,
            "/".join(part for part in (prefix, run.instance_key, run.execution_key, filename) if part),
            run.instance_key, run.execution_key,
        )
        encrypted_access = {key: secrets_enc[key] for key in ("s3_access_key", "s3_secret_key") if key in secrets_enc}
        await self._write(BackupArtifact, artifact.id, claim=claim, filename=filename, size_bytes=size,
                          checksum_sha256=checksum, locator=location.locator(), access_snapshot_enc=encrypted_access)
        store = S3Store(location, self._credentials(secrets_enc), self._policy)
        loop = asyncio.get_running_loop()

        def upload_started(upload_id):
            future = asyncio.run_coroutine_threadsafe(
                self._write(BackupArtifact, artifact.id, claim=claim, multipart_upload_id=upload_id), loop,
            )
            try:
                future.result(max(0.1, deadline - time.monotonic()))
            except Exception:
                raise BackupAdapterError("storage_failed") from None

        version = await run_blocking(lambda stop: store.save(
            source, size, checksum, deadline=deadline, stop_event=stop, upload_started=upload_started,
        ), self._stop_event)
        await self._write(BackupArtifact, artifact.id, claim=claim, status="ready",
                          version_id=version, multipart_upload_id=None)

    @asynccontextmanager
    async def _retention_claim(self, artifact: BackupArtifact, active: _Claim) -> AsyncIterator[_Claim | None]:
        row = await self._read(BackupRun, artifact.run_id)
        if row is None or row.active_slot is not None:
            yield None
            return
        claim = _Claim(row.id, row.execution_key, active.worker_key, recovery=True)
        if await self._reserve_recovery(row, claim) is None:
            yield None
            return
        try:
            yield claim
        finally:
            await self._write(BackupRun, row.id, claim=claim, recovery_worker_key=None)

    async def _retain(self, instance_key: str, destination: str, claim: _Claim, keep: int) -> bool:
        # 先核实上次删除未知的精确位置，才能重新计数/重试；不忽略可能仍存在的旧副本。
        async with self.manager.sessionmaker() as db:
            uncertain = list(await db.scalars(select(BackupArtifact).join(BackupRun).where(
                BackupRun.instance_key == instance_key, BackupArtifact.destination == destination,
                BackupArtifact.status.in_(("unknown", "deleting")),
            )))
        success = True
        for artifact in uncertain:
            async with self._retention_claim(artifact, claim) as owned:
                if owned is None:
                    success = False
                    continue
                current = await self._read(BackupArtifact, artifact.id)
                if current is not None and current.status in ("unknown", "deleting"):
                    await self._recover_artifact(current, owned)
                    checked = await self._read(BackupArtifact, artifact.id)
                    if checked is not None and checked.status in ("unknown", "deleting"):
                        success = False
        async with self.manager.sessionmaker() as db:
            candidates = list(await db.scalars(select(BackupArtifact).join(BackupRun).where(
                BackupRun.instance_key == instance_key, BackupArtifact.destination == destination,
                BackupArtifact.status == "ready",
            ).order_by(BackupRun.created_at.desc(), BackupRun.id.desc(), BackupArtifact.id.desc()).offset(keep)))
        for artifact in candidates:
            async with self._retention_claim(artifact, claim) as owned:
                if owned is None or not await self._retain_artifact(artifact, owned, instance_key):
                    success = False
        return success

    async def _retain_artifact(self, artifact: BackupArtifact, claim: _Claim, instance_key: str) -> bool:
        local = self._local(instance_key)
        locator = artifact.locator
        remote = artifact.destination == "local" and artifact.writer_host != self.hostname
        if remote:
            try:
                locator = await run_blocking(lambda stop: local.verified_locator(
                    artifact.locator, artifact.size_bytes, artifact.checksum_sha256, portable=True,
                    deadline=time.monotonic() + self.settings.backup_timeout_seconds, stop_event=stop,
                ), self._stop_event)
            except BackupAdapterError:
                return False
            if locator is None:
                return False
        if not await self._write(BackupArtifact, artifact.id, claim=claim, expected_status="ready", status="deleting"):
            return False
        try:
            if remote:
                await run_blocking(lambda _stop: local.delete(locator, missing_is_error=True), self._stop_event)
            elif artifact.destination == "local":
                await run_blocking(lambda _stop: local.delete(locator), self._stop_event)
            else:
                await self._on_s3(artifact, lambda store, stop: store.delete(
                    artifact.version_id, deadline=time.monotonic() + 30, stop_event=stop,
                ), stop_event=self._stop_event)
            await self._write(
                BackupArtifact, artifact.id, claim=claim, status="deleted", deleted_at=datetime.now(UTC),
                access_snapshot_enc={}, multipart_upload_id=None, error_code=None, error_message=None,
            )
            return True
        except BackupAdapterError as exc:
            # 远端目录无法确认缺失，保留可用记录供写入主机下载。
            if remote and exc.code == "storage_missing":
                await self._write(BackupArtifact, artifact.id, claim=claim, status="ready")
            else:
                await self._write(
                    BackupArtifact, artifact.id, claim=claim, status="unknown", error_code="cleanup_failed",
                    error_message=BackupAdapterError("cleanup_failed").detail,
                )
            return False

    async def _finish(
        self, row: BackupRun, claim: _Claim, result: str, error: BackupAdapterError | None, cleanup_failed: bool,
    ) -> None:
        async with self.manager.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).where(BackupArtifact.run_id == row.id)))
        for artifact in artifacts:
            if artifact.status == "pending":
                status = "unknown" if result == "interrupted" else "failed"
                await self._write(BackupArtifact, artifact.id, claim=claim, status=status,
                                  error_code=error.code if error else "storage_failed",
                                  error_message=error.detail if error else BackupAdapterError("storage_failed").detail)
        if cleanup_failed:
            await self._write(BackupRun, row.id, claim=claim, cleanup_status="failed",
                              cleanup_error=BackupAdapterError("cleanup_failed").detail)
        configuration = BackupConfiguration.model_validate(row.config_snapshot["configuration"])
        secrets_enc = row.config_snapshot.get("secrets_enc", {})
        if result != "succeeded" or cleanup_failed:
            await self._notify_failure(row, claim, configuration, secrets_enc, result, artifacts, cleanup_failed)
        # SMTP/密码不再需要；S3访问凭据仅保存在仍可用产物的私有加密快照。
        snapshot = deepcopy(row.config_snapshot)
        snapshot["secrets_enc"] = {}
        await self._write(
            BackupRun, row.id, claim=claim, status=result, active_slot=None, worker_key=None,
            finished_at=datetime.now(UTC),
            error_code=error.code if error else None, error_message=error.detail if error else None,
            config_snapshot=snapshot,
        )

    async def _notify_failure(
        self, row: BackupRun, claim: _Claim, configuration: BackupConfiguration, secrets_enc: dict, result: str,
        artifacts: list[BackupArtifact], cleanup_failed: bool,
    ) -> None:
        if not notification_ready(configuration, secrets_enc):
            return
        if self._closing:
            await self._write(BackupRun, row.id, claim=claim, notification_status="unknown",
                              notification_error="服务关停，通知受理结果待核实")
            return
        await self._write(BackupRun, row.id, claim=claim, notification_status="pending")
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
            await self._write(BackupRun, row.id, claim=claim, notification_status="sending")
            outcome = await run_blocking(lambda stop: mail.send_failure_notification(
                config, notice, self._policy, deadline=time.monotonic() + 60, stop_event=stop,
            ), self._stop_event)
            await self._write(BackupRun, row.id, claim=claim, notification_status=outcome.status,
                              notification_error=outcome.error_message)
        except BackupAdapterError:
            await self._write(BackupRun, row.id, claim=claim, notification_status="failed",
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
                local = self._local(row.instance_key)
                if artifact.writer_host == self.hostname:
                    source = await run_blocking(lambda _stop: local.open(artifact.locator, artifact.size_bytes))
                else:
                    source = await run_blocking(lambda stop: local.open_verified(
                        artifact.locator, artifact.size_bytes, artifact.checksum_sha256, portable=True,
                        deadline=time.monotonic() + self.settings.backup_timeout_seconds, stop_event=stop,
                    ))
            else:
                source = await self._on_s3(artifact, lambda store, stop: store.open(
                    artifact.size_bytes, artifact.checksum_sha256, artifact.version_id,
                    deadline=time.monotonic() + self.settings.backup_timeout_seconds, stop_event=stop,
                ))
            return DownloadResource(artifact.filename, artifact.size_bytes, source)
        except BackupAdapterError as exc:
            if exc.code == "storage_missing":
                if destination == "local" and artifact.writer_host != self.hostname:
                    raise BackupUnavailableError("本机无法访问备份副本，请检查共享目录或在写入主机重试") from None
                await self._write(
                    BackupArtifact, artifact.id, expected_status="ready", status="missing",
                    error_code=exc.code, error_message=exc.detail,
                )
                raise BackupGoneError("备份副本已不存在") from None
            raise BackupUnavailableError("备份副本暂时无法访问或身份校验失败") from None

    def _recovery_conditions(self, row: BackupRun, claim: _Claim) -> tuple:
        recovering = (
            BackupRun.status == row.status, BackupRun.worker_key == row.worker_key,
            BackupRun.active_slot == row.active_slot, BackupRun.recovery_worker_key == row.recovery_worker_key,
            or_(BackupRun.recovery_worker_key.is_(None), BackupRun.recovery_worker_key == claim.worker_key,
                workers.confirmed_terminal(BackupRun.recovery_worker_key)),
        )
        if row.active_slot is None:
            return recovering
        if (row.status == "running" and self._drained == _Claim(row.id, row.execution_key, claim.worker_key)
                and row.worker_key == claim.worker_key):
            return recovering
        return (*recovering, or_(BackupRun.worker_key.is_(None), workers.confirmed_terminal(BackupRun.worker_key)))

    async def _reserve_recovery(self, row: BackupRun, claim: _Claim) -> BackupRun | None:
        return await self._reserve(row, claim, *self._recovery_conditions(row, claim),
                                   recovery_worker_key=claim.worker_key)

    async def _recover(self, *, include_legacy_queued: bool = True, include_terminal: bool = True) -> None:
        """只清已结束的执行；原 worker 出现短暂读取失败时不据此接管。"""
        pending_cleanup = or_(
            BackupRun.cleanup_status.in_(("failed", "unknown", "pending")),
            select(BackupArtifact.id).where(
                BackupArtifact.run_id == BackupRun.id, BackupArtifact.status.in_(("deleting", "unknown")),
            ).exists(),
        )
        eligible = BackupRun.active_slot == 1
        if include_terminal:
            eligible = or_(eligible, pending_cleanup)
        async with self.manager.sessionmaker() as db:
            rows = list(await db.scalars(select(BackupRun).where(eligible).order_by(BackupRun.id)))
        key = self.worker.worker_key if self.worker is not None else None
        for row in rows:
            if self._closing or not self._worker_matches(key):
                return
            if not include_legacy_queued and row.status == "queued" and row.worker_key is None:
                continue
            claim = _Claim(row.id, row.execution_key, key, recovery=True)
            if not await self._can_recover(row, claim):
                continue
            async with self._filesystem_scope(self._local(row.instance_key)) as (available, storage_error):
                if storage_error is not None:
                    raise storage_error
                if not available or self._closing:
                    continue
                if (owned := await self._reserve_recovery(row, claim)) is None:
                    continue
                if not self._closing:
                    self._stop_event.clear()
                async with self._watch_worker(claim):
                    await self._recover_run(owned, claim)
        if self._drained is not None:
            row = await self._read(BackupRun, self._drained.run_id)
            if row is None or row.status != "running" or row.worker_key != self._drained.worker_key:
                self._drained = None
        self._uncertain = self._drained is not None

    async def _can_recover(self, row: BackupRun, claim: _Claim) -> bool:
        registered = {row.worker_key, row.recovery_worker_key} - {None, claim.worker_key}
        async with self.manager.sessionmaker() as db:
            if self.worker is not None and registered:
                for key in sorted(registered):
                    await workers.prepare_orphan_scan(db, self.worker.engine.lease_seconds, worker_key=key)
                await db.commit()
            return await db.scalar(select(BackupRun.id).where(
                BackupRun.id == row.id, BackupRun.execution_key == row.execution_key,
                *self._recovery_conditions(row, claim),
            )) is not None

    async def _recover_run(self, row: BackupRun, claim: _Claim) -> None:
        active = row.active_slot is not None
        async with self.manager.sessionmaker() as db:
            statement = select(BackupArtifact).where(BackupArtifact.run_id == row.id)
            if not active:
                statement = statement.where(BackupArtifact.status.in_(("deleting", "unknown")))
            artifacts = list(await db.scalars(statement))
        for artifact in artifacts:
            await self._recover_artifact(artifact, claim)

        values = {"recovery_worker_key": None}
        if active or row.cleanup_status in ("failed", "unknown", "pending"):
            local = self._local(row.instance_key)
            same_host = row.execution_host is None or row.execution_host == self.hostname
            pending = False
            failed = False
            if row.encrypted:
                if same_host:
                    try:
                        await self._clean_encryption_home(row)
                    except BackupAdapterError:
                        failed = True
                else:
                    pending = True
            try:
                removed = await run_blocking(lambda _stop: local.clean_temporary(row.execution_key))
                pending = pending or (not same_host and not removed)
            except BackupAdapterError:
                failed = True
            if failed:
                values.update(cleanup_status="failed", cleanup_error=BackupAdapterError("cleanup_failed").detail)
            elif active and (pending or row.cleanup_status == "pending") and row.cleanup_status != "failed":
                values.update(cleanup_status="unknown", cleanup_error="上次清理结果待核实")
        if active:
            snapshot = deepcopy(row.config_snapshot)
            snapshot["secrets_enc"] = {}
            values.update(
                status="interrupted", active_slot=None, worker_key=None, finished_at=datetime.now(UTC),
                error_code="interrupted", error_message=BackupAdapterError("interrupted").detail,
                config_snapshot=snapshot,
            )
            if row.notification_status in ("pending", "sending"):
                values.update(
                    notification_status="unknown", notification_error=BackupAdapterError("mail_unknown").detail,
                )
        await self._write(BackupRun, row.id, claim=claim, **values)

    async def _recover_artifact(self, artifact: BackupArtifact, claim: _Claim) -> None:
        if artifact.status in ("deleted", "failed", "missing"):
            return
        was_deleting = artifact.status == "deleting" or artifact.error_code == "cleanup_failed"
        portable = artifact.destination == "local" and artifact.writer_host != self.hostname
        status, version, locator = "unknown", artifact.version_id, artifact.locator
        try:
            if not artifact.locator or artifact.size_bytes is None or not artifact.checksum_sha256:
                raise BackupAdapterError("storage_identity")
            if artifact.destination == "local":
                local = self._local(artifact.locator["instance_key"])
                verified = await run_blocking(lambda stop: local.verified_locator(
                    artifact.locator, artifact.size_bytes, artifact.checksum_sha256, portable=portable,
                    deadline=time.monotonic() + self.settings.backup_timeout_seconds, stop_event=stop,
                ), self._stop_event)
                if not verified:
                    raise BackupAdapterError("storage_missing")
                if not portable:
                    locator = verified
            else:
                version = await self._on_s3(artifact, lambda store, stop: store.head(
                    artifact.size_bytes, artifact.checksum_sha256, artifact.version_id,
                    deadline=time.monotonic() + 30, stop_event=stop,
                ))
            status = "ready"
        except BackupAdapterError as exc:
            if exc.code == "storage_missing":
                if portable:
                    return
                status = "deleted" if was_deleting else "missing" if artifact.status == "ready" else "unknown"
        except (KeyError, TypeError, ValueError):
            status = "unknown"
        upload_aborted = False
        if artifact.multipart_upload_id and artifact.destination == "s3":
            # HEAD 可能很慢。发出中止前再次确认仍握有这次清理权。
            async with self.manager.sessionmaker() as db:
                await self._check_claim(db, claim)
            try:
                await self._on_s3(artifact, lambda store, stop: store.abort(
                    artifact.multipart_upload_id, deadline=time.monotonic() + 30, stop_event=stop,
                ), stop_event=self._stop_event)
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
        await self._write(BackupArtifact, artifact.id, claim=claim, expected_status=artifact.status, **values)
