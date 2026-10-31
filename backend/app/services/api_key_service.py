"""平台密钥的管理、独立认证与小说授权。"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import case, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from app.core.security import SecretCipher, mask_secret
from app.models import ApiKey, ApiKeyProject, Project, User
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreateOut, ApiKeyOut, ApiKeyUpdate
from app.services import project_lifecycle

INVALID_KEY = "API Key 无效或已失效"


@dataclass(frozen=True)
class ApiKeyPrincipal:
    key_id: int
    name: str
    user_id: int
    username: str
    all_projects: bool
    project_ids: tuple[int, ...]
    expires_at: datetime | None


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _to_out(key: ApiKey, project_ids: list[int]) -> ApiKeyOut:
    expiry = _utc(key.expires_at)
    status = "revoked" if key.revoked_at is not None else "expired" if expiry and expiry <= _now_utc() else "active"
    return ApiKeyOut(
        id=key.id,
        name=key.name,
        key_masked=key.key_masked,
        all_projects=key.all_projects,
        project_ids=[] if key.all_projects else project_ids,
        status=status,
        expires_at=expiry,
        revoked_at=key.revoked_at,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        updated_at=key.updated_at,
    )


async def _project_ids(db: AsyncSession, key_ids: list[int]) -> dict[int, list[int]]:
    if not key_ids:
        return {}
    rows = await db.execute(
        select(ApiKeyProject.api_key_id, ApiKeyProject.project_id)
        .join(Project, Project.id == ApiKeyProject.project_id)
        .where(ApiKeyProject.api_key_id.in_(key_ids), *project_lifecycle.active_conditions())
        .order_by(ApiKeyProject.project_id)
    )
    out: dict[int, list[int]] = {}
    for key_id, project_id in rows:
        out.setdefault(key_id, []).append(project_id)
    return out


async def _get_owned_key(db: AsyncSession, user_id: int, key_id: int, *, lock: bool = False) -> ApiKey:
    statement = select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    key = await db.scalar(statement)
    if key is None:
        raise NotFoundError("API Key 不存在")
    return key


async def _validate_projects(
    db: AsyncSession, user_id: int, all_projects: bool, project_ids: list[int]
) -> list[int]:
    if all_projects:
        return []
    ids = sorted(set(project_ids))
    if not ids:
        raise BadRequestError("请至少选择一部授权小说，或选择全部自己的小说")
    found = set(await db.scalars(select(Project.id).where(
        Project.user_id == user_id, Project.id.in_(ids), *project_lifecycle.active_conditions()
    )))
    if found != set(ids):
        raise NotFoundError("部分授权小说不存在")
    return ids


async def list_keys(db: AsyncSession, user_id: int) -> list[ApiKeyOut]:
    keys = list(await db.scalars(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
    ))
    projects = await _project_ids(db, [key.id for key in keys])
    return [_to_out(key, projects.get(key.id, [])) for key in keys]


async def create_key(
    db: AsyncSession, user_id: int, cipher: SecretCipher, data: ApiKeyCreate
) -> ApiKeyCreateOut:
    ids = await _validate_projects(db, user_id, data.all_projects, data.project_ids)
    plaintext = "anv_" + secrets.token_urlsafe(32)
    key = ApiKey(
        user_id=user_id,
        name=data.name,
        key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        key_enc=cipher.encrypt(plaintext),
        key_masked=mask_secret(plaintext),
        all_projects=data.all_projects,
        expires_at=data.expires_at,
    )
    db.add(key)
    try:
        await db.flush()
        db.add_all(ApiKeyProject(api_key_id=key.id, project_id=project_id) for project_id in ids)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("创建密钥时授权小说发生变化或出现冲突，请刷新后重试") from None
    await db.refresh(key)
    return ApiKeyCreateOut(**_to_out(key, ids).model_dump(), key=plaintext)


async def update_key(db: AsyncSession, user_id: int, key_id: int, data: ApiKeyUpdate) -> ApiKeyOut:
    key = await _get_owned_key(db, user_id, key_id, lock=True)
    if key.revoked_at is not None:
        raise BadRequestError("API Key 已撤销，不能再修改")
    fields = data.model_fields_set
    ids = (await _project_ids(db, [key.id])).get(key.id, [])
    all_projects = key.all_projects if "all_projects" not in fields else bool(data.all_projects)
    scope_changed = bool(fields & {"all_projects", "project_ids"})
    if scope_changed:
        selected = ids if "project_ids" not in fields else list(data.project_ids or [])
        ids = await _validate_projects(db, user_id, all_projects, selected)
    values = data.model_dump(exclude_unset=True, exclude={"project_ids"})
    if values or scope_changed:
        values["updated_at"] = _now_utc()
        result = await db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise BadRequestError("API Key 已撤销，不能再修改")
        if scope_changed:
            await db.execute(delete(ApiKeyProject).where(ApiKeyProject.api_key_id == key_id))
            db.add_all(ApiKeyProject(api_key_id=key_id, project_id=project_id) for project_id in ids)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise ConflictError("授权小说发生变化，请刷新后重试") from None
    await db.refresh(key)
    return _to_out(key, ids)


async def reveal_key(db: AsyncSession, user_id: int, key_id: int, cipher: SecretCipher) -> str:
    key = await _get_owned_key(db, user_id, key_id, lock=True)
    if key.revoked_at is not None or key.key_enc is None:
        raise BadRequestError("API Key 已撤销，不能再查看")
    try:
        return cipher.decrypt(key.key_enc)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from None


async def revoke_key(db: AsyncSession, user_id: int, key_id: int) -> None:
    await _get_owned_key(db, user_id, key_id)
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=_now_utc(), key_enc=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


def _valid_key_conditions(now: datetime):
    return (
        ApiKey.revoked_at.is_(None),
        or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        ApiKey.user_id.in_(select(User.id).where(User.is_active.is_(True))),
    )


async def authenticate_key(db: AsyncSession, raw_key: str | None, limit_per_minute: int) -> ApiKeyPrincipal:
    """原子认证和扣减额度，短事务提交后才进入业务读取。"""
    if not raw_key or len(raw_key) != 47 or not raw_key.startswith("anv_"):
        raise UnauthorizedError(INVALID_KEY)
    digest = hashlib.sha256(raw_key.encode()).hexdigest()
    now = _now_utc()
    window = int(now.timestamp()) // 60
    later_window = ApiKey.rate_window < window
    result = await db.execute(
        update(ApiKey)
        .where(
            ApiKey.key_hash == digest,
            *_valid_key_conditions(now),
            or_(later_window, ApiKey.rate_count < limit_per_minute),
        )
        .values(
            # 迟到的旧分钟请求扣减当前已存窗口，不能把新窗口倒退后重新获得额度。
            rate_count=case((later_window, 1), else_=ApiKey.rate_count + 1),
            rate_window=case((later_window, window), else_=ApiKey.rate_window),
            last_used_at=case(
                (or_(ApiKey.last_used_at.is_(None), ApiKey.last_used_at < now), now),
                else_=ApiKey.last_used_at,
            ),
            # 调用统计不表示用户编辑了密钥配置。
            updated_at=ApiKey.updated_at,
        )
        .returning(ApiKey.id, ApiKey.name, ApiKey.user_id, ApiKey.all_projects, ApiKey.expires_at)
        .execution_options(synchronize_session=False)
    )
    row = result.one_or_none()
    await db.commit()
    checked_at = _now_utc()
    if row is None:
        limited_window = await db.scalar(
            select(ApiKey.rate_window).where(ApiKey.key_hash == digest, *_valid_key_conditions(checked_at))
        )
        if limited_window is None:
            raise UnauthorizedError(INVALID_KEY)
        raise HTTPException(
            status_code=429,
            detail="API Key 请求过于频繁，请稍后重试",
            headers={"Retry-After": str(max(1, (limited_window + 1) * 60 - int(checked_at.timestamp())))},
        )
    expiry = _utc(row.expires_at)
    # 更新可能等待数据库写锁；返回主体前重新检查是否已越过到期时间。
    if expiry is not None and expiry <= checked_at:
        raise UnauthorizedError(INVALID_KEY)
    username = await db.scalar(select(User.username).where(User.id == row.user_id, User.is_active.is_(True)))
    if username is None:
        raise UnauthorizedError(INVALID_KEY)
    ids = (await _project_ids(db, [row.id])).get(row.id, []) if not row.all_projects else []
    return ApiKeyPrincipal(
        key_id=row.id,
        name=row.name,
        user_id=row.user_id,
        username=username,
        all_projects=row.all_projects,
        project_ids=tuple(ids),
        expires_at=expiry,
    )


async def get_authorized_project(db: AsyncSession, principal: ApiKeyPrincipal, project_id: int) -> Project:
    project = await db.scalar(select(Project).where(
        Project.id == project_id, Project.user_id == principal.user_id, *project_lifecycle.active_conditions()
    ))
    if project is None or (not principal.all_projects and project_id not in principal.project_ids):
        raise NotFoundError("小说不存在")
    return project
