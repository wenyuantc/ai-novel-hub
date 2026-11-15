import asyncio
import base64
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.images import validate_image
from app.ai.prompts.cover import build_cover_prompt
from app.ai.providers import ProviderFactory
from app.ai.providers.base import AIProviderError
from app.core.errors import AppError, BadRequestError, NotFoundError
from app.core.security import SecretCipher
from app.models import Project, ProjectCover
from app.schemas.project import ProjectOut
from app.schemas.project_cover import CoverGenerateOut, CoverGenerateRequest, CoverPromptOut
from app.services import (
    ai_config_service,
    ai_dispatch,
    collaboration_ai_guard,
    project_access,
    project_ai_config,
    project_service,
    style_service,
)

# 仅在真实发送、撤权与结果投影均完成验证后开放。
_COLLABORATION_IMAGE_ENABLED = True


class _CoverStorageError(AppError):
    status_code = 500

    def __init__(self) -> None:
        super().__init__("服务器内部错误")


async def get_cover(db: AsyncSession, user_id: int, project_id: int) -> ProjectCover:
    await project_service.get_manageable_project(db, user_id, project_id)
    cover = await db.get(ProjectCover, project_id)
    if cover is None:
        raise NotFoundError("项目尚未设置封面")
    return cover


async def get_manual_cover(db: AsyncSession, user_id: int, project_id: int) -> ProjectCover:
    await project_access.require_project(db, user_id, project_id, "content.read")
    cover = await db.get(ProjectCover, project_id)
    if cover is None:
        raise NotFoundError("项目尚未设置封面")
    return cover


async def _lock_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    return await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)


async def put_cover(db: AsyncSession, user_id: int, project_id: int, data: bytes) -> ProjectOut:
    project = await _lock_project(db, user_id, project_id)
    try:
        mime_type = validate_image(data)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    cover = await db.get(ProjectCover, project.id)
    now = datetime.now(UTC)
    if cover is None:
        cover = ProjectCover(project_id=project.id, data=data, mime_type=mime_type, updated_at=now)
        db.add(cover)
    else:
        previous = cover.updated_at.replace(tzinfo=UTC) if cover.updated_at.tzinfo is None else cover.updated_at
        cover.data, cover.mime_type = data, mime_type
        # SQLite 的 func.now() 只有秒精度；显式微秒版本保证快速连续替换也更新 URL。
        cover.updated_at = max(now, previous + timedelta(microseconds=1))
    await db.commit()
    return await project_service.project_out(db, project, actor_user_id=user_id)


async def delete_cover(db: AsyncSession, user_id: int, project_id: int) -> ProjectOut:
    project = await _lock_project(db, user_id, project_id)
    cover = await db.get(ProjectCover, project.id)
    if cover is not None:
        await db.delete(cover)
    await db.commit()
    return await project_service.project_out(db, project, actor_user_id=user_id)


async def default_prompt(db: AsyncSession, user_id: int, project_id: int) -> CoverPromptOut:
    try:
        # 提示词可能包含项目内容；认证、原协作权限与项目锁须持续到投影完成。
        project = await project_access.require_project(db, user_id, project_id, "ai.use", for_update=True)
        style = await style_service.visible_default_style(db, project, actor_user_id=user_id)
        warning = ("当前账号无法继承作品默认风格，已按无风格生成提示词。"
                   if project.default_style_id is not None and style is None else None)
        return CoverPromptOut(prompt=build_cover_prompt(project, style), warning=warning)
    except SQLAlchemyError:
        pass
    # 退出 except 后再抛，不能把 SQL、参数或驱动异常链交给通用错误日志。
    raise _CoverStorageError()


async def generate_cover(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    body: CoverGenerateRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    *,
    ai_timeout: float,
    allow_private: bool,
) -> CoverGenerateOut:
    try:
        return await _generate_cover(
            db, user_id, project_id, body, cipher, factory, ai_timeout=ai_timeout, allow_private=allow_private,
        )
    except SQLAlchemyError:
        pass
    raise _CoverStorageError()


async def _generate_cover(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    body: CoverGenerateRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    *,
    ai_timeout: float,
    allow_private: bool,
) -> CoverGenerateOut:
    project = await project_access.require_project(db, user_id, project_id, "ai.use")
    if project.user_id != user_id:
        if not _COLLABORATION_IMAGE_ENABLED:
            raise NotFoundError("项目不存在")
        return await _generate_collaborator_cover(
            db, user_id, project_id, body, cipher, factory, ai_timeout=ai_timeout, allow_private=allow_private,
        )
    cfg = await ai_config_service.resolve_config(db, user_id, body.config_id, model_type="image")
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    try:
        async with asyncio.timeout(ai_timeout):
            image = await provider.generate_image(body.prompt, cfg.model)
        mime_type = validate_image(image.data, image.mime_type)
    except TimeoutError as exc:
        raise BadRequestError("封面生成超时，请稍后重试") from exc
    except (AIProviderError, ValueError) as exc:
        raise BadRequestError(str(exc)) from exc
    return CoverGenerateOut(image_base64=base64.b64encode(image.data).decode("ascii"), mime_type=mime_type)


async def _generate_collaborator_cover(
    db: AsyncSession, user_id: int, project_id: int, body: CoverGenerateRequest,
    cipher: SecretCipher, factory: ProviderFactory, *, ai_timeout: float, allow_private: bool,
) -> CoverGenerateOut:
    # 原 actor、邀请轮次和配置身份只在服务端保留；真实 SDK 的每次物理请求重新校验。
    binding = await project_ai_config.resolve_project_model(
        db, user_id, project_id, "image", cipher=cipher, config_id=body.config_id,
        allow_private=allow_private, for_update=True,
    )
    stamp = await ai_dispatch.read_visibility_stamp(db, project_id)
    guard = collaboration_ai_guard.for_http(db, user_id, binding, stamp, cipher=cipher, allow_private=allow_private)
    maker = ai_dispatch.visibility_sessionmaker(db)
    await db.rollback()  # 模型构造和外部发送期间不持请求事务及项目锁。
    try:
        bound = await guard.prepare_provider(maker, factory, request_timeout=ai_timeout)
        async with asyncio.timeout(ai_timeout):
            image = await bound.provider.generate_image(body.prompt, bound.model)
        mime_type = validate_image(image.data, image.mime_type)
    except ai_dispatch.VisibilityPolicyError:
        raise
    except TimeoutError as exc:
        raise BadRequestError("封面生成超时，请稍后重试") from exc
    except (AIProviderError, ValueError):
        # 第三方异常可能带作者端点、模型或密钥；协作者只得到固定安全提示。
        raise BadRequestError("图片模型调用失败，请稍后重试") from None
    result = CoverGenerateOut(
        image_base64=base64.b64encode(image.data).decode("ascii"), mime_type=mime_type,
        model_source=binding.source,
    )
    # 返回前复验原 token、邀请、生命周期与原模型；锁由请求 session 持有至响应处理完成。
    await project_access.require_project(
        db, user_id, project_id, "ai.use", for_update=True, grant_identity=binding.grant_identity,
    )
    await project_ai_config.revalidate_project_model(
        db, binding, cipher=cipher, allow_private=allow_private, lock_config=True,
    )
    await ai_dispatch.require_visibility_stamp(db, stamp)
    return result
