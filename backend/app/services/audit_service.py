"""管理员审计查询与受保护日期清理。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
from pydantic import ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models import AdminAuditLog, Chapter, ChapterVersion, User
from app.schemas.audit import (
    AuditCleanupOut,
    AuditCleanupPreview,
    AuditCleanupRequest,
    AuditDateRange,
    AuditLogBrief,
    AuditLogDetail,
    AuditLogFilters,
    AuditLogList,
    AuditSummary,
    AuditVersionRef,
)
from app.services.audit_writer import AuditOperation, content_hash

PREVIEW_PURPOSE = "admin_audit_cleanup"
PREVIEW_TTL = timedelta(minutes=10)


class _PreviewClaims(AuditDateRange):
    model_config = ConfigDict(extra="forbid")
    purpose: str
    actor_user_id: StrictInt = Field(gt=0)
    high_water_id: StrictInt = Field(ge=0)
    count: StrictInt = Field(ge=0)
    nonce: UUID
    iat: StrictInt
    exp: StrictInt


def _dates(dates: AuditDateRange):
    return [AdminAuditLog.occurred_at >= dates.from_at, AdminAuditLog.occurred_at < dates.to_at]


def _brief(row: AdminAuditLog) -> AuditLogBrief:
    return AuditLogBrief(**{name: getattr(row, name) for name in AuditLogBrief.model_fields})


async def list_logs(db: AsyncSession, filters: AuditLogFilters) -> AuditLogList:
    conditions = []
    for field in ("actor_user_id", "target_type", "target_id", "action", "result"):
        value = getattr(filters, field)
        if value is not None:
            conditions.append(getattr(AdminAuditLog, field) == value)
    if filters.actor_q:
        value = filters.actor_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(AdminAuditLog.actor_username.ilike(f"%{value}%", escape="\\"))
    if filters.from_at is not None:
        conditions.extend(_dates(AuditDateRange(from_at=filters.from_at, to_at=filters.to_at)))
    total = int(await db.scalar(select(func.count(AdminAuditLog.id)).where(*conditions)) or 0)
    rows = await db.scalars(
        select(AdminAuditLog)
        .where(*conditions)
        .order_by(AdminAuditLog.occurred_at.desc(), AdminAuditLog.id.desc())
        .offset((filters.page - 1) * filters.page_size)
        .limit(filters.page_size)
    )
    return AuditLogList(items=[_brief(row) for row in rows], total=total)


async def get_log(db: AsyncSession, log_id: int) -> AuditLogDetail:
    row = await db.get(AdminAuditLog, log_id)
    if row is None:
        raise NotFoundError("审计记录不存在或已被清理")
    ref = AuditVersionRef.model_validate(row.version_ref) if row.version_ref else None
    status = None
    if ref is not None:
        version = await db.scalar(
            select(ChapterVersion)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(
                ChapterVersion.id == ref.version_id,
                Chapter.id == ref.chapter_id,
                Chapter.project_id == ref.project_id,
            )
        )
        status = (
            "missing"
            if version is None
            else (
                "available"
                if version.seq == ref.seq and content_hash(version.content) == ref.content_sha256
                else "changed"
            )
        )
    return AuditLogDetail(
        **_brief(row).model_dump(),
        summary=AuditSummary.model_validate(row.summary),
        version_ref=ref,
        version_status=status,
    )


async def preview_cleanup(db: AsyncSession, actor: User, dates: AuditDateRange, secret: str) -> AuditCleanupPreview:
    high_water = int(await db.scalar(select(func.max(AdminAuditLog.id))) or 0)
    count = int(
        await db.scalar(
            select(func.count(AdminAuditLog.id)).where(
                *_dates(dates),
                AdminAuditLog.id <= high_water,
                AdminAuditLog.action != "audit.cleanup",
            )
        )
        or 0
    )
    protected = int(
        await db.scalar(
            select(func.count(AdminAuditLog.id)).where(
                *_dates(dates),
                AdminAuditLog.id <= high_water,
                AdminAuditLog.action == "audit.cleanup",
            )
        )
        or 0
    )
    now = datetime.now(UTC)
    expiry = now + PREVIEW_TTL
    claims = _PreviewClaims(
        **dates.model_dump(),
        purpose=PREVIEW_PURPOSE,
        actor_user_id=actor.id,
        high_water_id=high_water,
        count=count,
        nonce=uuid4(),
        iat=int(now.timestamp()),
        exp=int(expiry.timestamp()),
    )
    token = jwt.encode(claims.model_dump(mode="json"), secret, algorithm="HS256")
    return AuditCleanupPreview(
        **dates.model_dump(),
        count=count,
        protected_count=protected,
        expires_at=expiry,
        preview_token=token,
    )


def _decode_preview(token: str, secret: str) -> _PreviewClaims:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "iat"]})
        claims = _PreviewClaims.model_validate(payload)
        if claims.purpose != PREVIEW_PURPOSE or claims.exp - claims.iat != int(PREVIEW_TTL.total_seconds()):
            raise ValueError("purpose")
        return claims
    except (jwt.PyJWTError, ValidationError, ValueError) as exc:
        raise BadRequestError("清理预览无效或已过期，请重新预览", extra={"code": "invalid_preview"}) from exc


async def cleanup(db: AsyncSession, actor: User, req: AuditCleanupRequest, secret: str) -> AuditCleanupOut:
    async with AuditOperation(db, actor, "audit.cleanup", "audit") as audit:
        if not audit.enabled:
            raise ForbiddenError("需要管理员权限")
        claims = _decode_preview(req.preview_token, secret)
        if claims.actor_user_id != actor.id:
            raise ForbiddenError("清理预览与操作人不匹配", extra={"code": "preview_actor_mismatch"})
        audit.operation_id = str(claims.nonce)
        exists = await db.scalar(select(AdminAuditLog.id).where(AdminAuditLog.operation_id == audit.operation_id))
        if exists is not None:
            raise ConflictError("清理预览已使用，请重新预览", extra={"code": "preview_replayed"})
        audit.summary = AuditSummary(
            from_at=claims.from_at,
            to_at=claims.to_at,
            high_water_id=claims.high_water_id,
            expected_count=claims.count,
        )
        # 先占 nonce，数据库唯一约束使同令牌并发请求串行并在删除之前失败。
        row = audit._event("success")
        db.add(row)
        try:
            await db.flush()
        except IntegrityError as exc:
            original = exc.orig
            duplicate = str(original) == "UNIQUE constraint failed: admin_audit_logs.operation_id" or any(
                getattr(error, "sqlstate", None) == "23505"
                and getattr(error, "constraint_name", None) == "uq_admin_audit_logs_operation_id"
                for error in (original, getattr(original, "__cause__", None))
            )
            if not duplicate:
                raise
            raise ConflictError("清理预览已使用，请重新预览", extra={"code": "preview_replayed"}) from exc
        deleted = await db.execute(
            delete(AdminAuditLog).where(
                *_dates(claims),
                AdminAuditLog.id <= claims.high_water_id,
                AdminAuditLog.action != "audit.cleanup",
            )
        )
        count = deleted.rowcount
        audit.summary.deleted_count = count
        row.summary = audit.summary.model_dump(mode="json")
        await db.commit()
        audit.committed = True
        return AuditCleanupOut(deleted_count=count, audit_log_id=row.id)
