"""正文工具仅提供原文唯一匹配的局部替换；生成全文必须走任务。"""

from pydantic import Field
from sqlalchemy import select, update

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter
from app.services import analysis_service, chapter_service, version_service

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
    first = chapter.content.find(args.original)
    if first < 0 or chapter.content.find(args.original, first + 1) >= 0:
        raise BadRequestError("原文未找到或不唯一，请扩大或缩小引用范围")
    return chapter


async def preview(ctx: ToolCtx, args: ReplaceArgs) -> dict:
    chapter = await _chapter(ctx, args)
    return {
        "title": "局部替换章节正文",
        "target": f"第{chapter.chapter_no}章 {chapter.title}",
        "diff": [{"field": "content", "label": "正文片段", "before": args.original, "after": args.replacement}],
        "warnings": [],
        # 完整正文变化也要求重新确认，避免只比引用片段漏掉相邻编辑。
        "content_hash": analysis_service.content_hash(chapter.content),
    }


async def execute(ctx: ToolCtx, args: ReplaceArgs) -> dict:
    chapter = await _chapter(ctx, args)
    before_content = chapter.content
    await version_service.ensure_baseline(ctx.db, chapter)
    before = await version_service.get_latest(ctx.db, chapter.id)
    if before is None or before.content != before_content:
        before = await version_service.record(ctx.db, chapter, "baseline")
    content = before_content.replace(args.original, args.replacement, 1)
    result = await ctx.db.execute(
        update(Chapter)
        .where(
            Chapter.id == chapter.id,
            Chapter.project_id == ctx.project_id,
            Chapter.status != "generating",
            Chapter.content == before_content,
        )
        .values(content=content, word_count=chapter_service.count_words(content))
    )
    if result.rowcount != 1:
        raise ConflictError("正文同时被其他操作修改，请刷新后重试")
    await ctx.db.refresh(chapter)
    after = await version_service.record(ctx.db, chapter, "agent", note="智能体局部替换")
    return {
        "chapter_id": chapter.id,
        "before_version_id": before.id,
        "after_version_id": after.id,
        "before_hash": analysis_service.content_hash(before_content),
        "after_hash": analysis_service.content_hash(content),
        "word_count": chapter.word_count,
    }


TOOLS = [Tool("replace_chapter_text", "局部替换章节正文并保存版本", ReplaceArgs, "text", execute, preview)]
