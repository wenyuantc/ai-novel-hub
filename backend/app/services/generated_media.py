"""作品资产生图。使用认证会话绑定模型，图片计量、归属和发出请求的身份由服务端确定。"""

import asyncio
import math
from collections.abc import AsyncIterator
from io import BytesIO

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.images import MAX_COVER_BYTES
from app.ai.providers import create_provider
from app.ai.providers.base import AIProviderError, GeneratedImage, ImageCapabilityError, ImageOptions
from app.core.errors import BadRequestError
from app.core.security import SecretCipher
from app.media import service
from app.models.media import MediaObject
from app.services import ai_dispatch, collaboration_ai_guard, project_access, project_ai_config


def _verify_ratio(image: GeneratedImage, ratio: str, provider: str) -> None:
    width, height = (int(n) for n in ratio.split(":"))
    try:
        with Image.open(BytesIO(image.data)) as decoded:
            # Gemini 按离散像素档输出（如 4:3 实际为 1184×864），保留生成的原始构图。
            tolerance = 0.03 if provider == "gemini" else 0.0
            if not math.isclose(decoded.width / decoded.height, width / height, rel_tol=tolerance):
                raise ImageCapabilityError("图片模型返回的画幅与请求不符，请更换支持该画幅的模型")
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise AIProviderError("图片模型返回了无效的图片") from exc


async def generate_stored_image(
    db: AsyncSession, actor_id: int, project_id: int, prompt: str, *, options: ImageOptions,
    cipher: SecretCipher, config_id: int | None = None, ai_timeout: float, allow_private: bool,
    plugin_id: str | None = None,
) -> MediaObject:
    """认证请求的独立会话。调用方把已获准发送的图片字节传入，并用返回的对象 ID 关联业务记录。"""
    binding = await project_ai_config.resolve_project_model(
        db, actor_id, project_id, "image", cipher=cipher, config_id=config_id,
        allow_private=allow_private, for_update=True,
    )
    stamp = await ai_dispatch.read_visibility_stamp(db, project_id)
    guard = collaboration_ai_guard.for_http(db, actor_id, binding, stamp, cipher=cipher, allow_private=allow_private)
    maker = ai_dispatch.visibility_sessionmaker(db)
    await db.rollback()
    bound = await guard.prepare_provider(
        maker, create_provider, request_timeout=ai_timeout, usage_kind="image.generate",
    )
    try:
        async with asyncio.timeout(ai_timeout):
            image = await bound.provider.generate_image(prompt, bound.model, options=options)
        if options.aspect_ratio:
            await asyncio.to_thread(_verify_ratio, image, options.aspect_ratio, bound.provider_name)
    except ai_dispatch.VisibilityPolicyError:
        raise
    except ImageCapabilityError as exc:
        raise BadRequestError(str(exc)) from None
    except TimeoutError:
        raise BadRequestError("图片生成超时，请稍后重试") from None
    except AIProviderError:
        raise BadRequestError("图片模型调用失败，请检查模型配置和参考图") from None

    # 存储会自行提交配额预留，须在生成后重验原会话、协作授权和原模型。
    await project_access.require_project(db, actor_id, project_id, "ai.use", for_update=True,
                                         grant_identity=binding.grant_identity)
    await project_ai_config.revalidate_project_model(db, binding, cipher=cipher, allow_private=allow_private,
                                                    lock_config=True)
    await ai_dispatch.require_visibility_stamp(db, stamp)

    async def chunks() -> AsyncIterator[bytes]:
        yield image.data

    return await service.store(
        db, owner_id=binding.project_owner_id, project_id=project_id, plugin_id=plugin_id, scope="project",
        kind="image", mime=image.mime_type, source=chunks(), max_bytes=MAX_COVER_BYTES, origin="generated",
    )
