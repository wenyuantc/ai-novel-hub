"""明确接入的管理写边界；不监听 ORM、不替换 session、不采集请求。

调用方必须通过 commit() 提交，再进行 refresh/响应投影。失败路径先整体回滚，
仅用提前保存的 primitive 快照开启新事务；同一 operation_id 不会覆盖已提交结果。
"""

import hashlib
import logging
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, UnauthorizedError
from app.models import AdminAuditLog, ChapterVersion, Project, Skill, User
from app.schemas.audit import AuditAction, AuditSummary, AuditTargetType, AuditValueChange, AuditVersionRef

logger = logging.getLogger(__name__)
SAFE_VALUE_FIELDS = frozenset(
    {
        "is_admin",
        "is_active",
        "target_words",
        "status",
        "chapter_no",
        "outline_id",
        "plan_id",
        "style_id",
        "starred",
        "hidden_by",
        "auto_analyze",
        "default_style_id",
    }
)
ERROR_MESSAGES = {
    "bad_request": "业务条件不满足",
    "forbidden": "该管理操作不被允许",
    "not_found": "目标不存在或不可访问",
    "conflict": "操作与当前状态冲突",
    "system_error": "操作因系统错误失败",
    "invalid_preview": "清理预览无效或已过期",
    "preview_actor_mismatch": "清理预览与操作人不匹配",
    "preview_replayed": "清理预览已使用，请重新预览",
}


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _SafeSerializationFailure(Exception):
    """只保留供 Agent 识别的序列化冲突码，不持有原始数据库异常。"""

    sqlstate = "40001"


