import asyncio
import base64
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.images import validate_image
from app.ai.prompts.cover import build_cover_prompt
from app.ai.providers import ProviderFactory
from app.ai.providers.base import AIProviderError
from app.core.errors import BadRequestError, NotFoundError
from app.core.security import SecretCipher
from app.models import Project, ProjectCover, WritingStyle
from app.schemas.project import ProjectOut
from app.schemas.project_cover import CoverGenerateOut, CoverGenerateRequest, CoverPromptOut
from app.services import ai_config_service, project_service


async def get_cover(db: AsyncSession, user_id: int, project_id: int) -> ProjectCover:
    await project_service.get_manageable_project(db, user_id, project_id)
    cover = await db.get(ProjectCover, project_id)
    if cover is None:
        raise NotFoundError("项目尚未设置封面")
    return cover


async def _lock_project(db: AsyncSession, user_id: int, project_id: int) -> Project:
    return await project_service.get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")


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
    project = await project_service.get_owned_project(db, user_id, project_id)
    style = await db.get(WritingStyle, project.default_style_id) if project.default_style_id else None
    return CoverPromptOut(prompt=build_cover_prompt(project, style))


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
    await project_service.get_owned_project(db, user_id, project_id)
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
