import asyncio
import time
from contextlib import aclosing

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.endpoint_guard import ensure_endpoint_allowed
from app.ai.providers import AIProviderError, LLMProvider, ProviderFactory
from app.core.errors import BadRequestError, NotFoundError
from app.core.security import SecretCipher, mask_secret
from app.models import AIConfig
from app.schemas.ai_config import (
    AIConfigCreate,
    AIConfigOut,
    AIConfigTestResult,
    AIConfigUpdate,
    ModelInfo,
    ModelListRequest,
    ModelListResult,
    ModelType,
    check_output_within_context,
)

TEST_TIMEOUT_SECONDS = 30
LIST_MODELS_TIMEOUT_SECONDS = 30
TOKENS_PER_K = 1000
# 推算上下文预算时，未设最大输出按 8K 预留
DEFAULT_OUTPUT_K = 8
# 系统提示、章节要求等固定模板占用
PROMPT_RESERVE_TOKENS = 4000
# 中文约 1 字 ≈ 1 token 的保守估算
CHARS_PER_TOKEN = 1.0
MIN_CONTEXT_CHARS = 1000


def to_out(cfg: AIConfig, cipher: SecretCipher) -> AIConfigOut:
    try:
        masked = mask_secret(cipher.decrypt(cfg.api_key_enc))
    except ValueError:
        masked = "（解密失败）"
    return AIConfigOut(
        id=cfg.id,
        name=cfg.name,
        provider=cfg.provider,  # type: ignore[arg-type]
        base_url=cfg.base_url,
        model=cfg.model,
        model_type=cfg.model_type,  # type: ignore[arg-type]
        api_key_masked=masked,
        is_default=cfg.is_default,
        thinking_level=cfg.thinking_level,  # type: ignore[arg-type]
        context_window_k=cfg.context_window_k,
        max_output_k=cfg.max_output_k,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


def _clean_base_url(url: str | None) -> str | None:
    url = (url or "").strip()
    return url.rstrip("/") or None


async def list_configs(db: AsyncSession, user_id: int) -> list[AIConfig]:
    rows = await db.scalars(
        select(AIConfig)
        .where(AIConfig.user_id == user_id)
        .order_by(AIConfig.model_type.desc(), AIConfig.is_default.desc(), AIConfig.id)
    )
    return list(rows)


async def get_config(db: AsyncSession, user_id: int, config_id: int) -> AIConfig:
    cfg = await db.scalar(select(AIConfig).where(AIConfig.id == config_id, AIConfig.user_id == user_id))
    if cfg is None:
        raise NotFoundError("模型配置不存在")
    return cfg


async def _clear_default(db: AsyncSession, user_id: int, model_type: str, except_id: int | None = None) -> None:
    """默认配置按模型类型各自唯一。"""
    stmt = update(AIConfig).where(
        AIConfig.user_id == user_id, AIConfig.model_type == model_type, AIConfig.is_default.is_(True)
    )
    if except_id is not None:
        stmt = stmt.where(AIConfig.id != except_id)
    await db.execute(stmt.values(is_default=False))


async def create_config(db: AsyncSession, cipher: SecretCipher, user_id: int, data: AIConfigCreate) -> AIConfig:
    has_any = await db.scalar(
        select(AIConfig.id).where(AIConfig.user_id == user_id, AIConfig.model_type == data.model_type).limit(1)
    )
    is_default = data.is_default or has_any is None  # 该类型的第一条配置自动成为默认
    if is_default:
        await _clear_default(db, user_id, data.model_type)
    cfg = AIConfig(
        user_id=user_id,
        name=data.name.strip(),
        provider=data.provider,
        base_url=_clean_base_url(data.base_url),
        api_key_enc=cipher.encrypt(data.api_key.strip()),
        model=data.model.strip(),
        model_type=data.model_type,
        is_default=is_default,
        thinking_level=data.thinking_level,
        context_window_k=data.context_window_k,
        max_output_k=data.max_output_k,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def update_config(
    db: AsyncSession, cipher: SecretCipher, user_id: int, config_id: int, data: AIConfigUpdate
) -> AIConfig:
    cfg = await get_config(db, user_id, config_id)
    fields = data.model_fields_set
    old_type = cfg.model_type
    if data.name is not None:
        cfg.name = data.name.strip()
    if data.provider is not None:
        cfg.provider = data.provider
    if "base_url" in fields:
        cfg.base_url = _clean_base_url(data.base_url)
    if data.api_key:
        cfg.api_key_enc = cipher.encrypt(data.api_key.strip())
    if data.model is not None:
        cfg.model = data.model.strip()
    if data.model_type is not None:
        cfg.model_type = data.model_type
    if data.thinking_level is not None:
        cfg.thinking_level = data.thinking_level
    if "context_window_k" in fields:
        cfg.context_window_k = data.context_window_k
    if "max_output_k" in fields:
        cfg.max_output_k = data.max_output_k
    try:
        check_output_within_context(cfg.context_window_k, cfg.max_output_k)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    type_changed = cfg.model_type != old_type
    was_default = cfg.is_default
    if data.is_default is True:
        await _clear_default(db, user_id, cfg.model_type, except_id=cfg.id)
        cfg.is_default = True
    elif data.is_default is False:
        cfg.is_default = False
    elif type_changed:
        # 换类型后不得抢占新类型已有的默认；新类型尚无配置时自动成为默认
        has_other = await db.scalar(
            select(AIConfig.id)
            .where(AIConfig.user_id == user_id, AIConfig.model_type == cfg.model_type, AIConfig.id != cfg.id)
            .limit(1)
        )
        cfg.is_default = has_other is None
    if type_changed and was_default:
        await db.flush()
        await _promote_default(db, user_id, old_type)
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def set_default(db: AsyncSession, user_id: int, config_id: int) -> AIConfig:
    cfg = await get_config(db, user_id, config_id)
    await _clear_default(db, user_id, cfg.model_type, except_id=cfg.id)
    cfg.is_default = True
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def delete_config(db: AsyncSession, user_id: int, config_id: int) -> None:
    cfg = await get_config(db, user_id, config_id)
    was_default = cfg.is_default
    model_type = cfg.model_type
    await db.delete(cfg)
    await db.flush()
    if was_default:
        await _promote_default(db, user_id, model_type)
    await db.commit()


async def _promote_default(db: AsyncSession, user_id: int, model_type: str) -> None:
    """该类型失去默认配置时，把最新一条提升为默认。"""
    nxt = await db.scalar(
        select(AIConfig)
        .where(AIConfig.user_id == user_id, AIConfig.model_type == model_type)
        .order_by(AIConfig.id.desc())
        .limit(1)
    )
    if nxt is not None:
        nxt.is_default = True


MODEL_TYPE_LABELS = {"text": "文本", "image": "图片"}


async def resolve_config(
    db: AsyncSession, user_id: int, config_id: int | None, model_type: ModelType = "text"
) -> AIConfig:
    """取指定配置；未指定则取该类型的默认配置（无默认时取最新一条）。"""
    label = MODEL_TYPE_LABELS[model_type]
    if config_id is not None:
        cfg = await get_config(db, user_id, config_id)
        if cfg.model_type != model_type:
            raise BadRequestError(f"所选配置不是{label}模型")
        return cfg
    cfg = await db.scalar(
        select(AIConfig)
        .where(AIConfig.user_id == user_id, AIConfig.model_type == model_type)
        .order_by(AIConfig.is_default.desc(), AIConfig.id.desc())
        .limit(1)
    )
    if cfg is None:
        raise BadRequestError(f"尚未配置{label}模型，请先在「模型配置」中添加并设为默认")
    return cfg


def max_output_tokens(cfg: AIConfig) -> int | None:
    return cfg.max_output_k * TOKENS_PER_K if cfg.max_output_k else None


def context_char_budget(cfg: AIConfig, default: int) -> int:
    """按配置的上下文大小推算章节前文的字符预算；未设置时使用系统默认。"""
    if not cfg.context_window_k:
        return default
    output_k = cfg.max_output_k or DEFAULT_OUTPUT_K
    tokens = (cfg.context_window_k - output_k) * TOKENS_PER_K - PROMPT_RESERVE_TOKENS
    return max(MIN_CONTEXT_CHARS, int(tokens * CHARS_PER_TOKEN))


async def build_provider(
    cfg: AIConfig, cipher: SecretCipher, factory: ProviderFactory, request_timeout: float, *, allow_private: bool
) -> LLMProvider:
    try:
        api_key = cipher.decrypt(cfg.api_key_enc)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    try:
        if not allow_private:
            await ensure_endpoint_allowed(cfg.base_url)
        provider = factory(cfg.provider, api_key, cfg.base_url, request_timeout)
    except AIProviderError as exc:
        raise BadRequestError(str(exc)) from exc
    provider.follow_redirects = allow_private
    return provider


async def test_config(
    db: AsyncSession,
    cipher: SecretCipher,
    factory: ProviderFactory,
    user_id: int,
    config_id: int,
    *,
    allow_private: bool,
) -> AIConfigTestResult:
    cfg = await get_config(db, user_id, config_id)
    started = time.perf_counter()
    try:
        provider = await build_provider(cfg, cipher, factory, TEST_TIMEOUT_SECONDS, allow_private=allow_private)
    except BadRequestError as exc:
        return AIConfigTestResult(ok=False, message=exc.detail, latency_ms=_elapsed(started))
    if cfg.model_type == "image":
        return await _test_image_config(provider, cfg.model, started)
    messages = [{"role": "user", "content": "这是一次连通性测试，请只回复：ok"}]
    received = ""
    try:
        async with (
            asyncio.timeout(TEST_TIMEOUT_SECONDS),
            aclosing(
                provider.generate_stream(messages, cfg.model, max_tokens=16, thinking="off")  # type: ignore[arg-type]
            ) as stream,
        ):
            async for piece in stream:
                received += piece
                if len(received) >= 16:
                    break
    except TimeoutError:
        return AIConfigTestResult(
            ok=False, message=f"连接超时（{TEST_TIMEOUT_SECONDS} 秒）", latency_ms=_elapsed(started)
        )
    except AIProviderError as exc:
        return AIConfigTestResult(ok=False, message=str(exc), latency_ms=_elapsed(started))
    if not received.strip():
        return AIConfigTestResult(ok=False, message="模型未返回任何内容", latency_ms=_elapsed(started))
    return AIConfigTestResult(
        ok=True, message=f"连接成功，模型回复：{received.strip()[:50]}", latency_ms=_elapsed(started)
    )


async def _test_image_config(provider: LLMProvider, model: str, started: float) -> AIConfigTestResult:
    """图片模型不发起生成（耗时且计费），改为拉取模型列表校验连通与鉴权。"""
    try:
        async with asyncio.timeout(TEST_TIMEOUT_SECONDS):
            items = await provider.list_models()
    except TimeoutError:
        return AIConfigTestResult(
            ok=False, message=f"连接超时（{TEST_TIMEOUT_SECONDS} 秒）", latency_ms=_elapsed(started)
        )
    except AIProviderError as exc:
        return AIConfigTestResult(ok=False, message=str(exc), latency_ms=_elapsed(started))
    if any(m["id"] == model for m in items):
        message = f"连接成功，接口提供模型 {model}"
    else:
        message = f"连接成功，但模型列表中未找到 {model}，请确认模型名"
    return AIConfigTestResult(ok=True, message=message, latency_ms=_elapsed(started))


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def list_models(
    db: AsyncSession,
    cipher: SecretCipher,
    factory: ProviderFactory,
    user_id: int,
    req: ModelListRequest,
    *,
    allow_private: bool,
) -> ModelListResult:
    """用表单中尚未保存的参数拉取模型列表；Key 为空时可回退到已保存配置的 Key。"""
    api_key = (req.api_key or "").strip()
    if not api_key and req.config_id is not None:
        saved = await get_config(db, user_id, req.config_id)
        try:
            api_key = cipher.decrypt(saved.api_key_enc)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
    base_url = _clean_base_url(req.base_url)
    try:
        if not allow_private:
            await ensure_endpoint_allowed(base_url)
        provider = factory(req.provider, api_key, base_url, LIST_MODELS_TIMEOUT_SECONDS)
        provider.follow_redirects = allow_private
        async with asyncio.timeout(LIST_MODELS_TIMEOUT_SECONDS):
            items = await provider.list_models()
    except TimeoutError as exc:
        raise BadRequestError(f"获取模型列表超时（{LIST_MODELS_TIMEOUT_SECONDS} 秒）") from exc
    except AIProviderError as exc:
        raise BadRequestError(str(exc).replace("模型调用失败", "获取模型列表失败", 1)) from exc
    unique = {m["id"]: m for m in items}
    return ModelListResult(models=[ModelInfo(id=k, name=unique[k]["name"]) for k in sorted(unique)])
