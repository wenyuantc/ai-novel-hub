"""系列共享设定图片：固定原系列身份、来源及本人图片模型。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.prompts.registry import SETTING_THUMBNAIL_KEY
from app.ai.prompts.setting_thumbnail import build_thumbnail_prompt
from app.ai.providers import AIProviderError, ProviderFactory
from app.ai.providers.dispatch_hooks import DispatchHooks, use_dispatch_hooks
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import AIConfig, Series
from app.schemas.series import SeriesAiOut, SeriesCharacterData, SeriesWorldData
from app.schemas.setting_image import (
    SeriesThumbnailResult,
    ThumbnailAiApplyRequest,
    ThumbnailAiGenerateOut,
    ThumbnailAiGenerateRequest,
    ThumbnailAiPrompt,
)
from app.services import (
    ai_config_service,
    ai_dispatch,
    collaboration_ai_guard,
    project_ai_config,
    prompt_service,
    series_service,
    setting_image_codec,
    setting_image_service,
    thumbnail_ai_tokens,
)
from app.services import (
    authorization_service as authz,
)

_PROMPT_PURPOSE = "series-thumbnail-prompt"
_PREVIEW_PURPOSE = "series-thumbnail-preview"


@dataclass(frozen=True, slots=True, repr=False)
class _Config:
    id: int
    instance_key: str
    user_id: int
    model_type: str
    provider: str
    base_url: str | None
    api_key_enc: str
    model: str
    thinking_level: str
    context_window_k: int | None
    max_output_k: int | None
    fingerprint: str


@dataclass(frozen=True, slots=True, repr=False)
class _Scope:
    token: dict
    fields: list[tuple[str, str]]


def _changed() -> ConflictError:
    return ConflictError("系列设定、授权或模型已变化，请刷新后重新生成", extra={"code": "thumbnail_ai_changed"})


async def _identity(db: AsyncSession, actor_id: int) -> authz.AuthenticatedActor:
    actor = authz.actor_snapshot(db, actor_id)
    if isinstance(actor, authz.AuthenticatedActor):
        return actor
    current = await authz.require_authenticated(db, actor_id)
    return authz.AuthenticatedActor(current.id, current.token_version)


def _source_stamp(entry) -> str:
    raw = json.dumps(
        {"kind": entry.kind, "data": entry.data, "ai_settings": entry.ai_settings},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _fields(entry) -> list[tuple[str, str]]:
    policy = SeriesAiOut.model_validate(entry.ai_settings)
    if policy.ai_mode == "never":
        raise BadRequestError("该设定未允许用于 AI", extra={"code": "setting_not_available"})
    hidden = set(policy.ai_hidden_builtin_fields)
    if entry.kind == "character":
        data = SeriesCharacterData.model_validate(entry.data)
        values = (
            ("name", "名称", data.name), ("aliases", "别名", "、".join(data.aliases)),
            ("gender", "性别", data.gender), ("profile", "简介", data.profile),
            ("appearance", "外貌", data.appearance), ("personality", "性格", data.personality),
            ("background", "背景", data.background),
        )
    elif entry.kind == "world":
        data = SeriesWorldData.model_validate(entry.data)
        values = (
            ("title", "名称", data.title), ("aliases", "别名", "、".join(data.aliases)),
            ("category", "类型", data.category), ("content", "内容", data.content),
        )
    else:
        raise NotFoundError("共享设定不存在")
    return [(label, value) for key, label, value in values if key not in hidden and value]


async def _lock_series(db: AsyncSession, actor: authz.AuthenticatedActor, sid: int) -> Series:
    """与系列写入和撤权竞争同一父行；调用方持锁直到会话关闭。"""
    sqlite = db.get_bind().dialect.name == "sqlite"
    previous = int(await db.scalar(text("PRAGMA busy_timeout")) or 0) if sqlite else None
    try:
        if sqlite:
            await db.execute(text("PRAGMA busy_timeout = 2000"))
        else:
            await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        await authz.require_authenticated_for_send(db, actor)
        if sqlite:
            changed = await db.execute(
                update(Series).where(Series.id == sid)
                .values(revision=Series.revision, updated_at=Series.updated_at)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise NotFoundError("系列不存在")
        stmt = select(Series).where(Series.id == sid).execution_options(populate_existing=True)
        if not sqlite:
            stmt = stmt.with_for_update(read=True)
        parent = await db.scalar(stmt)
        if parent is None:
            raise NotFoundError("系列不存在")
        return parent
    except OperationalError as exc:
        if (sqlite and ai_dispatch.is_sqlite_busy(exc)) or getattr(exc.orig, "sqlstate", None) == "55P03":
            raise ai_dispatch.VisibilityBusyError from None
        raise
    finally:
        if previous is not None and db.in_transaction() and db.is_active:
            await db.execute(text(f"PRAGMA busy_timeout = {previous}"))


async def _scope(db: AsyncSession, actor: authz.AuthenticatedActor, sid: int, eid: int) -> _Scope:
    parent = await _lock_series(db, actor, sid)
    # _readable 检查 accepted editor 或作者；仅管理权限不构成系列授权。
    await series_service._readable(db, actor.id, sid)
    grant = await series_service._current_authorization_version(db, parent, actor.id)
    entry = await series_service._entry(db, sid, eid)
    fields = _fields(entry)
    return _Scope(token={
        "version": 1, "actor_id": actor.id, "token_version": actor.token_version,
        "series_id": sid, "owner_id": parent.owner_id, "revision": parent.revision,
        "entry_id": eid, "kind": entry.kind, "grant": grant, "source": _source_stamp(entry),
    }, fields=fields)


async def _verify_scope(
    db: AsyncSession, actor: authz.AuthenticatedActor, sid: int, eid: int, expected: dict,
) -> _Scope:
    try:
        current = await _scope(db, actor, sid, eid)
    except BadRequestError as exc:
        if (exc.extra or {}).get("code") == "setting_not_available":
            raise _changed() from None
        raise
    if current.token != expected:
        raise _changed()
    return current


async def _read_config(db: AsyncSession, actor_id: int, config_id: int | None) -> _Config:
    await ai_config_service.lock_config_owner(db, actor_id, write=False)
    cfg = await ai_config_service.resolve_config(db, actor_id, config_id, model_type="image")
    if not project_ai_config._instance_key_valid(cfg.instance_key):
        raise _changed()
    return _Config(
        id=cfg.id, instance_key=cfg.instance_key, user_id=cfg.user_id, model_type=cfg.model_type,
        provider=cfg.provider, base_url=cfg.base_url, api_key_enc=cfg.api_key_enc, model=cfg.model,
        thinking_level=cfg.thinking_level, context_window_k=cfg.context_window_k, max_output_k=cfg.max_output_k,
        fingerprint=project_ai_config._fingerprint(cfg),
    )


def _config_proof(cfg: _Config) -> dict:
    return {"id": cfg.id, "instance_key": cfg.instance_key, "fingerprint": cfg.fingerprint}


async def _verify_config(db: AsyncSession, actor_id: int, expected: dict) -> None:
    await ai_config_service.lock_config_owner(db, actor_id, write=False)
    cfg = await db.scalar(select(AIConfig).where(
        AIConfig.id == expected.get("id"), AIConfig.user_id == actor_id, AIConfig.model_type == "image",
    ).execution_options(populate_existing=True))
    if (cfg is None or cfg.instance_key != expected.get("instance_key")
            or project_ai_config._fingerprint(cfg) != expected.get("fingerprint")):
        raise _changed()


async def _verify(
    db: AsyncSession, actor: authz.AuthenticatedActor, sid: int, eid: int, expected: dict, config: dict,
) -> None:
    await _verify_scope(db, actor, sid, eid, expected)
    await _verify_config(db, actor.id, config)


@asynccontextmanager
async def _send_lease(
    maker: async_sessionmaker[AsyncSession], actor: authz.AuthenticatedActor, sid: int, eid: int,
    scope: dict, config: dict,
) -> AsyncIterator[None]:
    db = maker()
    try:
        await _verify(db, actor, sid, eid, scope, config)
        yield
    finally:
        # 即使重复取消也等待事务关闭，不能让锁遗留在后台。
        await ai_dispatch._close_send_session(db)


async def _version(db: AsyncSession, sid: int, eid: int, expected: str) -> None:
    image = await setting_image_service._series_image(db, sid, eid)
    setting_image_service._check_version(image.id if image else None, expected)


async def default_prompt(
    db: AsyncSession, actor_id: int, sid: int, eid: int, cipher: SecretCipher,
) -> ThumbnailAiPrompt:
    actor = await _identity(db, actor_id)
    scope = await _scope(db, actor, sid, eid)
    override = await prompt_service.get_override(db, actor_id, SETTING_THUMBNAIL_KEY)
    return ThumbnailAiPrompt(
        prompt=build_thumbnail_prompt(scope.token["kind"], scope.fields, override=override),
        prompt_token=thumbnail_ai_tokens.issue_token(cipher, _PROMPT_PURPOSE, scope.token),
    )


async def generate(
    db: AsyncSession, actor_id: int, sid: int, eid: int, body: ThumbnailAiGenerateRequest,
    cipher: SecretCipher, factory: ProviderFactory, *, ai_timeout: float, allow_private: bool,
) -> ThumbnailAiGenerateOut:
    actor = await _identity(db, actor_id)
    expected = thumbnail_ai_tokens.read_token(cipher, _PROMPT_PURPOSE, body.prompt_token)
    scope = await _verify_scope(db, actor, sid, eid, expected)
    if body.expected_revision != scope.token["revision"]:
        raise series_service._stale(scope.token["revision"])
    await _version(db, sid, eid, body.expected_version)
    cfg = await _read_config(db, actor.id, body.config_id)
    config_proof = _config_proof(cfg)
    maker = ai_dispatch.visibility_sessionmaker(db)
    await db.rollback()
    # 不可信工厂可在构造时发送，不能只相信其自称支持 hooks。
    if not collaboration_ai_guard._trusted_factory(factory):
        raise collaboration_ai_guard.CollaborationSendBlocked()
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    if not collaboration_ai_guard._trusted_provider(factory, provider, cfg.provider):
        raise collaboration_ai_guard.CollaborationSendBlocked()
    bound = collaboration_ai_guard._BoundModelProvider(provider, cfg.model, "image")
    hooks = DispatchHooks(lambda: _send_lease(maker, actor, sid, eid, expected, config_proof))
    try:
        with use_dispatch_hooks(hooks):
            async with asyncio.timeout(ai_timeout):
                image = await bound.generate_image(body.prompt, cfg.model)
    except TimeoutError:
        raise BadRequestError("图片生成超时，请稍后重试") from None
    except AIProviderError as exc:
        # SDK 可能包装受控发送错误；任何第三方原文都不向用户返回。
        original: BaseException | None = exc
        seen: set[int] = set()
        while original is not None and id(original) not in seen:
            seen.add(id(original))
            if isinstance(original, AppError):
                raise original from None
            original = original.__cause__ or original.__context__
        raise BadRequestError("图片模型调用失败，请稍后重试") from None
    finally:
        await ai_dispatch._wait_send_cleanup(hooks.aclose())
    normalized = await setting_image_codec.normalize_image_async(image.data, image.mime_type)
    # 持同一短事务到 ASGI 输出结束，旧邀请与结果不能跨过撤权返回。
    await _verify(db, actor, sid, eid, expected, config_proof)
    await _version(db, sid, eid, body.expected_version)
    proof = {
        "scope": expected, "config": config_proof, "expected_version": body.expected_version,
        "sha256": normalized.sha256, "size": len(normalized.data),
    }
    return ThumbnailAiGenerateOut(
        image_base64=base64.b64encode(normalized.data).decode("ascii"), model_source="actor",
        preview_token=thumbnail_ai_tokens.issue_token(cipher, _PREVIEW_PURPOSE, proof),
    )


async def apply(
    db: AsyncSession, actor_id: int, sid: int, eid: int, body: ThumbnailAiApplyRequest, cipher: SecretCipher,
) -> SeriesThumbnailResult:
    actor = await _identity(db, actor_id)
    proof = thumbnail_ai_tokens.read_token(cipher, _PREVIEW_PURPOSE, body.preview_token)
    scope, config = proof.get("scope"), proof.get("config")
    if not isinstance(scope, dict) or not isinstance(config, dict) or type(proof.get("expected_version")) is not str:
        raise _changed()
    await _verify(db, actor, sid, eid, scope, config)
    await _version(db, sid, eid, proof["expected_version"])
    await db.rollback()
    try:
        data = base64.b64decode(body.image_base64, validate=True)
    except (ValueError, binascii.Error):
        raise BadRequestError("生成图片数据无效，请重新生成") from None
    if (len(data) != proof.get("size") or hashlib.sha256(data).hexdigest() != proof.get("sha256")):
        raise _changed()
    image = await anyio.to_thread.run_sync(
        setting_image_codec.validate_standard_image, data, "image/webp", limiter=setting_image_codec._limiter,
    )
    revision = scope.get("revision")
    if type(revision) is not int or revision < 0:
        raise _changed()
    async with series_service._edit(db, actor.id, sid, revision) as parent:
        await _verify_scope(db, actor, sid, eid, scope)
        uses = await series_service._uses(db, sid, entry_ids=[eid])
        await series_service._lock_projects(db, actor.id, parent, [use.project_id for use in uses], projection=True)
        await _verify_config(db, actor.id, config)
        await _version(db, sid, eid, proof["expected_version"])
        meta = await setting_image_service.set_series_image_without_commit(db, sid, eid, image)
        parent.revision += 1
        return SeriesThumbnailResult(thumbnail=meta, revision=parent.revision)