class AuditOperation:
    def __init__(
        self,
        db: AsyncSession,
        actor: User | int | None,
        action: AuditAction,
        target_type: AuditTargetType,
        target_id: int | None = None,
        *,
        project_id: int | None = None,
        enabled: bool = True,
        operation_id: str | None = None,
        include_own_project: bool = False,
        system_expiry: bool = False,
        bootstrap: bool = False,
        local_recovery: bool = False,
    ):
        self.db = db
        if system_expiry and (actor is not None or action != "project.expire"):
            raise ValueError("系统身份仅允许内部到期清理")
        if bootstrap and (actor is not None or action != "user.roles.bootstrap"):
            raise ValueError("引导系统身份仅允许内部启动角色变更")
        if local_recovery and (actor is not None or action != "user.two_step.local_recovery" or target_type != "user"):
            raise ValueError("本机恢复身份仅用于超级管理员两步验证恢复")
        self.local_recovery = local_recovery
        self.bootstrap = bootstrap
        self.system_expiry = system_expiry
        self.include_own_project = include_own_project
        self.actor_id = actor if isinstance(actor, int) or actor is None else actor.id
        self.enabled = enabled
        self.committed = False
        self.operation_id = operation_id or str(uuid4())
        self.values = dict(action=action, target_type=target_type, target_id=target_id, project_id=project_id)
        self.summary = AuditSummary()
        self.version_ref = None
        self.tracked = []
        self.row = None

    async def __aenter__(self):
        if not self.enabled:
            return self
        try:
            # 此时业务权限和父行屏障尚未确认，所有初始化查询均不能 flush 调用方的待写实体。
            with self.db.no_autoflush:
                actor = await self.db.get(User, self.actor_id) if self.actor_id is not None else None
                from app.services.authorization_service import snapshot

                management = await snapshot(self.db, actor.id) if actor is not None else None
                self.enabled = self.system_expiry or self.bootstrap or self.local_recovery or bool(
                    management and management.is_active and management.is_management_account
                )
                if not self.enabled:
                    return self
                internal_source = "管理员引导" if self.bootstrap else "系统自动清理"
                if self.local_recovery:
                    internal_source = "部署者本机恢复"
                self.values.update(
                    actor_user_id=actor.id if actor is not None else None,
                    actor_username=actor.username if actor is not None else internal_source,
                )
                project_id = self.values.get("project_id")
                if project_id is not None:
                    project = await self.db.get(Project, project_id)
                    if project is not None:
                        if actor is not None and project.user_id == actor.id and not self.include_own_project:
                            self.enabled = False
                        else:
                            self.values.update(project_title=project.title, owner_user_id=project.user_id)
                            self.values["owner_username"] = await self.db.scalar(
                                select(User.username).where(User.id == project.user_id)
                            )
                return self
        except AppError:
            raise
        except Exception as exc:
            # __aenter__ 失败不会调用 __aexit__。不能把 SQL 或参数交给上层的 traceback logger。
            original = exc.orig if isinstance(exc, DBAPIError) else None
            sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            if sqlstate == "40001":
                # Agent 依赖 DBAPIError + SQLSTATE 处理可重试的序列化冲突；只重建无敏感数据的异常。
                failure = DBAPIError(None, None, _SafeSerializationFailure("事务序列化冲突"), hide_parameters=True)
            else:
                logger.error("管理员审计初始化失败 operation=%s", self.operation_id)
                failure = AuditOperationError("服务器内部错误")
            del original, sqlstate
        # 离开 except 后抛出，避免 __context__ 持有原始数据库异常。
        raise failure

    def target(self, obj, *, label: str | None = None):
        if not self.enabled:
            return
        if isinstance(obj, Skill) and obj.owner_user_id is not None:
            self.enabled = False
            return
        self.values["target_id"] = obj.id
        self.values["target_label"] = (
            label
            if label is not None
            else getattr(obj, "title", None) or getattr(obj, "name", None) or getattr(obj, "username", None)
        )
        if self.values["target_label"] is not None:
            self.values["target_label"] = self.values["target_label"][:200]
        if self.values["target_type"] == "chapter":
            self.values["chapter_id"] = obj.id
        elif self.values["target_type"] == "chapter_version":
            self.values["chapter_id"] = obj.chapter_id

    async def owner(self, user_id: int | None):
        if self.enabled and user_id is not None:
            self.values["owner_user_id"] = user_id
            self.values["owner_username"] = await self.db.scalar(select(User.username).where(User.id == user_id))

    def track(self, obj, fields):
        if self.enabled:
            self.tracked.append((obj, {field: deepcopy(getattr(obj, field)) for field in fields}))
            if hasattr(obj, "word_count"):
                self.summary.word_count_before = obj.word_count
            if isinstance(obj, Skill):
                self.summary.body_chars_before = len(obj.body)

    def version(self, version: ChapterVersion | None, project_id: int):
        if self.enabled and version is not None:
            self.version_ref = AuditVersionRef(
                project_id=project_id,
                chapter_id=version.chapter_id,
                version_id=version.id,
                seq=version.seq,
                content_sha256=content_hash(version.content),
            ).model_dump()

    def affected(self, ids: list[int]):
        if self.enabled:
            self.summary.affected_count = len(ids)
            self.summary.affected_ids = ids[:100]
            self.summary.ids_truncated = len(ids) > 100

    def _finalize(self):
        for obj, before in self.tracked:
            for field, old in before.items():
                value = getattr(obj, field)
                if old != value:
                    if field not in self.summary.changed_fields:
                        self.summary.changed_fields.append(field)
                    if field in SAFE_VALUE_FIELDS:
                        self.summary.changes.append(AuditValueChange(field=field, before=old, after=value))
            if hasattr(obj, "word_count"):
                self.summary.word_count_after = obj.word_count
            if isinstance(obj, Skill):
                self.summary.body_chars_after = len(obj.body)

    def _event(self, result, *, code=None):
        # 完整再校验，防止后续服务直接赋值绕过有界 JSON 白名单。
        summary = AuditSummary.model_validate(self.summary.model_dump()).model_dump(mode="json")
        if summary["settings_changes"] and self.values["action"] != "system_settings.update":
            raise ValueError("设置变更摘要仅允许用于系统设置修改")
        if self.values["action"] != "task.cancel_request" and any(
            summary[key] is not None for key in ("task_key", "task_kind", "accepted_from_status", "cancel_reason")
        ):
            raise ValueError("取消摘要仅允许用于任务取消受理")
        if summary["rbac_change"] is not None and self.values["action"] not in {
            "role.create", "role.update", "role.delete",
            "user.roles.update", "user.roles.bootstrap", "user.quotas.update",
        }:
            raise ValueError("角色配额摘要仅允许用于对应管理动作")
        if summary["backup_change"] is not None and self.values["action"] not in {
            "backup.settings.update", "backup.run.accept", "backup.download.authorize",
        }:
            raise ValueError("备份摘要仅允许用于对应备份动作")
        return AdminAuditLog(
            operation_id=self.operation_id,
            **self.values,
            result=result,
            summary=summary,
            version_ref=self.version_ref,
            error_code=code,
            error_message=ERROR_MESSAGES.get(code),
        )

    def _failure_event(self, exc: BaseException | None) -> AdminAuditLog:
        code = {400: "bad_request", 403: "forbidden", 404: "not_found", 409: "conflict"}.get(
            exc.status_code if isinstance(exc, AppError) else 500, "system_error"
        )
        if isinstance(exc, AppError) and (exc.extra or {}).get("code") in ERROR_MESSAGES:
            code = exc.extra["code"]
        # 失败不把未提交 after 值当成事实；原摘要仍供尚未结束的外层事务使用。
        summary, version_ref = self.summary, self.version_ref
        try:
            if self.values["action"] == "audit.cleanup":
                self.summary = AuditSummary(**{
                    key: getattr(summary, key) for key in ("from_at", "to_at", "high_water_id", "expected_count")
                })
            elif self.values["action"] == "task.cancel_request":
                self.summary = AuditSummary(**{
                    key: getattr(summary, key) for key in ("task_key", "task_kind", "cancel_reason")
                })
            else:
                self.summary = AuditSummary()
            self.version_ref = None
            return self._event("failure", code=code)
        finally:
            self.summary, self.version_ref = summary, version_ref

    async def commit(self, commit_fn=None, *, flush_fn=None):
        if self.enabled:
            self._finalize()
            # 业务约束先 flush，由现有业务边界翻译；审计错误不能被误译成用户名/版本冲突。
            if flush_fn is None:
                await self.db.flush()
            else:
                await flush_fn(self.db)
            self.row = self._event("success")
            self.db.add(self.row)
            await self.db.flush()
        if commit_fn is None:
            await self.db.commit()
        else:
            await commit_fn(self.db)
        scope = getattr(self.db, "_agent_audit_scope", None)
        if scope is not None and self.enabled:
            from app.services.agent.transactions import AgentAuditFailure

            # 代理的 commit 只是 flush；预览将丢弃成功，执行须等待真正的根事务提交。
            scope.coordinator.stage(AgentAuditFailure.from_event(self._failure_event(None)), mode=scope.mode)
        else:
            self.committed = True  # 必须在任何 refresh/输出投影之前设置。

    def _propagate(self, exc):
        if isinstance(exc, AppError) or not isinstance(exc, Exception):
            return False
        original = exc.orig if isinstance(exc, DBAPIError) else None
        if getattr(original, "sqlstate", None) == "40001" or getattr(original, "pgcode", None) == "40001":
            # 保留可辨识的序列化冲突，但 SQL、参数和原异常链不得传给 Agent。
            safe = DBAPIError(None, None, _SafeSerializationFailure("事务序列化冲突"), hide_parameters=True)
            try:
                raise safe from None
            except DBAPIError as sanitized:
                sanitized.__context__ = None
                raise
        # 不把原始 SQLAlchemy 异常（其中可能有正文/密码 SQL 参数）交给全局 traceback logger。
        logger.error("管理员操作发生系统错误 operation=%s committed=%s", self.operation_id, self.committed)
        try:
            raise AuditOperationError("服务器内部错误") from None
        except AuditOperationError as safe:
            safe.__context__ = None
            raise

    async def __aexit__(self, exc_type, exc, tb):
        if exc is None or not self.enabled:
            return False
        scope = getattr(self.db, "_agent_audit_scope", None)
        if scope is not None:
            from app.services.agent.transactions import AgentAuditFailure

            if isinstance(exc, UnauthorizedError):
                scope.coordinator.discard(self.operation_id)
                return False
            # 不能在 SAVEPOINT 内查询、提交或 flush 失败审计；由根事务协调器接手。
            scope.coordinator.fail(AgentAuditFailure.from_event(self._failure_event(exc)))
            return self._propagate(exc)
        if self.committed:
            return self._propagate(exc)
        try:
            await self.db.rollback()
            # commit 可能已成功但确认丢失，绝不把该事实改记失败。
            exists = await self.db.scalar(
                select(AdminAuditLog.id).where(AdminAuditLog.operation_id == self.operation_id)
            )
            if exists is None:
                self.db.add(self._failure_event(exc))
                await self.db.commit()
            else:
                await self.db.rollback()
        except Exception:
            # 数据库不可用时不能保证失败审计，且绝不递归或重新提交原业务。
            try:
                await self.db.rollback()
            except Exception:
                pass
            logger.error("管理员失败审计写入失败 operation=%s", self.operation_id)
        return self._propagate(exc)


class AuditOperationError(AppError):
    """审计边界内未知故障的固定安全响应，不携带原始数据库参数。"""

    status_code = 500
