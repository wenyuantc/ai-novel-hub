"""持久覆盖的单一写入/发布边界，保持请求和任务共享的 Settings 对象身份。"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import ConflictError
from app.models import SystemSettings, User
from app.schemas.system_settings import (
    SYSTEM_SETTING_KEYS,
    SystemSettingAuditChange,
    SystemSettingBoolItem,
    SystemSettingIntItem,
    SystemSettingsOut,
    SystemSettingsUpdate,
)
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation, AuditOperationError
from app.services.mcp import settings_gate
from app.services.system_settings_snapshot import configure_deployment, validated_row

logger = logging.getLogger(__name__)
CONFLICT = "系统设置已被其他管理员修改，请刷新后重新确认"
UNKNOWN = "系统设置保存状态暂时无法确认，请稍后刷新核实"


class SystemSettingsManager:
    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession], notify: Callable[[], None],
                 *, poll_interval: float):
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.runtime = authz.runtime_for_factory(sessionmaker)
        self.notify = notify
        self._source_state = configure_deployment(sessionmaker, settings)
        self._deployment = {key: getattr(self._source_state.deployment, key) for key in SYSTEM_SETTING_KEYS}
        self._poll_interval = poll_interval
        self._poller: asyncio.Task | None = None
        self._refresh = asyncio.Event()
        self.notify_peers: Callable[[], None] | None = None
        # 必须先确认持久设置，再允许依赖运行设置的实际发送/准入。
        self.runtime.settings_confirmed = False
        self._defaults = {key: Settings.model_fields[key].default for key in SYSTEM_SETTING_KEYS}
        self._deployment_sources = {
            key: "deployment" if key in settings.model_fields_set else "default" for key in SYSTEM_SETTING_KEYS
        }
        self._lock = asyncio.Lock()
        self._writes: set[asyncio.Task] = set()
        self._recovery: asyncio.Task | None = None
        self._uncertain = False
        self._closing = False
        self._revision = 0
        self._overrides: dict = {}
        self._updated_at: datetime | None = None

    def _source(self, key, overrides):
        return "admin" if key in overrides else self._deployment_sources[key]

    def _publish(self, revision: int, overrides: dict, updated_at: datetime | None) -> SystemSettingsOut:
        # 只发布白名单；此方法没有 await，同一事件循环不会观察到半套更新。
        effective = {**self._deployment, **overrides}
        for key in SYSTEM_SETTING_KEYS:
            setattr(self.settings, key, effective[key])
        self._revision, self._overrides, self._updated_at = revision, dict(overrides), updated_at
        self._uncertain = False
        self.runtime.settings_confirmed = True
        self.notify()
        return self._output()

    def _output(self) -> SystemSettingsOut:
        items = []
        for key in SYSTEM_SETTING_KEYS:
            values = dict(
                key=key, value=self._overrides.get(key, self._deployment[key]),
                deployment_value=self._deployment[key], default_value=self._defaults[key],
                source=self._source(key, self._overrides), deployment_source=self._deployment_sources[key],
                overridden=key in self._overrides,
            )
            item_type = SystemSettingBoolItem if type(self._deployment[key]) is bool else SystemSettingIntItem
            items.append(item_type(**values))
        return SystemSettingsOut(revision=self._revision, updated_at=self._updated_at, items=items)

    async def _reload(self) -> SystemSettingsOut:
        async with self.runtime.hold():
            async with self.sessionmaker() as db:
                # 在 SQLite 默认回滚日志模式，真实读事务保护读取到发布这段窗口。
                # WAL 的读快照不阻挡写方提交，须短暂保留写权限。
                if db.get_bind().dialect.name == "sqlite":
                    conn = await db.connection()
                    journal = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
                    await conn.exec_driver_sql("BEGIN IMMEDIATE" if journal == "wal" else "BEGIN")
                await settings_gate.lock_for_reload(db)
                observed = time.monotonic()
                row = await db.get(SystemSettings, 1)
                values = validated_row(row)
                # 发布时共享门禁仍在本事务内，避免其他实例提交穿过读/发布窗口。
                self._source_state.verified_until = observed + self._poll_interval
                if self.runtime.settings_confirmed and not self._uncertain and values[:2] == (
                    self._revision, self._overrides,
                ):
                    return self._output()
                return self._publish(*values)

    async def load(self) -> SystemSettingsOut:
        async with self._lock:
            try:
                result = await self._reload()
                if self._poller is None and not self._closing:
                    self._poller = asyncio.create_task(self._poll(), name="system-settings-poll")
                return result
            except Exception:
                logger.error("系统设置持久状态读取失败")
                self._schedule_recovery()
                raise AuditOperationError(UNKNOWN) from None

    def request_refresh(self) -> None:
        """只提示本实例重读数据库，由一个轮询协程合并重复请求。"""
        self._refresh.set()

    async def _poll(self) -> None:
        while not self._closing:
            delay = max(0, self._source_state.verified_until - time.monotonic())
            try:
                async with asyncio.timeout(delay):
                    await self._refresh.wait()
            except TimeoutError:
                pass
            self._refresh.clear()
            async with self._lock:
                if self._closing:
                    return
                try:
                    await self._reload()
                except Exception:
                    logger.error("系统设置持久状态读取失败")
                    self._schedule_recovery()
                    # 上一轮已过期；限制本轮失败后的重试频率，不延长消费者缓存。
                else:
                    continue
            await asyncio.sleep(self._poll_interval)

    def _track_write(self, task: asyncio.Task) -> None:
        self._writes.add(task)

        def done(completed: asyncio.Task):
            self._writes.discard(completed)
            # HTTP调用者可能已取消；取走异常避免任务日志输出SQL参数。
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

    async def update(self, actor_id: int, request: SystemSettingsUpdate) -> SystemSettingsOut:
        if self._closing:
            raise AuditOperationError("服务正在关闭，请稍后重试")
        task = asyncio.create_task(self._update(actor_id, request), name="system-settings-write")
        self._track_write(task)
        return await asyncio.shield(task)

    async def _update(self, actor_id: int, request: SystemSettingsUpdate) -> SystemSettingsOut:
        async with self._lock:
            try:
                # 先恢复任何先前不确定提交；失败时禁止继续覆盖。
                await self._reload()
            except Exception:
                self._schedule_recovery()
                raise AuditOperationError(UNKNOWN) from None
            operation_id = str(uuid4())
            changes = request.changes.model_dump(exclude_unset=True)
            candidate = dict(self._overrides)
            for key, value in changes.items():
                if value is None:
                    candidate.pop(key, None)
                else:
                    candidate[key] = value
            now = datetime.now(UTC)
            revision = self._revision + 1
            # 包括提交未知后的即时重读，不能释放后让领取看见旧Settings。
            async with self.runtime.hold():
                try:
                    async with self.sessionmaker() as db:
                        access = await authz.require(db, actor_id, "settings.update", write=True)
                        # User → MCP 设置独占门禁 → SystemSettings；发送侧不持授权写门禁。
                        await settings_gate.lock_for_update(db)
                        current = validated_row(await db.get(SystemSettings, 1, populate_existing=True))
                        actor = await db.get(User, access.id, populate_existing=True)
                        async with AuditOperation(
                            db, actor, "system_settings.update", "system_settings", 1, operation_id=operation_id,
                        ) as audit:
                            audit.values["target_label"] = "系统设置"
                            if (request.revision != self._revision or current[0] != self._revision
                                    or current[1] != self._overrides):
                                raise ConflictError(CONFLICT)
                            before = self._overrides
                            audit.summary.settings_changes = [
                                SystemSettingAuditChange(
                                    key=key, before_override=before.get(key), after_override=candidate.get(key),
                                    before_effective=before.get(key, self._deployment[key]),
                                    after_effective=candidate.get(key, self._deployment[key]),
                                    before_source=self._source(key, before), after_source=self._source(key, candidate),
                                ) for key in changes
                                if (key in before) != (key in candidate) or before.get(key) != candidate.get(key)
                            ]
                            if self._revision == 0:
                                db.add(SystemSettings(
                                    id=1, revision=revision, overrides=candidate,
                                    last_operation_id=operation_id, updated_at=now,
                                ))
                                try:
                                    await db.flush()
                                except IntegrityError as exc:
                                    raise ConflictError(CONFLICT) from exc
                            else:
                                result = await db.execute(
                                    update(SystemSettings)
                                    .where(SystemSettings.id == 1, SystemSettings.revision == self._revision)
                                    .values(revision=revision, overrides=candidate,
                                            last_operation_id=operation_id, updated_at=now)
                                    .returning(SystemSettings.id)
                                )
                                if result.scalar_one_or_none() is None:
                                    raise ConflictError(CONFLICT)
                            # 其他实例可在物理提交后立即写下一版。回执迟到不能延长本版的新鲜期。
                            valid_until = time.monotonic() + self._poll_interval
                            await audit.commit()
                            if self.notify_peers is not None:
                                self.notify_peers()
                            if time.monotonic() >= valid_until:
                                return await self._reload()
                            self._source_state.verified_until = valid_until
                            return self._publish(revision, candidate, now)
                except BaseException:
                    # 包括commit已落盘但确认抛错；读取当前持久事实，不重试旧候选。
                    self._uncertain = True
                    self.runtime.settings_confirmed = False
                    try:
                        await self._reload()
                    except Exception:
                        self._schedule_recovery()
                    raise

    def _schedule_recovery(self) -> None:
        self._uncertain = True
        self.runtime.settings_confirmed = False
        if not self._closing and (self._recovery is None or self._recovery.done()):
            self._recovery = asyncio.create_task(self._recover(), name="system-settings-recovery")

    async def _recover(self) -> None:
        while self._uncertain and not self._closing:
            await asyncio.sleep(1)
            async with self._lock:
                try:
                    await self._reload()
                except Exception:
                    logger.error("系统设置持久状态仍待确认")

    async def close(self) -> None:
        self._closing = True
        if self._poller is not None:
            self._poller.cancel()
            await asyncio.gather(self._poller, return_exceptions=True)
        if self._writes:
            await asyncio.gather(*self._writes, return_exceptions=True)
        if self._recovery is not None:
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)
