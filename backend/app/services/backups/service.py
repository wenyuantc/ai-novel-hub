"""备份配置与准入的短事务边界；长快照/网络I/O由独立运行器执行。"""

import asyncio
import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import BackupArtifact, BackupRun, BackupSettings
from app.schemas.backup import (
    SECRET_KEYS,
    BackupArtifactOut,
    BackupAuditChange,
    BackupConfiguration,
    BackupManualRequest,
    BackupOverview,
    BackupRunFilters,
    BackupRunList,
    BackupRunOut,
    BackupSettingsOut,
    BackupSettingsUpdate,
)
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation, AuditOperationError

logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
UNKNOWN = "备份持久状态暂时无法确认，请稍后刷新核实"


class BackupUnavailableError(AppError):
    status_code = 503


class BackupGoneError(AppError):
    status_code = 410


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def next_schedule(configuration: BackupConfiguration, now: datetime) -> datetime | None:
    if not configuration.schedule_enabled:
        return None
    local = as_utc(now).astimezone(BEIJING)
    hour, minute = map(int, configuration.schedule_time.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


@dataclass(frozen=True)
class BackupSnapshot:
    revision: int
    instance_key: str
    configuration: BackupConfiguration
    secrets_enc: dict[str, str] = field(repr=False)
    next_scheduled_at: datetime | None
    updated_at: datetime | None


def notification_ready(configuration: BackupConfiguration, secrets_enc: dict) -> bool:
    credentials = bool(secrets_enc.get("smtp_username")) == bool(secrets_enc.get("smtp_password"))
    return bool(
        configuration.smtp_host and configuration.smtp_from_address and configuration.smtp_recipients and credentials
    )


def configuration_errors(configuration: BackupConfiguration, secrets_enc: dict) -> list[str]:
    errors = []
    if not configuration.local_enabled and not configuration.s3_enabled:
        errors.append("请至少启用一个存储目的地")
    if configuration.encryption_enabled and not secrets_enc.get("backup_password"):
        errors.append("加密已开启，请先设置备份密码")
    if configuration.s3_enabled and not all(
        (
            configuration.s3_endpoint,
            configuration.s3_region,
            configuration.s3_bucket,
            secrets_enc.get("s3_access_key"),
            secrets_enc.get("s3_secret_key"),
        )
    ):
        errors.append("S3 连接配置不完整")
    return errors


def artifact_output(row: BackupArtifact) -> BackupArtifactOut:
    return BackupArtifactOut(
        id=row.id,
        destination=row.destination,
        status=row.status,
        filename=row.filename,
        size_bytes=row.size_bytes,
        checksum_sha256=row.checksum_sha256,
        error_code=row.error_code,
        error_message=row.error_message,
        downloadable=row.status == "ready",
        deleted_at=row.deleted_at,
    )


def run_output(row: BackupRun, artifacts: list[BackupArtifact]) -> BackupRunOut:
    return BackupRunOut(
        **{key: getattr(row, key) for key in BackupRunOut.model_fields if key != "artifacts"},
        artifacts=[artifact_output(artifact) for artifact in artifacts],
    )


class BackupManager:
    def __init__(
        self,
        settings: Settings,
        cipher: SecretCipher,
        sessionmaker: async_sessionmaker[AsyncSession],
        notify: Callable[[], None] = lambda: None,
    ):
        self.settings = settings
        self.cipher = cipher
        self.sessionmaker = sessionmaker
        self.notify = notify
        self.tool_readiness: Callable[[BackupConfiguration], list[str]] = lambda configuration: []
        self.authorization = authz.runtime_for_factory(sessionmaker)
        self.lock = asyncio.Lock()
        self.closing = False
        self.confirmed = False
        self._snapshot = BackupSnapshot(0, "", BackupConfiguration(), {}, None, None)
        self._writes: set[asyncio.Task] = set()
        self._recovery: asyncio.Task | None = None

    def _publish(self, row: BackupSettings | None) -> BackupSettingsOut:
        if row is None:
            snapshot = BackupSnapshot(0, "", BackupConfiguration(), {}, None, None)
        else:
            if (
                row.revision < 1
                or set(row.secrets_enc) - SECRET_KEYS
                or any(not isinstance(value, str) or not value for value in row.secrets_enc.values())
            ):
                raise ValueError("持久备份设置无效")
            snapshot = BackupSnapshot(
                row.revision,
                row.instance_key,
                BackupConfiguration.model_validate(row.configuration),
                dict(row.secrets_enc),
                as_utc(row.next_scheduled_at) if row.next_scheduled_at else None,
                as_utc(row.updated_at),
            )
        self._snapshot = snapshot
        self.confirmed = True
        return self._settings_output(snapshot)

    @staticmethod
    def _settings_output(snapshot: BackupSnapshot) -> BackupSettingsOut:
        return BackupSettingsOut(
            **snapshot.configuration.model_dump(),
            revision=snapshot.revision,
            updated_at=snapshot.updated_at,
            **{key + "_configured": bool(snapshot.secrets_enc.get(key)) for key in SECRET_KEYS},
        )

    async def _reload(self) -> BackupSettingsOut:
        async with self.authorization.hold(), self.sessionmaker() as db:
            row = await db.get(BackupSettings, 1)
            return self._publish(row)

    async def _load_locked(self) -> BackupSettingsOut:
        """持有manager锁的入口共用读取边界，非法配置不回退默认值。"""
        try:
            return await self._reload()
        except Exception:
            self._schedule_recovery()
            logger.error("备份配置持久状态读取失败")
            raise BackupUnavailableError(UNKNOWN) from None

    async def load(self) -> BackupSettingsOut:
        async with self.lock:
            return await self._load_locked()

    async def snapshot(self) -> BackupSnapshot:
        await self.load()
        # No await after the published copy is selected; never expose mutable manager state.
        current = self._snapshot
        return BackupSnapshot(
            current.revision,
            current.instance_key,
            current.configuration.model_copy(deep=True),
            dict(current.secrets_enc),
            current.next_scheduled_at,
            current.updated_at,
        )

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self._writes.add(task)

        def done(completed: asyncio.Task):
            self._writes.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    def _ensure_open(self) -> None:
        if self.closing:
            raise BackupUnavailableError("服务正在关闭，请稍后重试")

    def _readiness_errors(self, snapshot: BackupSnapshot) -> list[str]:
        errors = configuration_errors(snapshot.configuration, snapshot.secrets_enc)
        engine = make_url(self.settings.database_url)
        if engine.get_backend_name() not in ("postgresql", "sqlite"):
            errors.append("当前数据库类型不支持整库备份")
        elif engine.get_backend_name() == "sqlite" and (not engine.database or engine.database == ":memory:"):
            errors.append("内存 SQLite 不支持持久备份")
        errors.extend(self.tool_readiness(snapshot.configuration))
        if not self.confirmed:
            errors.append(UNKNOWN)
        return errors

    async def update(self, actor: authz.AuthenticatedActor, request: BackupSettingsUpdate) -> BackupSettingsOut:
        self._ensure_open()
        return await asyncio.shield(
            self._track(
                asyncio.create_task(
                    self._update(actor, request),
                    name="backup-settings-write",
                )
            )
        )

    async def _update(self, actor: authz.AuthenticatedActor, request: BackupSettingsUpdate) -> BackupSettingsOut:
        async with self.lock:
            await self._load_locked()
            before = self._snapshot
            values = before.configuration.model_dump(mode="json")
            secrets_enc = dict(before.secrets_enc)
            changes = request.changes.model_fields_set
            for key in changes:
                value = getattr(request.changes, key)
                if key in SECRET_KEYS:
                    if value is None:
                        secrets_enc.pop(key, None)
                    else:
                        secrets_enc[key] = self.cipher.encrypt(value.get_secret_value())
                else:
                    values[key] = value
            candidate = BackupConfiguration.model_validate(values)
            revision, now, operation_id = before.revision + 1, datetime.now(UTC), str(uuid4())
            async with self.authorization.hold():
                try:
                    async with self.sessionmaker() as db:
                        await authz.require(db, actor, "backups.configure", write=True)
                        async with AuditOperation(
                            db,
                            actor.id,
                            "backup.settings.update",
                            "backup_settings",
                            1,
                            operation_id=operation_id,
                        ) as audit:
                            audit.values["target_label"] = "整库备份设置"
                            if request.revision != before.revision:
                                raise ConflictError(
                                    "备份配置已变化，请刷新后重新确认", extra={"code": "settings_conflict"}
                                )
                            if candidate.schedule_enabled:
                                errors = configuration_errors(candidate, secrets_enc)
                                if not notification_ready(candidate, secrets_enc):
                                    errors.append("开启定时前请完善失败邮件配置")
                                if errors:
                                    raise BadRequestError("；".join(errors), extra={"code": "backup_not_ready"})
                            schedule_changed = bool(changes & {"schedule_time", "schedule_enabled"})
                            due = next_schedule(candidate, now) if schedule_changed else before.next_scheduled_at
                            if due is not None and due.astimezone(BEIJING).date() == now.astimezone(BEIJING).date():
                                ran_today = await db.scalar(
                                    select(BackupRun.id).where(
                                        BackupRun.scheduled_date == now.astimezone(BEIJING).date().isoformat(),
                                    )
                                )
                                if ran_today is not None:
                                    due += timedelta(days=1)
                            audit.summary.backup_change = BackupAuditChange(
                                fields=sorted(changes),
                                settings_revision=revision,
                            )
                            write_values = dict(
                                revision=revision,
                                instance_key=before.instance_key or str(uuid4()),
                                configuration=candidate.model_dump(mode="json"),
                                secrets_enc=secrets_enc,
                                next_scheduled_at=due,
                                last_operation_id=operation_id,
                                updated_at=now,
                            )
                            if before.revision == 0:
                                db.add(BackupSettings(id=1, **write_values))
                                try:
                                    await db.flush()
                                except IntegrityError as exc:
                                    raise ConflictError(
                                        "备份配置已变化，请刷新后重新确认",
                                        extra={"code": "settings_conflict"},
                                    ) from exc
                            else:
                                changed = await db.execute(
                                    update(BackupSettings)
                                    .where(
                                        BackupSettings.id == 1,
                                        BackupSettings.revision == before.revision,
                                    )
                                    .values(**write_values)
                                    .returning(BackupSettings.id)
                                )
                                if changed.scalar_one_or_none() is None:
                                    raise ConflictError(
                                        "备份配置已变化，请刷新后重新确认",
                                        extra={"code": "settings_conflict"},
                                    )
                            row = BackupSettings(id=1, **write_values)
                            await audit.commit()
                            result = self._publish(row)
                            self.notify()
                            return result
                except BaseException:
                    await self._confirm_after_error()
                    raise

    async def _confirm_after_error(self) -> None:
        self.confirmed = False
        try:
            await self._reload()
        except Exception:
            self._schedule_recovery()

    def _schedule_recovery(self) -> None:
        self.confirmed = False
        if not self.closing and (self._recovery is None or self._recovery.done()):
            self._recovery = asyncio.create_task(self._recover(), name="backup-state-recovery")

    async def _recover(self) -> None:
        while not self.closing and not self.confirmed:
            await asyncio.sleep(1)
            async with self.lock:
                try:
                    await self._reload()
                except Exception:
                    logger.error("备份配置持久状态仍待确认")

    async def accept_manual(self, actor: authz.AuthenticatedActor, request: BackupManualRequest) -> BackupRunOut:
        self._ensure_open()
        return await asyncio.shield(
            self._track(
                asyncio.create_task(
                    self._accept(actor, request, None),
                    name="backup-manual-accept",
                )
            )
        )

    async def accept_scheduled(self, now: datetime | None = None) -> BackupRunOut | None:
        if self.closing or not self.confirmed:
            return None
        return await self._accept(None, None, now or datetime.now(UTC))

    async def _accept(
        self,
        actor: authz.AuthenticatedActor | None,
        request: BackupManualRequest | None,
        scheduled_now: datetime | None,
    ) -> BackupRunOut | None:
        async with self.lock:
            try:
                await self._load_locked()
                snapshot = self._snapshot
                async with self.authorization.hold(), self.sessionmaker() as db:
                    access = await authz.require(db, actor, "backups.run", write=True) if actor is not None else None
                    if request is not None:
                        existing = await db.scalar(
                            select(BackupRun).where(BackupRun.request_id == str(request.request_id))
                        )
                        if existing is not None:
                            if (
                                existing.actor_user_id != actor.id
                                or existing.settings_revision != request.settings_revision
                            ):
                                raise ConflictError("请求标识已用于其他备份意图", extra={"code": "request_conflict"})
                            return await self._run_from_db(db, existing)
                    now = as_utc(scheduled_now or datetime.now(UTC))
                    scheduled_date = None
                    if actor is None:
                        due = snapshot.next_scheduled_at
                        if not snapshot.configuration.schedule_enabled or due is None or due > now:
                            return None
                        local = now.astimezone(BEIJING)
                        hour, minute = map(int, snapshot.configuration.schedule_time.split(":"))
                        slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if slot > local:
                            slot -= timedelta(days=1)
                        scheduled_date = slot.date().isoformat()
                        if await db.scalar(select(BackupRun.id).where(BackupRun.scheduled_date == scheduled_date)):
                            await db.execute(
                                update(BackupSettings)
                                .where(BackupSettings.id == 1)
                                .values(
                                    next_scheduled_at=next_schedule(snapshot.configuration, now),
                                )
                            )
                            await db.commit()
                            return None
                    if request is not None and request.settings_revision != snapshot.revision:
                        raise ConflictError("备份配置已变化，请重新确认", extra={"code": "settings_conflict"})
                    errors = self._readiness_errors(snapshot)
                    if snapshot.revision == 0:
                        errors.append("请先保存备份配置")
                    if errors:
                        if actor is None:
                            return None
                        raise BadRequestError("；".join(errors), extra={"code": "backup_not_ready"})
                    if await db.scalar(select(BackupRun.id).where(BackupRun.active_slot == 1)):
                        if actor is None:
                            return None
                        raise ConflictError("已有备份正在执行，请稍后重试", extra={"code": "backup_busy"})
                    engine = make_url(self.settings.database_url).get_backend_name()
                    file_format = "dump" if engine == "postgresql" else "sqlite3"
                    encrypted = snapshot.configuration.encryption_enabled
                    run = BackupRun(
                        execution_key=str(uuid4()),
                        instance_key=snapshot.instance_key,
                        request_id=str(request.request_id) if request else None,
                        scheduled_date=scheduled_date,
                        source="manual" if actor else "scheduled",
                        actor_user_id=actor.id if actor else None,
                        actor_username=access.username if access else None,
                        settings_revision=snapshot.revision,
                        config_snapshot={
                            "configuration": snapshot.configuration.model_dump(mode="json"),
                            "secrets_enc": deepcopy(snapshot.secrets_enc),
                        },
                        database_engine=engine,
                        schema_revision=await self._schema_revision(db),
                        encrypted=encrypted,
                        file_format=file_format + (".gpg" if encrypted else ""),
                        status="queued",
                        active_slot=1,
                        created_at=now,
                        notification_status="not_configured",
                        cleanup_status="not_required",
                    )
                    if actor is not None:
                        async with AuditOperation(db, actor.id, "backup.run.accept", "backup_run") as audit:
                            await self._insert_run(db, run, snapshot, now)
                            audit.target(run, label="整库备份受理")
                            audit.summary.backup_change = BackupAuditChange(
                                settings_revision=snapshot.revision,
                                execution_key=run.execution_key,
                                encrypted=encrypted,
                            )
                            await audit.commit()
                    else:
                        await self._insert_run(db, run, snapshot, now)
                        await db.execute(
                            update(BackupSettings)
                            .where(BackupSettings.id == 1)
                            .values(
                                next_scheduled_at=next_schedule(snapshot.configuration, now),
                            )
                        )
                        await db.commit()
                    self.notify()
                    return await self._run_from_db(db, run)
            except AppError as exc:
                if isinstance(exc, AuditOperationError):
                    await self._confirm_after_error()
                raise
            except BaseException as exc:
                await self._confirm_after_error()
                if not isinstance(exc, Exception):
                    raise
                logger.error("备份受理持久状态待核实")
                raise AuditOperationError(UNKNOWN) from None

    @staticmethod
    async def _schema_revision(db: AsyncSession) -> str | None:
        connection = await db.connection()
        if await connection.run_sync(lambda conn: inspect(conn).has_table("alembic_version")):
            return await db.scalar(text("SELECT version_num FROM alembic_version"))
        return None

    @staticmethod
    async def _insert_run(db: AsyncSession, run: BackupRun, snapshot: BackupSnapshot, now: datetime) -> None:
        db.add(run)
        try:
            await db.flush()
        except IntegrityError as exc:
            # The unique active slot is the durable second guard; do not continue an aborted transaction.
            raise ConflictError("备份受理发生竞争，请刷新核实", extra={"code": "backup_busy"}) from exc
        for destination in ("local", "s3"):
            if getattr(snapshot.configuration, destination + "_enabled"):
                db.add(
                    BackupArtifact(
                        run_id=run.id,
                        destination=destination,
                        status="pending",
                        created_at=now,
                        locator={},
                        access_snapshot_enc={},
                    )
                )

    @staticmethod
    async def _run_from_db(db: AsyncSession, row: BackupRun) -> BackupRunOut:
        artifacts = list(
            await db.scalars(
                select(BackupArtifact).where(BackupArtifact.run_id == row.id).order_by(BackupArtifact.id),
            )
        )
        return run_output(row, artifacts)

    async def read_settings(self, actor: authz.AuthenticatedActor) -> BackupSettingsOut:
        result = await self.load()
        async with self.sessionmaker() as db:
            await authz.require(db, actor, "backups.configure")
        return result

    async def list_runs(self, actor: authz.AuthenticatedActor, filters: BackupRunFilters) -> BackupRunList:
        async with self.sessionmaker() as db:
            await authz.require(db, actor, "backups.read")
            terms = []
            if filters.status is not None:
                terms.append(BackupRun.status == filters.status)
            if filters.source is not None:
                terms.append(BackupRun.source == filters.source)
            total = await db.scalar(select(func.count()).select_from(BackupRun).where(*terms))
            rows = list(
                await db.scalars(
                    select(BackupRun)
                    .where(*terms)
                    .order_by(BackupRun.created_at.desc(), BackupRun.id.desc())
                    .offset((filters.page - 1) * filters.page_size)
                    .limit(filters.page_size),
                )
            )
            artifacts = (
                list(
                    await db.scalars(
                        select(BackupArtifact)
                        .where(BackupArtifact.run_id.in_([row.id for row in rows]))
                        .order_by(BackupArtifact.id),
                    )
                )
                if rows
                else []
            )
            grouped: dict[int, list[BackupArtifact]] = {}
            for artifact in artifacts:
                grouped.setdefault(artifact.run_id, []).append(artifact)
            return BackupRunList(items=[run_output(row, grouped.get(row.id, [])) for row in rows], total=total or 0)

    async def get_run(self, actor: authz.AuthenticatedActor, run_id: int) -> BackupRunOut:
        async with self.sessionmaker() as db:
            await authz.require(db, actor, "backups.read")
            row = await db.get(BackupRun, run_id)
            if row is None:
                raise NotFoundError("备份记录不存在")
            return await self._run_from_db(db, row)

    async def overview(self, actor: authz.AuthenticatedActor) -> BackupOverview:
        snapshot = await self.snapshot()
        async with self.sessionmaker() as db:
            await authz.require(db, actor, "backups.read")

            async def latest(*terms):
                row = await db.scalar(select(BackupRun).where(*terms).order_by(BackupRun.id.desc()).limit(1))
                return await self._run_from_db(db, row) if row is not None else None

            active = await latest(BackupRun.active_slot == 1)
            success = await latest(BackupRun.status == "succeeded")
            failure = await latest(BackupRun.status.in_(("partial", "failed", "interrupted")))
            available = await latest(
                BackupRun.id.in_(select(BackupArtifact.run_id).where(BackupArtifact.status == "ready"))
            )
        errors = self._readiness_errors(snapshot)
        if snapshot.revision == 0:
            errors.append("请先保存备份配置")
        config = snapshot.configuration
        return BackupOverview(
            revision=snapshot.revision,
            schedule_enabled=config.schedule_enabled,
            schedule_time=config.schedule_time,
            local_enabled=config.local_enabled,
            s3_enabled=config.s3_enabled,
            local_keep=config.local_keep,
            s3_keep=config.s3_keep,
            encryption_enabled=config.encryption_enabled,
            ready=not errors,
            readiness_errors=errors,
            notification_ready=notification_ready(config, snapshot.secrets_enc),
            next_scheduled_at=snapshot.next_scheduled_at,
            active_run=active,
            last_success=success,
            last_failure=failure,
            last_available=available,
        )

    async def authorize_download(
        self,
        actor: authz.AuthenticatedActor,
        run_id: int,
        destination: str,
        *,
        audit: bool = False,
    ) -> BackupArtifactOut:
        async with self.lock, self.authorization.hold(), self.sessionmaker() as db:
            await authz.require(db, actor, "backups.download", write=audit)
            row = await db.get(BackupRun, run_id)
            if row is None:
                raise NotFoundError("备份记录不存在")
            artifact = await db.scalar(
                select(BackupArtifact).where(
                    BackupArtifact.run_id == run_id,
                    BackupArtifact.destination == destination,
                )
            )
            if artifact is None:
                raise NotFoundError("该备份未启用此目的地")
            if artifact.status != "ready":
                raise BackupGoneError("备份副本已过期、缺失或不可用")
            if audit:
                async with AuditOperation(db, actor.id, "backup.download.authorize", "backup_run", run_id) as operation:
                    operation.values["target_label"] = "整库备份下载授权"
                    operation.summary.backup_change = BackupAuditChange(
                        settings_revision=row.settings_revision,
                        execution_key=row.execution_key,
                        destination=destination,
                        encrypted=row.encrypted,
                    )
                    await operation.commit()
            return artifact_output(artifact)

    async def close(self) -> None:
        self.closing = True
        if self._writes:
            await asyncio.gather(*self._writes, return_exceptions=True)
        if self._recovery is not None:
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)
