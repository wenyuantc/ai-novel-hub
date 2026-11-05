"""大纲导入导出（ainovel.outline v1，outline-pro R5）。

格式：{format: "ainovel.outline", version: 1, volumes: [{volume_no, volume_title,
outlines: [{chapter_no, title, summary,
plans: [{title, summary, key_events, characters: [名字], emotion, goal, est_words}]}]}]}。不含任何内部 id。

- 导入 / 预览的 body 为 {data: <原样 JSON>}；大小按紧凑序列化（与前端 JSON.stringify 一致）的 UTF-8 字节计，上限 2MB。
- 卷号与章节号在导入时重新编排：卷按文件中的顺序依次编号；章节号连续递增（导入的细纲尚未建章，每条章纲占 1 个章节号）。
  append：卷号接在当前最后一卷之后、章节号接在全书最后一章之后；replace：先存快照（import_replace），
  删除全部大纲（细纲级联删除，已写章节只解除关联），卷号与章节号从 1 开始。
- 出场角色按名字、再按别名精确匹配目标项目的角色；匹配不上的忽略并写入 warnings / unresolved_characters。
"""

import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import ChapterPlan, Character, Outline
from app.schemas.outline import (
    MAX_EST_WORDS,
    MAX_OUTLINE_FILE_BYTES,
    MIN_EST_WORDS,
    OUTLINE_FILE_FORMAT,
    OUTLINE_FILE_VERSION,
    OutlineFile,
    OutlineImportPreview,
    OutlineImportRequest,
    OutlineImportResult,
    clean_key_events,
)
from app.schemas.story import OutlineOut
from app.services import outline_service, outline_snapshot_service, plan_service, planning_lifecycle, story_service
from app.services.entity_ai_service import resolve_name
from app.services.project_service import get_owned_project


async def export_outline(db: AsyncSession, user_id: int, project_id: int) -> dict[str, Any]:
    await get_owned_project(db, user_id, project_id)
    outlines = await outline_service.list_outlines(db, project_id)
    plans = await plan_service.plans_of(db, [o.id for o in outlines])
    names = dict(
        (await db.execute(select(Character.id, Character.name).where(Character.project_id == project_id))).all()
    )
    volumes: list[dict[str, Any]] = []
    for o in outlines:
        if not volumes or volumes[-1]["volume_no"] != o.volume_no:
            volumes.append({"volume_no": o.volume_no, "volume_title": o.volume_title, "outlines": []})
        elif not volumes[-1]["volume_title"] and o.volume_title:
            volumes[-1]["volume_title"] = o.volume_title
        volumes[-1]["outlines"].append(
            {
                "chapter_no": o.chapter_no,
                "title": o.title,
                "summary": o.summary,
                "plans": [
                    {
                        "title": p.title,
                        "summary": p.summary,
                        "key_events": list(p.key_events or []),
                        "characters": [names[c] for c in p.character_ids or [] if c in names],
                        "emotion": p.emotion,
                        "goal": p.goal,
                        "est_words": p.est_words,
                    }
                    for p in plans.get(o.id, [])
                ],
            }
        )
    return {"format": OUTLINE_FILE_FORMAT, "version": OUTLINE_FILE_VERSION, "volumes": volumes}


def parse_outline_file(data: Any) -> OutlineFile:
    """校验大小、format、version 与结构；错误一律 400 中文提示。"""
    try:
        size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise BadRequestError("大纲文件格式不正确") from exc
    if size > MAX_OUTLINE_FILE_BYTES:
        raise BadRequestError("大纲文件过大，上限为 2MB")
    if not isinstance(data, dict):
        raise BadRequestError("大纲文件格式不正确：应为 JSON 对象")
    if data.get("format") != OUTLINE_FILE_FORMAT:
        raise BadRequestError(f"不是有效的大纲文件（format 应为 {OUTLINE_FILE_FORMAT}）")
    version = data.get("version")
    if isinstance(version, bool) or version != OUTLINE_FILE_VERSION:
        raise BadRequestError(f"不支持的大纲文件版本：{version}（当前支持 {OUTLINE_FILE_VERSION}）")
    try:
        parsed = OutlineFile.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(x) for x in err.get("loc", ()))
        raise BadRequestError(f"大纲文件格式不正确：{loc}: {err.get('msg', '')}") from exc
    if not any(v.outlines for v in parsed.volumes):
        raise BadRequestError("大纲文件中没有任何章纲")
    return parsed


