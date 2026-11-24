"""项目导入预览的限量加密暂存与单次确认。"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import DateTime, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, BadRequestError
from app.models.project_import_preview import (
    ProjectImportPreview,
    ProjectImportPreviewChunk,
    ProjectImportPreviewQuota,
)
from app.services import authorization_service as authz

logger = logging.getLogger(__name__)
MAX_BYTES = 100 * 1024 * 1024
TOO_LARGE = "备份文件过大，上限为 100MB"
TOKEN_TTL = 600
MAX_TOKENS_PER_USER = 2
MAX_TOKENS_TOTAL = 6
TOKEN_EXPIRED = "导入凭据已过期，请重新选择备份文件"
_TOKEN_RANDOM_BYTES = 24
_NONCE_BYTES = 12
_TAG_BYTES = 16
_CHUNK_BYTES = 1024 * 1024
_CHUNK_BATCH = 8
_CLEANUP_INTERVAL = 60


class PreviewStorageError(AppError):
    status_code = 503

    def __init__(self):
        super().__init__("导入预览暂时不可用，请稍后重试")


@dataclass(frozen=True, slots=True)
class PreviewIdentity:
    token_hash: str
    user_id: int
    revision: int
    size_bytes: int

    def aad(self, index: int) -> bytes:
        return f"{self.token_hash}:{self.user_id}:{self.revision}:{self.size_bytes}:{index}".encode("ascii")


async def _now(db: AsyncSession) -> datetime:
    # clock_timestamp 在等待写入队列后仍反映当前时刻。SQLite 的时间值以 UTC 保存。
    clock = (func.clock_timestamp(type_=DateTime(timezone=True))
             if db.get_bind().dialect.name == "postgresql" else func.current_timestamp())
    now = await db.scalar(select(clock))
    if now is None:
        raise PreviewStorageError()
    return now


def _cipher(encryption_key: str) -> AESGCM:
    key = hmac.digest(base64.urlsafe_b64decode(encryption_key), b"ainovel/project-import-preview/v1", "sha256")
    return AESGCM(key)


async def _lock_quota(db: AsyncSession) -> int:
    dialect_insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    table = ProjectImportPreviewQuota
    revision = await db.scalar(
        dialect_insert(table).values(id=1, revision=1)
        .on_conflict_do_update(index_elements=[table.id], set_={"revision": table.revision + 1})
        .returning(table.revision)
    )
    if revision is None:
        raise PreviewStorageError()
    return revision


async def _purge_expired(db: AsyncSession, now: datetime) -> None:
    await db.execute(delete(ProjectImportPreview).where(ProjectImportPreview.expires_at <= now))


async def _evict(db: AsyncSession, user_id: int) -> None:
    preview = ProjectImportPreview
    mine = select(preview.token_hash).order_by(preview.expires_at.desc(), preview.revision.desc()).where(
        preview.user_id == user_id,
    ).offset(MAX_TOKENS_PER_USER - 1)
    await db.execute(delete(preview).where(preview.token_hash.in_(mine)))
    oldest = select(preview.token_hash).order_by(preview.expires_at.desc(), preview.revision.desc()).offset(
        MAX_TOKENS_TOTAL - 1,
    )
    await db.execute(delete(preview).where(preview.token_hash.in_(oldest)))


async def store(db: AsyncSession, user_id: int, raw: bytes, encryption_key: str) -> str:
    """调用方已结束预览读事务。新条目与淘汰和分块在同一个事务生效。"""
    if not 0 < len(raw) <= MAX_BYTES:
        raise BadRequestError(TOO_LARGE)
    token = secrets.token_urlsafe(_TOKEN_RANDOM_BYTES)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    cipher = _cipher(encryption_key)
    try:
        async with db.begin():
            # 与确认、用户删除遵守 User → 业务资源的锁序。
            await authz.lock_users(db, user_id, missing_ok=True)
            await authz.require_authenticated(db, user_id)
            revision = await _lock_quota(db)
            now = await _now(db)
            await _purge_expired(db, now)
            await _evict(db, user_id)
            identity = PreviewIdentity(token_hash, user_id, revision, len(raw))
            expiry = timedelta(seconds=TOKEN_TTL)
            await db.execute(insert(ProjectImportPreview).values(
                token_hash=token_hash, user_id=user_id, revision=revision,
                size_bytes=identity.size_bytes, expires_at=now + expiry,
            ))
            pending = []
            with memoryview(raw) as body:
                for index, offset in enumerate(range(0, identity.size_bytes, _CHUNK_BYTES)):
                    nonce = secrets.token_bytes(_NONCE_BYTES)
                    chunk = body[offset:offset + _CHUNK_BYTES]
                    ciphertext = nonce + cipher.encrypt(nonce, chunk, identity.aad(index))
                    pending.append({"token_hash": token_hash, "chunk_index": index, "ciphertext": ciphertext})
                    if len(pending) == _CHUNK_BATCH:
                        await db.execute(insert(ProjectImportPreviewChunk), pending)
                        pending.clear()
                if pending:
                    await db.execute(insert(ProjectImportPreviewChunk), pending)
            # 大文件的传输时间从 600 秒确认窗口中排除。
            await db.execute(update(ProjectImportPreview).where(ProjectImportPreview.token_hash == token_hash)
                             .values(expires_at=await _now(db) + expiry))
    except SQLAlchemyError:
        raise PreviewStorageError() from None
    return token


def _matching(identity: PreviewIdentity, now: datetime):
    preview = ProjectImportPreview
    return (
        preview.token_hash == identity.token_hash, preview.user_id == identity.user_id,
        preview.revision == identity.revision, preview.size_bytes == identity.size_bytes, preview.expires_at > now,
    )


async def load(db: AsyncSession, user_id: int, token: str, encryption_key: str) -> tuple[PreviewIdentity, bytearray]:
    """读取身份与有界明文。调用方在解析前结束读事务，导入提交前再核实身份。"""
    if len(token) != _TOKEN_RANDOM_BYTES * 4 // 3 or not token.isascii():
        raise BadRequestError(TOKEN_EXPIRED)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    try:
        now = await _now(db)
        preview = await db.scalar(select(ProjectImportPreview).where(
            ProjectImportPreview.token_hash == token_hash,
            ProjectImportPreview.user_id == user_id,
            ProjectImportPreview.expires_at > now,
        ))
        if preview is None:
            raise BadRequestError(TOKEN_EXPIRED)
        identity = PreviewIdentity(token_hash, user_id, preview.revision, preview.size_bytes)
        if not 0 < identity.size_bytes <= MAX_BYTES:
            raise BadRequestError(TOKEN_EXPIRED)
        raw = await _read_chunks(db, identity, now, _cipher(encryption_key))
    except SQLAlchemyError:
        raise PreviewStorageError() from None
    return identity, raw


async def _read_chunks(db: AsyncSession, identity: PreviewIdentity, now: datetime, cipher: AESGCM) -> bytearray:
    chunk = ProjectImportPreviewChunk
    count = (identity.size_bytes + _CHUNK_BYTES - 1) // _CHUNK_BYTES
    statement = (
        select(chunk.chunk_index, chunk.ciphertext).join(ProjectImportPreview)
        .where(
            *_matching(identity, now), chunk.chunk_index < count,
            func.length(chunk.ciphertext) <= _CHUNK_BYTES + _NONCE_BYTES + _TAG_BYTES,
        )
        .order_by(chunk.chunk_index)
        .limit(_CHUNK_BATCH)
    )
    raw = bytearray()
    try:
        for start in range(0, count, _CHUNK_BATCH):
            # 硬限制每次从驱动读取的行数，避开 asyncpg 流式游标会额外预取行的问题。
            rows = (await db.execute(statement.where(chunk.chunk_index >= start))).all()
            if len(rows) != min(count - start, _CHUNK_BATCH):
                raise BadRequestError(TOKEN_EXPIRED)
            for expected_index, (index, blob) in enumerate(rows, start):
                expected_size = min(identity.size_bytes - len(raw), _CHUNK_BYTES)
                if index != expected_index or len(blob) != expected_size + _NONCE_BYTES + _TAG_BYTES:
                    raise BadRequestError(TOKEN_EXPIRED)
                raw.extend(cipher.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], identity.aad(index)))
    except InvalidTag:
        raise BadRequestError(TOKEN_EXPIRED) from None
    if len(raw) != identity.size_bytes:
        raise BadRequestError(TOKEN_EXPIRED)
    return raw


async def require_available(db: AsyncSession, identity: PreviewIdentity) -> None:
    try:
        found = await db.scalar(select(ProjectImportPreview.token_hash).where(*_matching(identity, await _now(db))))
    except SQLAlchemyError:
        raise PreviewStorageError() from None
    if found is None:
        raise BadRequestError(TOKEN_EXPIRED)


async def consume(db: AsyncSession, identity: PreviewIdentity) -> None:
    """和新作品同一次提交。事务失败时删除随之回滚。"""
    try:
        result = await db.execute(delete(ProjectImportPreview).where(*_matching(identity, await _now(db))))
    except SQLAlchemyError:
        raise PreviewStorageError() from None
    if result.rowcount != 1:
        raise BadRequestError(TOKEN_EXPIRED)


async def _cleanup(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    try:
        async with sessionmaker() as probe:
            if await probe.scalar(select(ProjectImportPreview.token_hash).where(
                ProjectImportPreview.expires_at <= await _now(probe),
            ).limit(1)) is None:
                return
        async with sessionmaker.begin() as db:
            await _lock_quota(db)
            await _purge_expired(db, await _now(db))
    except (SQLAlchemyError, PreviewStorageError):
        logger.warning("项目导入预览清理暂不可用")


@asynccontextmanager
async def preview_maintenance(sessionmaker: async_sessionmaker[AsyncSession]):
    """启动及后续低频清理未被确认或淘汰的过期分块。"""
    stop = asyncio.Event()
    await _cleanup(sessionmaker)

    async def run() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_CLEANUP_INTERVAL)
            except TimeoutError:
                await _cleanup(sessionmaker)

    task = asyncio.create_task(run(), name="project-import-preview-cleanup")
    try:
        yield
    finally:
        stop.set()
        await task
