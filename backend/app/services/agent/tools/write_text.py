"""正文工具仅提供原文唯一匹配的局部替换；生成全文必须走任务。"""

import json

from pydantic import Field
from sqlalchemy import select

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter
from app.services import analysis_service, generation_service, scene_service, version_service

from .base import EmptyArgs, Tool, ToolCtx


class ReplaceArgs(EmptyArgs):
    chapter_id: int = Field(gt=0)
    original: str = Field(min_length=1, max_length=2000)
    replacement: str = Field(max_length=5000)


async def _chapter(ctx: ToolCtx, args: ReplaceArgs) -> Chapter:
    chapter = await ctx.db.scalar(
        select(Chapter)
        .where(
            Chapter.id == args.chapter_id,
            Chapter.project_id == ctx.project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if chapter is None:
        raise NotFoundError("不存在")
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")
    await generation_service.ensure_outside_batch(ctx.db, chapter)
    first = chapter.content.find(args.original)
    if first < 0 or chapter.content.find(args.original, first + 1) >= 0:
        raise BadRequestError("原文未找到或不唯一，请扩大或缩小引用范围")
    await scene_service.resolve_chapter_range(ctx.db, chapter, first, first + len(args.original))
    return chapter


async def preview(ctx: ToolCtx, args: ReplaceArgs) -> dict:
    chapter = await _chapter(ctx, args)
    first = chapter.content.find(args.original)
    scene, start, end = await scene_service.resolve_chapter_range(
        ctx.db, chapter, first, first + len(args.original),
    )
    return {
        "title": "局部替换章节正文",
        "target": f"第{chapter.chapter_no}章 {chapter.title}",
        "diff": [{"field": "content", "label": "正文片段", "before": args.original, "after": args.replacement}],
        "warnings": [],
        # 完整正文变化也要求重新确认，避免只比引用片段漏掉相邻编辑。
        "content_hash": analysis_service.content_hash(chapter.content),
        "scene_id": scene.id, "start": start, "end": end, "content_revision": chapter.content_revision,
        "structure_hash": analysis_service.content_hash(json.dumps(
            await scene_service.capture_snapshot(ctx.db, chapter), ensure_ascii=False, sort_keys=True,
        )),
    }


async def execute(ctx: ToolCtx, args: ReplaceArgs) -> dict:
    chapter = await _chapter(ctx, args)
    before_content = chapter.content
    await version_service.ensure_baseline(ctx.db, chapter)
    before = await version_service.get_latest(ctx.db, chapter.id)
    if before is None or before.content != before_content:
        before = await version_service.record(ctx.db, chapter, "baseline")
    first = before_content.find(args.original)
    scene, start, end = await scene_service.resolve_chapter_range(
        ctx.db, chapter, first, first + len(args.original),
    )
    await scene_service.replace_scene_content(
        ctx.db, chapter, scene, scene.content[:start] + args.replacement + scene.content[end:],
        expected_revision=chapter.content_revision,
    )
    content = chapter.content
    after = await version_service.record(ctx.db, chapter, "agent", note="智能体局部替换")
    return {
        "chapter_id": chapter.id,
        "scene_id": scene.id,
        "before_version_id": before.id,
        "after_version_id": after.id,
        "before_hash": analysis_service.content_hash(before_content),
        "after_hash": analysis_service.content_hash(content),
        "word_count": chapter.word_count,
    }


TOOLS = [Tool("replace_chapter_text", "局部替换章节正文并保存版本", ReplaceArgs, "text", execute, preview)]
