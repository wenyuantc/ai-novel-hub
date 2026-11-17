"""作品设定图：可见字段构词、受控模型发送与预览确认。"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
from typing import Any, cast

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts.registry import SETTING_THUMBNAIL_KEY
from app.ai.prompts.setting_thumbnail import build_thumbnail_prompt
from app.ai.providers import ProviderFactory
from app.ai.providers.base import AIProviderError
from app.core.errors import BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.schemas.setting_field_value import EntityKind
from app.schemas.setting_fields import EntityRef
from app.schemas.setting_image import (
    ThumbnailAiApplyRequest,
    ThumbnailAiGenerateOut,
    ThumbnailAiGenerateRequest,
    ThumbnailAiPrompt,
    ThumbnailMeta,
)
from app.services import (
    ai_dispatch,
    collaboration_ai_guard,
    project_access,
    project_ai_config,
    prompt_service,
    series_policy,
    setting_ai_context,
    setting_field_service,
    setting_image_codec,
    setting_image_service,
    thumbnail_ai_tokens,
)
from app.services.authorization_service import AuthenticatedActor
from app.services.setting_field_formatting import format_entry_fields

_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "character": (("名称", "name"), ("别名", "aliases"), ("角色定位", "role_type"), ("年龄", "age"),
                  ("性别", "gender"), ("简介", "profile"), ("外貌", "appearance"),
                  ("性格", "personality"), ("背景", "background"), ("当前状态", "current_state"),
                  ("生存状态", "life_status"), ("关系备注", "relations_text")),
    "world": (("名称", "title"), ("别名", "aliases"), ("分类", "category"), ("内容", "content")),
    "organization": (("名称", "name"), ("类型", "org_type"), ("宗旨", "purpose"), ("地点", "location"),
                     ("实力", "power_level"), ("描述", "description")),
    "career": (("名称", "name"), ("类型", "kind"), ("描述", "description"), ("获得条件", "requirements"),
               ("能力", "abilities"), ("规则", "rules")),
}


def _actor(db: AsyncSession, actor_id: int) -> AuthenticatedActor:
    actor = db.info.get("authenticated_actor")
    if not isinstance(actor, AuthenticatedActor) or actor.id != actor_id:
        raise collaboration_ai_guard.CollaborationSendBlocked()
    return actor


def _changed() -> ai_dispatch.VisibilityPolicyError:
    return ai_dispatch.VisibilityPolicyError("设定来源已变化，请重新获取提示词", extra={
        "code": "entity_source_changed",
    })


def _stamp(stamp: ai_dispatch.VisibilityStamp) -> dict[str, Any]:
    return {"revision": stamp.revision, "epoch": stamp.epoch,
            "last_deleted_at": stamp.last_deleted_at.isoformat() if stamp.last_deleted_at is not None else None}


async def _projected_fields(
    db: AsyncSession, actor_id: int, project_id: int, kind: EntityKind, entity_id: int,
) -> tuple[list[tuple[str, str]], ai_dispatch.VisibilityStamp]:
    model = setting_image_service._MODELS[kind]
    row = await db.scalar(select(model).where(model.project_id == project_id, model.id == entity_id)
                          .execution_options(populate_existing=True))
    if row is None:
        raise _changed()
    if kind in ("character", "world"):
        await series_policy.reject_bound_entry(db, project_id, cast(series_policy.ProjectionKind, kind), entity_id,
                                               actor_user_id=actor_id)
    scope = await setting_ai_context.build_scope(db, project_id, explicit=((kind, entity_id),))
    scope.require_entry(kind, entity_id)
    # 不让其他 always / 一跳条目的引用字段变成自动提示词。
    scope.selected = {(kind, entity_id)}
    fields: list[tuple[str, str]] = []
    for label, key in _FIELDS[kind]:
        if not scope.field_visible(kind, entity_id, key):
            continue
        value = getattr(row, key)
        if isinstance(value, list):
            value = "、".join(str(item) for item in value)
        if value is not None and str(value).strip():
            fields.append((label, str(value)))
    resolved = await setting_field_service.resolve_entry_fields(
        db, project_id, [EntityRef(kind=kind, id=entity_id)], include_legacy=True,
    )
    projected = scope.entry_fields(resolved[kind, entity_id])
    projected = projected.model_copy(update={
        "fields": [slot for slot in projected.fields if slot.field_type != "reference"],
        "legacy_fields": [slot for slot in projected.legacy_fields if slot.field_type != "reference"],
    })
    custom = format_entry_fields(projected)
    if custom:
        fields.append(("补充设定", custom))
    await scope.verify(db)
    return fields, scope.stamp


def _digest_fields(fields: list[tuple[str, str]]) -> str:
    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _identity(
    db: AsyncSession, actor_id: int, project_id: int, kind: EntityKind, entity_id: int, *, lock: bool,
) -> tuple[Any, list[Any] | None]:
    project, access, member = await project_access.resolve_access(
        db, actor_id, project_id, for_update=lock, visibility_write=lock,
    )
    if not access.can_use_ai:
        await project_access.require_project(db, actor_id, project_id, "ai.use")
    if not access.can_edit_content:
        await project_access.require_project(db, actor_id, project_id, "content.edit")
    await setting_image_service._book_write_guard(db, actor_id, project_id, kind, entity_id, lock=False)
    grant = [member.invitation_id, member.revision] if access.is_collaborator and member else None
    return project, grant


async def _verify_source(
    db: AsyncSession, actor_id: int, project_id: int, kind: EntityKind, entity_id: int, source: dict[str, Any],
) -> ai_dispatch.VisibilityStamp:
    try:
        fields, stamp = await _projected_fields(db, actor_id, project_id, kind, entity_id)
    except BadRequestError as exc:
        if (exc.extra or {}).get("code") == "setting_not_available":
            raise _changed() from None
        raise
    expected = source.get("fields_hash")
    if (source.get("visibility") != _stamp(stamp) or not isinstance(expected, str)
            or not hmac.compare_digest(expected, _digest_fields(fields))):
        raise _changed()
    return stamp


async def _verify_identity(
    db: AsyncSession, actor_id: int, project_id: int, kind: EntityKind, entity_id: int,
    source: dict[str, Any], *, lock: bool,
) -> Any:
    actor = _actor(db, actor_id)
    if (source.get("actor_id") != actor_id or source.get("token_version") != actor.token_version
            or source.get("project_id") != project_id or source.get("kind") != kind
            or source.get("entity_id") != entity_id):
        raise _changed()
    project, grant = await _identity(db, actor_id, project_id, kind, entity_id, lock=lock)
    if source.get("owner_id") != project.user_id or source.get("grant") != grant:
        raise _changed()
    return project


async def default_prompt(
    db: AsyncSession, actor_id: int, project_id: int, kind: EntityKind, entity_id: int, cipher: SecretCipher,
) -> ThumbnailAiPrompt:
    project, grant = await _identity(db, actor_id, project_id, kind, entity_id, lock=True)
    actor = _actor(db, actor_id)
    fields, stamp = await _projected_fields(db, actor_id, project_id, kind, entity_id)
    source = {"actor_id": actor_id, "token_version": actor.token_version, "project_id": project_id,
              "kind": kind, "entity_id": entity_id, "owner_id": project.user_id, "grant": grant,
              "visibility": _stamp(stamp), "fields_hash": _digest_fields(fields)}
    override = await prompt_service.get_override(db, actor_id, SETTING_THUMBNAIL_KEY)
    return ThumbnailAiPrompt(prompt=build_thumbnail_prompt(kind, fields, override=override),
                             prompt_token=thumbnail_ai_tokens.issue_token(cipher, "project-prompt", source))


async def generate(
    db: AsyncSession, actor_id: int, project_id: int, kind: EntityKind, entity_id: int,
    body: ThumbnailAiGenerateRequest, cipher: SecretCipher, factory: ProviderFactory, *,
    ai_timeout: float, allow_private: bool,
) -> ThumbnailAiGenerateOut:
    source = thumbnail_ai_tokens.read_token(cipher, "project-prompt", body.prompt_token)
    await _verify_identity(db, actor_id, project_id, kind, entity_id, source, lock=True)
    stamp = await _verify_source(db, actor_id, project_id, kind, entity_id, source)
    current = await setting_image_service._current_book_resource(db, project_id, kind, entity_id)
    setting_image_service._check_version(current.id if current else None, body.expected_version)
    binding = await project_ai_config.resolve_project_model(
        db, actor_id, project_id, "image", cipher=cipher, config_id=body.config_id,
        allow_private=allow_private, for_update=True,
    )
    if (list(binding.grant_identity) if binding.grant_identity else None) != source.get("grant"):
        raise _changed()

    async def verify_source(send_db: AsyncSession) -> None:
        await _verify_source(send_db, actor_id, project_id, kind, entity_id, source)

    guard = collaboration_ai_guard.for_http(
        db, actor_id, binding, stamp, cipher=cipher, allow_private=allow_private, http_source_verify=verify_source,
    )
    maker = ai_dispatch.visibility_sessionmaker(db)
    await db.rollback()
    try:
        bound = await guard.prepare_provider(maker, factory, request_timeout=ai_timeout)
        async with asyncio.timeout(ai_timeout):
            generated = await bound.provider.generate_image(body.prompt, bound.model)
        image = await setting_image_codec.normalize_image_async(generated.data, generated.mime_type)
    except ai_dispatch.VisibilityPolicyError:
        raise
    except TimeoutError:
        raise BadRequestError("图片生成超时，请稍后重试") from None
    except (AIProviderError, ValueError):
        raise BadRequestError("图片模型调用失败，请稍后重试") from None
    await _verify_identity(db, actor_id, project_id, kind, entity_id, source, lock=True)
    await _verify_source(db, actor_id, project_id, kind, entity_id, source)
    await project_ai_config.revalidate_project_model(db, binding, cipher=cipher, allow_private=allow_private,
                                                    lock_config=True)
    current = await setting_image_service._current_book_resource(db, project_id, kind, entity_id)
    setting_image_service._check_version(current.id if current else None, body.expected_version)
    preview = {"source": source, "model": project_ai_config.serialize_project_model(binding),
               "expected_version": body.expected_version, "image_sha256": image.sha256}
    return ThumbnailAiGenerateOut(image_base64=base64.b64encode(image.data).decode("ascii"),
                                  model_source=binding.source,
                                  preview_token=thumbnail_ai_tokens.issue_token(cipher, "project-preview", preview))


async def apply(
    db: AsyncSession, actor_id: int, project_id: int, kind: EntityKind, entity_id: int,
    body: ThumbnailAiApplyRequest, cipher: SecretCipher, *, allow_private: bool,
) -> ThumbnailMeta:
    preview = thumbnail_ai_tokens.read_token(cipher, "project-preview", body.preview_token)
    source = preview.get("source")
    if not isinstance(source, dict):
        raise _changed()
    await _verify_identity(db, actor_id, project_id, kind, entity_id, source, lock=False)
    try:
        data = base64.b64decode(body.image_base64, validate=True)
    except (ValueError, binascii.Error):
        raise BadRequestError("生成图片格式无效") from None
    if len(data) > setting_image_codec.MAX_STANDARD_BYTES:
        raise BadRequestError("生成图片超过大小限制")
    image = await anyio.to_thread.run_sync(setting_image_codec.validate_standard_image, data, "image/webp",
                                           limiter=setting_image_codec._limiter)
    digest = preview.get("image_sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(digest, image.sha256):
        raise ConflictError("图片预览已变化，请重新生成", extra={"code": "thumbnail_ai_token_invalid"})
    project = await _verify_identity(db, actor_id, project_id, kind, entity_id, source, lock=True)
    await _verify_source(db, actor_id, project_id, kind, entity_id, source)
    binding = project_ai_config.deserialize_project_model(preview.get("model"), actor_id=actor_id,
                                                          project_id=project_id, project_owner_id=project.user_id)
    if binding is None or binding.model_type != "image":
        raise _changed()
    await project_ai_config.revalidate_project_model(db, binding, cipher=cipher, allow_private=allow_private,
                                                    lock_config=True)
    current = await setting_image_service._current_book_resource(db, project_id, kind, entity_id)
    expected = preview.get("expected_version")
    if not isinstance(expected, str):
        raise _changed()
    setting_image_service._check_version(current.id if current else None, expected)
    meta = await setting_image_service.set_book_image_without_commit(db, project_id, kind, entity_id, image)
    await db.commit()
    assert meta is not None
    return meta
