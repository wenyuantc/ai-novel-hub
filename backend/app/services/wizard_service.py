"""AI 向导（后台任务 wizard.*）：流式生成大纲 / 角色 / 世界观，结束后解析 JSON 并覆盖落库。

角色向导（R11）输出 {characters: [结构化字段],
relationships: [{a, b, role_a, role_b, category, intimacy, description}]}，
兼容旧格式（角色数组，只有 profile / relations_text）。写入顺序：先整体替换角色（级联清掉旧关系 / 成员 / 出场记录），
再按名字（或别名）建立关系，经 relationship_service.apply_change(source="wizard") 写入；名字对不上的关系忽略。
世界观向导输出 category，中英文映射到六个枚举值，映射不了的归为 other。
职业向导（wizard.careers）根据世界观生成职业与阶梯，整体覆盖（删除全部职业，级联清掉挂接与挂接历史）。
项目已有职业时，角色向导要求每个角色输出 careers: [{name, stage, role, note}]，按职业名 / 阶段名精确匹配后经
career_service.apply_assignment(source="wizard") 写入；职业对不上 / 超出上限的忽略并写入 done.result.warnings，
阶段名对不上的挂在第 1 阶并给警告。项目没有职业时角色向导的提示词与写入行为不变。
大纲向导（outline-pro）：outline_mode = replace（默认）覆盖前先存大纲快照（wizard_replace），
章节经外键解除关联、不删除；
append 不删旧大纲，提示词附上已有大纲末尾 10 条，写入时卷号接最后一卷、章节号接全书最后一章、sort 接末尾。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import (
    build_careers_wizard_messages,
    build_characters_messages,
    build_outline_messages,
    build_world_messages,
)
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError
from app.core.security import SecretCipher
from app.models import Career, Character, CharacterCareer, Outline, WorldSetting
from app.models.entities import WORLD_CATEGORIES
from app.schemas.career import CareerCreate
from app.schemas.chapter import WizardRequest
from app.schemas.story import CharacterOut, OutlineOut, WorldSettingOut
from app.services import (
    ai_config_service,
    career_service,
    outline_service,
    outline_snapshot_service,
    relationship_service,
    story_service,
)
from app.services.cast_service import is_protagonist_role
from app.services.entity_ai_service import normalize_aliases, normalize_career_assignments, normalize_category
from app.services.generation_service import Event
from app.services.project_service import get_owned_project, project_info

logger = logging.getLogger(__name__)

WizardKind = Literal["outline", "characters", "world", "careers"]
# 追加模式下提示词附上的已有大纲条数（末尾）
APPEND_CONTEXT_OUTLINES = 10


@dataclass
class WizardJob:
    project_id: int
    kind: WizardKind
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    # 仅 outline：replace = 覆盖（先存快照）；append = 追加到末尾
    outline_mode: str = "replace"


async def validate_wizard(
    db: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    req: WizardRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> int:
    """创建任务时同步校验归属、AI 配置与端点；返回解析出的 ai_config_id。不修改任何数据。"""
    await get_owned_project(db, user_id, project_id)
    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    return cfg.id


async def build_wizard_job(
    db: AsyncSession,
    *,
    user_id: int,
    project_id: int,
    kind: WizardKind,
    req: WizardRequest,
    ai_config_id: int | None,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> WizardJob:
    """任务开始运行时按项目最新的世界观 / 角色 / 大纲组装 messages。"""
    project = await get_owned_project(db, user_id, project_id)
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)

    info = project_info(project)
    world_text = story_service.format_world(await story_service.list_items(db, story_service.WORLD, project_id))
    characters_text = story_service.format_characters(
        await story_service.list_items(db, story_service.CHARACTERS, project_id)
    )
    outline_text = story_service.format_outlines(await story_service.list_items(db, story_service.OUTLINES, project_id))

    if kind == "outline":
        append_kwargs: dict[str, Any] = {}
        if req.outline_mode == "append":
            outlines = await story_service.list_items(db, story_service.OUTLINES, project_id)
            if outlines:
                append_kwargs = {
                    "existing_text": story_service.format_outlines(outlines[-APPEND_CONTEXT_OUTLINES:]),
                    "start_volume_no": (await outline_service.last_volume(db, project_id))[0] + 1,
                    "start_chapter_no": await outline_service.next_chapter_no(db, project_id),
                }
        messages = build_outline_messages(
            info,
            volume_count=req.volume_count,
            chapters_per_volume=req.chapters_per_volume,
            world_text=world_text,
            characters_text=characters_text,
            instruction=req.instruction,
            **append_kwargs,
        )
    elif kind == "characters":
        messages = build_characters_messages(
            info,
            character_count=req.character_count,
            world_text=world_text,
            outline_text=outline_text,
            instruction=req.instruction,
            careers_text=await career_service.careers_reference(db, project_id),
        )
    elif kind == "careers":
        messages = build_careers_wizard_messages(
            info, career_count=req.career_count, world_text=world_text, instruction=req.instruction
        )
    else:
        messages = build_world_messages(
            info, outline_text=outline_text, characters_text=characters_text, instruction=req.instruction
        )
    return WizardJob(
        project_id=project_id,
        kind=kind,
        provider=provider,
        model=cfg.model,
        messages=messages,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        outline_mode=req.outline_mode,
    )


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
        role_type = _text(c.get("role_type") or c.get("role"))[:50]
        rows.append(
            {
                "name": name,
                "role_type": role_type,
                "profile": _text(c.get("profile") or c.get("description")),
                # 旧格式的自然语言关系保留为旧关系备注
                "relations_text": _text(c.get("relations_text") or c.get("relations")),
                "aliases": [a for a in normalize_aliases(c.get("aliases")) if a != name],
                "age": _text(c.get("age"))[:50],
                "gender": _text(c.get("gender"))[:20],
                "appearance": _text(c.get("appearance")),
                "personality": _text(c.get("personality")),
                "background": _text(c.get("background")),
                "always_in_context": is_protagonist_role(role_type),
            }
        )
    if not rows:
        raise JSONExtractError("未解析到任何角色")
    for i, r in enumerate(rows, start=1):
        r["sort"] = i
    return rows


def normalize_character_relations(obj: Any) -> list[dict]:
    """角色向导的 relationships（可选）：「a 是 b 的 role_a，b 是 a 的 role_b」。缺两端或称呼的丢弃。"""
    items = obj.get("relationships") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return []
    rows = []
    for r in items:
        if not isinstance(r, dict):
            continue
        a = _text(r.get("a") or r.get("a_name") or r.get("from"))[:100]
        b = _text(r.get("b") or r.get("b_name") or r.get("to"))[:100]
        role_a = _text(r.get("role_a"))[:20]
        role_b = _text(r.get("role_b"))[:20] or role_a
        role_a = role_a or role_b
        if not a or not b or not role_a or a == b:
            continue
        rows.append(
            {
                "a": a,
                "b": b,
                "role_a": role_a,
                "role_b": role_b,
                "category": normalize_category(r.get("category")),
                "intimacy": max(-100, min(100, _int(r.get("intimacy"), 0))),
                "description": _text(r.get("description")),
            }
        )
    return rows


_WORLD_CATEGORY_ALIASES = (
    ("place", ("地点", "地理", "地域", "地图", "场景", "城市", "区域", "location")),
    ("faction", ("势力", "组织", "门派", "宗门", "国家", "阵营", "帮派", "种族")),
    ("item", ("物品", "道具", "法宝", "装备", "神器", "宝物", "artifact")),
    ("rule", ("规则", "体系", "力量", "法则", "科技", "制度", "修炼", "魔法", "system")),
    ("history", ("历史", "事件", "传说", "往事", "纪元", "年表")),
    ("other", ("其他",)),
)


def normalize_world_category(value: Any) -> str:
    v = _text(value).lower()
    if v in WORLD_CATEGORIES:
        return v
    for cat, keys in _WORLD_CATEGORY_ALIASES:
        if any(k in v for k in keys):
            return cat
    return "other"


def normalize_world(obj: Any) -> list[dict]:
    items = _unwrap_list(obj, ("world", "settings", "items", "world_settings"))
    rows = []
    for w in items:
        if not isinstance(w, dict):
            continue
        title = _text(w.get("title") or w.get("name"))[:200]
        if not title:
            continue
        rows.append(
            {
                "title": title,
                "content": _text(w.get("content") or w.get("description")),
                "category": normalize_world_category(w.get("category") or w.get("type")),
            }
        )
    if not rows:
        raise JSONExtractError("未解析到任何世界观条目")
    for i, r in enumerate(rows, start=1):
        r["sort"] = i
    return rows


async def _save_character_relations(
    db: AsyncSession, project_id: int, entities: list[Character], rels: list[dict]
) -> list[Any]:
    """按名字（再按别名）匹配新建的角色并写入关系；对不上 / 指向自己 / 同一对已有关系的跳过。"""
    lookup: dict[str, int] = {}
    for c in entities:
        lookup.setdefault(c.name, c.id)
    for c in entities:
        for a in c.aliases or []:
            lookup.setdefault(a, c.id)
    created = []
    for r in rels:
        a_id, b_id = lookup.get(r["a"]), lookup.get(r["b"])
        if a_id is None or b_id is None or a_id == b_id:
            continue
        if await relationship_service.find_pair(db, a_id, b_id) is not None:
            continue
        created.append(
            await relationship_service.create_relation(
                db,
                project_id,
                a_id=a_id,
                b_id=b_id,
                role_a=r["role_a"],
                role_b=r["role_b"],
                category=r["category"],
                intimacy=r["intimacy"],
                description=r["description"],
                source="wizard",
            )
        )
    return created


def normalize_character_careers(obj: Any) -> list[tuple[str, list[dict[str, Any]]]]:
    """角色向导输出中每个角色的 careers → [(角色名, [{name, stage, role, note}])]。"""
    try:
        items = _unwrap_list(obj, ("characters", "roles", "items"))
    except JSONExtractError:
        return []
    out = []
    for c in items:
        if not isinstance(c, dict):
            continue
        name = _text(c.get("name"))[:100]
        careers = normalize_career_assignments({"careers": c.get("careers")})
        if name and careers:
            out.append((name, careers))
    return out


async def _save_character_careers(
    db: AsyncSession, project_id: int, entities: list[Character], obj: Any
) -> tuple[list[CharacterCareer], list[str]]:
    """按职业名 / 阶段名匹配写入挂接；没有职业的项目直接跳过。返回 (挂接, 警告)。"""
    careers = await career_service.list_project_careers(db, project_id)
    if not careers:
        return [], []
    by_name = {c.name: c for c in careers}
    stages = await career_service.stages_of(db, [c.id for c in careers])
    lookup: dict[str, Character] = {}
    for e in entities:
        lookup.setdefault(e.name, e)
    created: list[CharacterCareer] = []
    warnings: list[str] = []
    for char_name, items in normalize_character_careers(obj):
        character = lookup.get(char_name)
        if character is None:
            continue
        for r in items:
            career = by_name.get(r["name"])
            if career is None:
                warnings.append(f"角色「{char_name}」的职业「{r['name']}」不存在，已忽略")
                continue
            ladder = stages.get(career.id, [])
            stage = next((s for s in ladder if s.name == r["stage"]), ladder[0] if ladder else None)
            if stage is None:
                continue
            if r["stage"] and stage.name != r["stage"]:
                warnings.append(
                    f"角色「{char_name}」的职业「{career.name}」没有名为「{r['stage']}」的阶段，"
                    f"已设为第 1 阶「{stage.name}」"
                )
            try:
                created.append(
                    await career_service.assign_new(
                        db, character, career, stage_id=stage.id, note=r["note"], source="wizard"
                    )
                )
            except AppError as exc:
                warnings.append(f"角色「{char_name}」的职业「{career.name}」未能挂接：{exc.detail}")
    return created, warnings


async def _save_careers(db: AsyncSession, project_id: int, obj: Any) -> dict[str, Any]:
    drafts, warnings = career_service.normalize_career_drafts(obj)
    if not drafts:
        raise JSONExtractError("未解析到任何职业")
    await db.execute(delete(Career).where(Career.project_id == project_id))
    careers = [
        await career_service.new_career(db, project_id, CareerCreate.model_validate({**d, "sort": i}))
        for i, d in enumerate(drafts, start=1)
    ]
    await db.commit()
    for c in careers:
        await db.refresh(c)
    outs = await career_service.career_outs(db, careers)
    return {"items": [o.model_dump(mode="json") for o in outs], "warnings": warnings}


_KIND_SPEC = {
    "outline": (Outline, normalize_outline, OutlineOut),
    "characters": (Character, normalize_characters, CharacterOut),
    "world": (WorldSetting, normalize_world, WorldSettingOut),
}


async def _prepare_outline_rows(db: AsyncSession, project_id: int, rows: list[dict], outline_mode: str) -> None:
    """大纲写入前：覆盖模式先存快照（已有大纲时）并删除旧大纲；追加模式把卷号 / 章节号 / 排序接到末尾。"""
    if outline_mode != "append":
        existing = await story_service.list_items(db, story_service.OUTLINES, project_id)
        if existing:
            await outline_snapshot_service.save_snapshot(db, project_id, "wizard_replace", "大纲向导覆盖前")
        await db.execute(delete(Outline).where(Outline.project_id == project_id))
        return
    last_volume = (await outline_service.last_volume(db, project_id))[0]
    start_no = await outline_service.next_chapter_no(db, project_id)
    start_sort = await story_service.next_sort(db, Outline, project_id)
    volumes: dict[int, int] = {}
    for i, r in enumerate(rows):
        volumes.setdefault(r["volume_no"], last_volume + len(volumes) + 1)
        r["volume_no"] = volumes[r["volume_no"]]
        r["chapter_no"] = start_no + i
        r["sort"] = start_sort + i


async def save_wizard_result(
    db: AsyncSession, project_id: int, kind: WizardKind, raw_text: str, *, outline_mode: str = "replace"
) -> dict[str, Any]:
    """解析模型输出并写入，返回 done 载荷 {items[, relationships, assignments], warnings?}。

    大纲覆盖模式（默认）与其他类型一样覆盖旧数据（覆盖前存大纲快照）；大纲追加模式不删旧数据，items 只含新增条目。
    """
    obj = extract_json(raw_text)
    if kind == "careers":
        return await _save_careers(db, project_id, obj)
    model, normalize, out_schema = _KIND_SPEC[kind]
    rows = normalize(obj)
    if kind == "outline":
        await _prepare_outline_rows(db, project_id, rows, outline_mode)
    else:
        await db.execute(delete(model).where(model.project_id == project_id))
    entities = [model(project_id=project_id, **row) for row in rows]
    db.add_all(entities)
    await db.flush()
    rels = []
    assignments: list[CharacterCareer] = []
    warnings: list[str] = []
    if kind == "characters":
        rels = await _save_character_relations(db, project_id, entities, normalize_character_relations(obj))
        assignments, warnings = await _save_character_careers(db, project_id, entities, obj)
    await db.commit()
    for e in entities:
        await db.refresh(e)
    payload: dict[str, Any] = {"items": [out_schema.model_validate(e).model_dump(mode="json") for e in entities]}
    if kind == "outline":
        payload["mode"] = outline_mode
    if kind == "characters":
        outs = await relationship_service.to_outs(db, rels)
        payload["relationships"] = [o.model_dump(mode="json") for o in outs]
        for a in assignments:
            await db.refresh(a)
        payload["assignments"] = [
            o.model_dump(mode="json") for o in await career_service.assignment_outs(db, assignments)
        ]
        payload["warnings"] = warnings
    return payload


async def run_wizard_job(job: WizardJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    pieces: list[str] = []
    try:
        stream_iter = job.provider.generate_stream(
            job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
        )
        async with aclosing(stream_iter) as stream:
            async for piece in stream:
                pieces.append(piece)
                yield "delta", {"text": piece}
        raw = "".join(pieces)
        if not raw.strip():
            raise AIProviderError("模型未返回任何内容")
        async with sessionmaker() as db:
            payload = await save_wizard_result(db, job.project_id, job.kind, raw, outline_mode=job.outline_mode)
        yield "done", {"kind": job.kind, **payload}
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except Exception:  # noqa: BLE001
        logger.exception("向导生成失败 project=%s kind=%s", job.project_id, job.kind)
        yield "error", {"detail": "生成失败：服务器内部错误"}