def _resolve_characters(
    parsed: OutlineFile, characters: list[Character]
) -> tuple[dict[str, int], list[str], list[str]]:
    """名字 → 角色 id；返回 (映射, 匹配不上的名字, 警告)。"""
    mapping: dict[str, int] = {}
    unresolved: list[str] = []
    warnings: list[str] = []
    for v in parsed.volumes:
        for o in v.outlines:
            for p in o.plans:
                for raw in p.characters:
                    name = raw.strip()
                    if not name or name in mapping or name in unresolved:
                        continue
                    cid, reason = resolve_name(name, characters)
                    if cid is None:
                        unresolved.append(name)
                        warnings.append(f"{reason}，已忽略")
                    else:
                        mapping[name] = cid
    return mapping, unresolved, warnings


def _counts(parsed: OutlineFile) -> tuple[int, int, int]:
    volumes = len([v for v in parsed.volumes if v.outlines])
    outlines = sum(len(v.outlines) for v in parsed.volumes)
    plans = sum(len(o.plans) for v in parsed.volumes for o in v.outlines)
    return volumes, outlines, plans


async def preview_import(db: AsyncSession, user_id: int, project_id: int, data: Any) -> OutlineImportPreview:
    await get_owned_project(db, user_id, project_id)
    parsed = parse_outline_file(data)
    characters = await story_service.list_items(db, story_service.CHARACTERS, project_id)
    _, unresolved, warnings = _resolve_characters(parsed, characters)
    volumes, outlines, plans = _counts(parsed)
    return OutlineImportPreview(
        volumes=volumes, outlines=outlines, plans=plans, unresolved_characters=unresolved, warnings=warnings
    )


async def import_outline(
    db: AsyncSession, user_id: int, project_id: int, req: OutlineImportRequest
) -> OutlineImportResult:
    await get_owned_project(db, user_id, project_id, lock=True)
    parsed = parse_outline_file(req.data)
    characters = await story_service.list_items(db, story_service.CHARACTERS, project_id)
    mapping, unresolved, warnings = _resolve_characters(parsed, characters)

    snapshot_id: int | None = None
    if req.mode == "replace":
        snap = await outline_snapshot_service.save_snapshot(db, project_id, "import_replace", "导入替换前")
        snapshot_id = snap.id
        await planning_lifecycle.detach_outlines(db, project_id)
        await db.execute(delete(Outline).where(Outline.project_id == project_id))
        volume_base, chapter_no, sort = 0, 1, 1
    else:
        volume_base = (await outline_service.last_volume(db, project_id))[0]
        chapter_no = await outline_service.next_chapter_no(db, project_id)
        sort = await story_service.next_sort(db, Outline, project_id)

    created: list[Outline] = []
    plans_created = 0
    volume_index = 0
    for vol in parsed.volumes:
        if not vol.outlines:
            continue
        volume_index += 1
        for item in vol.outlines:
            outline = Outline(
                project_id=project_id,
                volume_no=volume_base + volume_index,
                volume_title=vol.volume_title.strip(),
                chapter_no=chapter_no,
                title=item.title.strip() or "未命名",
                summary=item.summary.strip(),
                sort=sort,
            )
            db.add(outline)
            await db.flush()
            created.append(outline)
            sort += 1
            chapter_no += 1
            for seq, p in enumerate(item.plans, start=1):
                ids = [mapping[n.strip()] for n in p.characters if n.strip() in mapping]
                db.add(
                    ChapterPlan(
                        project_id=project_id,
                        outline_id=outline.id,
                        seq=seq,
                        title=p.title.strip() or f"第{seq}节",
                        summary=p.summary.strip(),
                        key_events=clean_key_events(p.key_events),
                        character_ids=list(dict.fromkeys(ids)),
                        emotion=p.emotion.strip(),
                        goal=p.goal.strip(),
                        est_words=max(MIN_EST_WORDS, min(MAX_EST_WORDS, p.est_words)),
                    )
                )
                plans_created += 1
    await planning_lifecycle.bump_revision(db, project_id)
    await db.commit()
    for o in created:
        await db.refresh(o)
    return OutlineImportResult(
        mode=req.mode,
        volumes=volume_index,
        outlines=len(created),
        plans=plans_created,
        snapshot_id=snapshot_id,
        unresolved_characters=unresolved,
        warnings=warnings,
        items=[OutlineOut.model_validate(o) for o in created],
    )
