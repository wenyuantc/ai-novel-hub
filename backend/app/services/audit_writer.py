"""明确接入的管理写边界；不监听 ORM、不替换 session、不采集请求。

调用方必须通过 commit() 提交，再进行 refresh/响应投影。失败路径先整体回滚，
仅用提前保存的 primitive 快照开启新事务；同一 operation_id 不会覆盖已提交结果。
"""

import hashlib
import logging
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
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


class AuditOperation:
    def __init__(
        self,
        db: AsyncSession,
        actor: User | int,
        action: AuditAction,
        target_type: AuditTargetType,
        target_id: int | None = None,
        *,
        project_id: int | None = None,
        enabled: bool = True,
        operation_id: str | None = None,
    ):
        self.db = db
        self.actor_id = actor if isinstance(actor, int) else actor.id
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
        actor = await self.db.get(User, self.actor_id)
        self.enabled = bool(actor and actor.is_admin and actor.is_active)
        if not self.enabled:
            return self
        self.values.update(actor_user_id=actor.id, actor_username=actor.username)
        project_id = self.values.get("project_id")
        if project_id is not None:
            # 仅已核实启用管理员可读这些项目；普通用户不做额外查询。
            project = await self.db.get(Project, project_id)
            if project is not None:
                if project.user_id == actor.id:
                    self.enabled = False
                else:
                    self.values.update(project_title=project.title, owner_user_id=project.user_id)
                    self.values["owner_username"] = await self.db.scalar(
                        select(User.username).where(User.id == project.user_id)
                    )
        return self

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
        return AdminAuditLog(
            operation_id=self.operation_id,
            **self.values,
            result=result,
            summary=summary,
            version_ref=self.version_ref,
            error_code=code,
            error_message=ERROR_MESSAGES.get(code),
        )

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
        self.committed = True  # 必须在任何 refresh/输出投影之前设置。

    def _propagate(self, exc):
        if isinstance(exc, AppError):
            return False
        # 不把原始 SQLAlchemy 异常（其中可能有正文/密码 SQL 参数）交给全局 traceback logger。
        logger.error("管理员操作发生系统错误 operation=%s committed=%s", self.operation_id, self.committed)
        raise AuditOperationError("服务器内部错误") from None

    async def __aexit__(self, exc_type, exc, tb):
        if exc is None or not self.enabled:
            return False
        if self.committed:
            return self._propagate(exc)
        try:
            await self.db.rollback()
            # commit 可能已成功但确认丢失，绝不把该事实改记失败。
            exists = await self.db.scalar(
                select(AdminAuditLog.id).where(AdminAuditLog.operation_id == self.operation_id)
            )
            if exists is None:
                code = {400: "bad_request", 403: "forbidden", 404: "not_found", 409: "conflict"}.get(
                    exc.status_code if isinstance(exc, AppError) else 500, "system_error"
                )
                if isinstance(exc, AppError) and (exc.extra or {}).get("code") in ERROR_MESSAGES:
                    code = exc.extra["code"]
                # 失败不把未提交 after 值当成事实；清理只保留已校验的范围和计数。
                if self.values["action"] == "audit.cleanup":
                    self.summary = AuditSummary(
                        **{
                            key: getattr(self.summary, key)
                            for key in ("from_at", "to_at", "high_water_id", "expected_count")
                        }
                    )
                else:
                    self.summary = AuditSummary()
                self.version_ref = None
                self.db.add(self._event("failure", code=code))
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
