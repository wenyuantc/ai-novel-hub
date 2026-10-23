"""AI 向导：流式生成大纲 / 角色 / 世界观，结束后解析 JSON 并覆盖落库。"""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import build_characters_messages, build_outline_messages, build_world_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory
from app.core.security import SecretCipher
from app.models import Character, Outline, WorldSetting
from app.schemas.chapter import WizardRequest
from app.schemas.story import CharacterOut, OutlineOut, WorldSettingOut
from app.services import ai_config_service, story_service
from app.services.generation_service import Event
from app.services.project_service import get_owned_project, project_info

logger = logging.getLogger(__name__)

WizardKind = Literal["outline", "characters", "world"]


@dataclass
class WizardJob:
    project_id: int
    kind: WizardKind
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]


async def prepare_wizard_job(
    db: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    kind: WizardKind,
    req: WizardRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> WizardJob:
    project = await get_owned_project(db, user_id, project_id)
    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)

    info = project_info(project)
    world_text = story_service.format_world(await story_service.list_items(db, story_service.WORLD, project_id))
    characters_text = story_service.format_characters(
        await story_service.list_items(db, story_service.CHARACTERS, project_id)
    )
    outline_text = story_service.format_outlines(await story_service.list_items(db, story_service.OUTLINES, project_id))

    if kind == "outline":
        messages = build_outline_messages(
            info,
            volume_count=req.volume_count,
            chapters_per_volume=req.chapters_per_volume,
            world_text=world_text,
            characters_text=characters_text,
            instruction=req.instruction,
        )
    elif kind == "characters":
        messages = build_characters_messages(
            info,
            character_count=req.character_count,
            world_text=world_text,
            outline_text=outline_text,
            instruction=req.instruction,
        )
    else:
        messages = build_world_messages(
            info, outline_text=outline_text, characters_text=characters_text, instruction=req.instruction
        )
    return WizardJob(project_id=project_id, kind=kind, provider=provider, model=cfg.model, messages=messages)


# ---------------- 解析与规范化 ----------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(_text(v) for v in value if _text(v))
    if isinstance(value, dict):
        return "；".join(f"{k}：{_text(v)}" for k, v in value.items() if _text(v))
    return str(value).strip()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _unwrap_list(obj: Any, keys: tuple[str, ...]) -> list:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in keys:
            if isinstance(obj.get(key), list):
                return obj[key]
        # 只有一个列表字段时直接取
        lists = [v for v in obj.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    raise JSONExtractError("JSON 结构不符合预期")


def normalize_outline(obj: Any) -> list[dict]:
    items = _unwrap_list(obj, ("volumes", "outline", "outlines", "chapters"))
    rows: list[dict] = []
    if items and all(isinstance(v, dict) and isinstance(v.get("chapters"), list) for v in items):
        for vi, vol in enumerate(items, start=1):
            volume_no = _int(vol.get("volume_no"), vi)
            volume_title = _text(vol.get("volume_title") or vol.get("title"))[:200]
            for ch in vol["chapters"]:
                if isinstance(ch, dict):
                    rows.append({"volume_no": volume_no, "volume_title": volume_title, **_chapter_fields(ch)})
    else:
        for ch in items:
            if isinstance(ch, dict):
                rows.append(
                    {
                        "volume_no": _int(ch.get("volume_no"), 1),
                        "volume_title": _text(ch.get("volume_title"))[:200],
                        **_chapter_fields(ch),
                    }
                )
    rows = [r for r in rows if r["title"]]
    if not rows:
        raise JSONExtractError("未解析到任何大纲章节")
    # 章节号统一为全书连续编号，避免模型按卷重复编号
    for i, r in enumerate(rows, start=1):
        r["chapter_no"] = i
        r["sort"] = i
    return rows


def _chapter_fields(ch: dict) -> dict:
    return {
        "title": _text(ch.get("title") or ch.get("name"))[:200],
        "summary": _text(ch.get("summary") or ch.get("content") or ch.get("description")),
    }


def normalize_characters(obj: Any) -> list[dict]:
    items = _unwrap_list(obj, ("characters", "roles", "items"))
    rows = []
    for c in items:
        if not isinstance(c, dict):
            continue
        name = _text(c.get("name"))[:100]
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "role_type": _text(c.get("role_type") or c.get("role"))[:50],
                "profile": _text(c.get("profile") or c.get("description")),
                "relations_text": _text(c.get("relations_text") or c.get("relations")),
            }
        )
    if not rows:
        raise JSONExtractError("未解析到任何角色")
    for i, r in enumerate(rows, start=1):
        r["sort"] = i
    return rows


def normalize_world(obj: Any) -> list[dict]:
    items = _unwrap_list(obj, ("world", "settings", "items", "world_settings"))
    rows = []
    for w in items:
        if not isinstance(w, dict):
            continue
        title = _text(w.get("title") or w.get("name"))[:200]
        if not title:
            continue
        rows.append({"title": title, "content": _text(w.get("content") or w.get("description"))})
    if not rows:
        raise JSONExtractError("未解析到任何世界观条目")
    for i, r in enumerate(rows, start=1):
        r["sort"] = i
    return rows


_KIND_SPEC = {
    "outline": (Outline, normalize_outline, OutlineOut),
    "characters": (Character, normalize_characters, CharacterOut),
    "world": (WorldSetting, normalize_world, WorldSettingOut),
}


async def save_wizard_result(db: AsyncSession, project_id: int, kind: WizardKind, raw_text: str) -> list[dict]:
    """解析模型输出并覆盖该类型旧数据，返回序列化后的实体列表。"""
    model, normalize, out_schema = _KIND_SPEC[kind]
    rows = normalize(extract_json(raw_text))
    await db.execute(delete(model).where(model.project_id == project_id))
    entities = [model(project_id=project_id, **row) for row in rows]
    db.add_all(entities)
    await db.commit()
    for e in entities:
        await db.refresh(e)
    return [out_schema.model_validate(e).model_dump(mode="json") for e in entities]


async def run_wizard_job(job: WizardJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    pieces: list[str] = []
    try:
        async with aclosing(job.provider.generate_stream(job.messages, job.model)) as stream:
            async for piece in stream:
                pieces.append(piece)
                yield "delta", {"text": piece}
        raw = "".join(pieces)
        if not raw.strip():
            raise AIProviderError("模型未返回任何内容")
        async with sessionmaker() as db:
            items = await save_wizard_result(db, job.project_id, job.kind, raw)
        yield "done", {"kind": job.kind, "items": items}
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except Exception:  # noqa: BLE001
        logger.exception("向导生成失败 project=%s kind=%s", job.project_id, job.kind)
        yield "error", {"detail": "生成失败：服务器内部错误"}
