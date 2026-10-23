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
from app.schemas.ai_config import AIConfigCreate, AIConfigOut, AIConfigTestResult, AIConfigUpdate

TEST_TIMEOUT_SECONDS = 30


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
        api_key_masked=masked,
        is_default=cfg.is_default,
        created_at=cfg.created_at,
        updated_at=cfg.updated_at,
    )


def _clean_base_url(url: str | None) -> str | None:
    url = (url or "").strip()
    return url.rstrip("/") or None


async def list_configs(db: AsyncSession, user_id: int) -> list[AIConfig]:
    rows = await db.scalars(
        select(AIConfig).where(AIConfig.user_id == user_id).order_by(AIConfig.is_default.desc(), AIConfig.id)
    )
    return list(rows)


async def get_config(db: AsyncSession, user_id: int, config_id: int) -> AIConfig:
    cfg = await db.scalar(select(AIConfig).where(AIConfig.id == config_id, AIConfig.user_id == user_id))
    if cfg is None:
        raise NotFoundError("模型配置不存在")
    return cfg


async def _clear_default(db: AsyncSession, user_id: int, except_id: int | None = None) -> None:
    stmt = update(AIConfig).where(AIConfig.user_id == user_id, AIConfig.is_default.is_(True))
    if except_id is not None:
        stmt = stmt.where(AIConfig.id != except_id)
    await db.execute(stmt.values(is_default=False))


async def create_config(db: AsyncSession, cipher: SecretCipher, user_id: int, data: AIConfigCreate) -> AIConfig:
    has_any = await db.scalar(select(AIConfig.id).where(AIConfig.user_id == user_id).limit(1))
    is_default = data.is_default or has_any is None  # 第一条配置自动成为默认
    if is_default:
        await _clear_default(db, user_id)
    cfg = AIConfig(
        user_id=user_id,
        name=data.name.strip(),
        provider=data.provider,
        base_url=_clean_base_url(data.base_url),
        api_key_enc=cipher.encrypt(data.api_key.strip()),
        model=data.model.strip(),
        is_default=is_default,
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
    if data.is_default is True:
        await _clear_default(db, user_id, except_id=cfg.id)
        cfg.is_default = True
    elif data.is_default is False:
        cfg.is_default = False
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def set_default(db: AsyncSession, user_id: int, config_id: int) -> AIConfig:
    cfg = await get_config(db, user_id, config_id)
    await _clear_default(db, user_id, except_id=cfg.id)
    cfg.is_default = True
    await db.commit()
    await db.refresh(cfg)
    return cfg


async def delete_config(db: AsyncSession, user_id: int, config_id: int) -> None:
    cfg = await get_config(db, user_id, config_id)
    was_default = cfg.is_default
    await db.delete(cfg)
    await db.flush()
    if was_default:
        nxt = await db.scalar(select(AIConfig).where(AIConfig.user_id == user_id).order_by(AIConfig.id.desc()).limit(1))
        if nxt is not None:
            nxt.is_default = True
    await db.commit()


async def resolve_config(db: AsyncSession, user_id: int, config_id: int | None) -> AIConfig:
    """取指定配置；未指定则取默认配置（无默认时取最新一条）。"""
    if config_id is not None:
        return await get_config(db, user_id, config_id)
    cfg = await db.scalar(
        select(AIConfig)
        .where(AIConfig.user_id == user_id)
        .order_by(AIConfig.is_default.desc(), AIConfig.id.desc())
        .limit(1)
    )
    if cfg is None:
        raise BadRequestError("尚未配置 AI 模型，请先在「模型配置」中添加并设为默认")
    return cfg


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
    messages = [{"role": "user", "content": "这是一次连通性测试，请只回复：ok"}]
    received = ""
    try:
        async with (
            asyncio.timeout(TEST_TIMEOUT_SECONDS),
            aclosing(provider.generate_stream(messages, cfg.model, max_tokens=16)) as stream,  # type: ignore[arg-type]
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


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
