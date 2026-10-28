"""批量顺序生成（后台任务 chapter.batch）：范围求值、预览、校验、断点与逐章执行循环。

- 整个批量是一个任务（resource_key = batch:{project_id}）。范围在发起时按 (chapter_no, id) 定下章节 id 列表
  （input.chapter_ids，最多 MAX_CHAPTERS 章），之后插入到范围内的章节不纳入。
- step_cursor 指向下一章在 chapter_ids 中的下标；checkpoint 见 new_checkpoint。每推进一个阶段单独提交一次，
  中断后最多重做当前章的当前阶段；重试（failed / cancelled / interrupted）与继续（paused）都从断点接着跑。
- 逐章复用单章实现：generation_service.claim_chapter → build_chapter_job → run_chapter_job（失败时其自身回退章节状态、
  写生成记录；成功时存 baseline + generate 版本）；分析复用 analysis_service.build_analysis_job → run_analysis_job
  （回写随分析落库）。分析在本任务内串行执行，不另外排队、不占额外并发名额；部署开关或项目开关关闭时跳过。
- 每章 build_job 在循环里临时组装，所以第 N 章的上下文看到的是第 N-1 章正文与分析回写之后的数据库状态。
- 审核点：每生成 review_every 章且后面还有要处理的章节时产出 paused，任务转为待审核（不占名额、不锁范围）。
- 单章生成失败重试 MAX_RETRIES 次（退避 RETRY_DELAYS 秒）；仍失败时 skip_on_fail 决定跳过继续还是任务失败。
"""

import asyncio
import copy
import logging
import math
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError
from app.models import Chapter, ChapterPlan, Project, Task
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.batch import BatchGenerateRequest, BatchPreview, BatchPreviewItem, BatchSkippedItem
from app.schemas.chapter import GenerateChapterRequest
from app.services import (
    ai_config_service,
    analysis_service,
    generation_service,
    kb_index_service,
    skill_service,
    style_service,
)
from app.services.project_service import get_owned_project

if TYPE_CHECKING:
    from app.services.task_engine.registry import Event, TaskContext

logger = logging.getLogger(__name__)

KIND = "chapter.batch"
MAX_CHAPTERS = 200
MAX_RETRIES = 2
# 单章失败后第 1 / 2 次重试前的等待秒数（测试中改为 0）
RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)
# 预估耗时：每 3000 字约 2 分钟，开启分析时每章另加 1 分钟
MINUTES_PER_3000 = 2.0
ANALYZE_MINUTES = 1.0

OPEN_BATCH_CONFLICT = "已有进行中或待审核的批量生成"
NOTHING_TO_GENERATE = "范围内没有需要生成的章节"

ITEM_STATES = ("pending", "done", "skipped_written", "skipped_missing", "failed")


def resource_key(project_id: int) -> str:
    return f"batch:{project_id}"


# ---------------- 范围与字数 ----------------


async def _range_chapters(
    db: AsyncSession, project_id: int, req: BatchGenerateRequest
) -> tuple[list[Chapter], Chapter, Chapter]:
    ends = {
        c.id: c
        for c in await db.scalars(
            select(Chapter).where(
                Chapter.project_id == project_id, Chapter.id.in_((req.start_chapter_id, req.end_chapter_id))
            )
        )
    }
    start, end = ends.get(req.start_chapter_id), ends.get(req.end_chapter_id)
    if start is None or end is None:
        raise BadRequestError("起止章节不存在或不属于该项目")
    if start.chapter_no > end.chapter_no:
        raise BadRequestError("起始章节不能晚于结束章节")
    chapters = list(
        await db.scalars(
            select(Chapter)
            .where(
                Chapter.project_id == project_id,
                Chapter.chapter_no >= start.chapter_no,
                Chapter.chapter_no <= end.chapter_no,
            )
            .order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    if len(chapters) > MAX_CHAPTERS:
        raise BadRequestError(f"一次批量最多 {MAX_CHAPTERS} 章，当前范围有 {len(chapters)} 章")
    return chapters, start, end


async def plan_words(db: AsyncSession, chapters: list[Chapter]) -> dict[int, int]:
    """章节 id → 细纲 est_words（只含有细纲的章节）。"""
    plan_ids = {c.plan_id: c.id for c in chapters if c.plan_id is not None}
    if not plan_ids:
        return {}
    rows = await db.execute(select(ChapterPlan.id, ChapterPlan.est_words).where(ChapterPlan.id.in_(plan_ids)))
    return {plan_ids[pid]: words for pid, words in rows.all()}


def target_for(req: BatchGenerateRequest, chapter_id: int, plans: dict[int, int]) -> tuple[int, str]:
    """(目标字数, 来源 plan | uniform)：勾选使用细纲字数且章节有细纲时取 est_words，否则统一字数。"""
    if req.use_plan_words and chapter_id in plans:
        return max(300, min(20000, int(plans[chapter_id] or req.target_words))), "plan"
    return req.target_words, "uniform"


def is_written(chapter: Chapter) -> bool:
    return bool(chapter.content.strip())


def _chapter_nos(chapters: list[Chapter]) -> str:
    return "、".join(str(c.chapter_no) for c in chapters)


def batch_title(first_no: int, last_no: int) -> str:
    return f"批量生成 第{first_no}章" if first_no == last_no else f"批量生成 第{first_no}–{last_no}章"


async def open_batch(db: AsyncSession, project_id: int, *, exclude_id: int | None = None) -> Task | None:
    stmt = select(Task).where(Task.project_id == project_id, Task.kind == KIND, Task.status.in_(OPEN_TASK_STATUSES))
    if exclude_id is not None:
        stmt = stmt.where(Task.id != exclude_id)
    return await db.scalar(stmt.limit(1))


def analyze_enabled(settings: Any, project: Project) -> bool:
    return bool(settings.auto_analyze_enabled and project.auto_analyze)


# ---------------- 预览 ----------------


async def preview(
    db: AsyncSession, settings: Any, user_id: int, project_id: int, req: BatchGenerateRequest
) -> BatchPreview:
    project = await get_owned_project(db, user_id, project_id)
    chapters, _, _ = await _range_chapters(db, project.id, req)
    plans = await plan_words(db, chapters)
    analyze = analyze_enabled(settings, project)
    items: list[BatchPreviewItem] = []
    skipped: list[BatchSkippedItem] = []
    for c in chapters:
        if is_written(c) and not req.include_written:
            skipped.append(
                BatchSkippedItem(chapter_id=c.id, chapter_no=c.chapter_no, title=c.title, word_count=c.word_count)
            )
            continue
        words, source = target_for(req, c.id, plans)
        items.append(
            BatchPreviewItem(
                chapter_id=c.id,
                chapter_no=c.chapter_no,
                title=c.title,
                target_words=words,
                words_source=source,  # type: ignore[arg-type]
                written=is_written(c),
            )
        )
    minutes = sum(i.target_words / 3000 * MINUTES_PER_3000 + (ANALYZE_MINUTES if analyze else 0) for i in items)
    warnings: list[str] = []
    if not items:
        warnings.append(NOTHING_TO_GENERATE)
    generating = [c for c in chapters if c.status == "generating"]
    if generating:
        warnings.append(f"第 {_chapter_nos(generating)} 章正在生成中，请等待其完成后再发起")
    outline_ids = {c.outline_id for c in chapters if c.outline_id is not None}
    if outline_ids:
        unbuilt = await db.scalar(
            select(ChapterPlan.id)
            .where(ChapterPlan.outline_id.in_(outline_ids), ChapterPlan.chapter_id.is_(None))
            .limit(1)
        )
        if unbuilt is not None:
            warnings.append("范围内有细纲还没建章，批量不会自动建章，请先到大纲页建章")
    if not analyze:
        warnings.append("项目已关闭自动分析，批量过程中不做分析")
    if await open_batch(db, project.id) is not None:
        warnings.append(OPEN_BATCH_CONFLICT)
    return BatchPreview(
        total=len(chapters),
        items=items,
        skipped=skipped,
        analyze=analyze,
        estimated_minutes=math.ceil(minutes),
        warnings=warnings,
    )


# ---------------- 校验（创建 / 重试 / 继续） ----------------


async def _check_config(db: AsyncSession, ctx: "TaskContext", user_id: int, ai_config_id: int | None) -> int:
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    # 只做校验（解密 + 端点防护），每章运行时再按最新配置重新构建
    await ai_config_service.build_provider(
        cfg,
        ctx.cipher,
        ctx.factory,
        ctx.settings.ai_request_timeout,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    return cfg.id


async def _check_not_generating(db: AsyncSession, chapter_ids: list[int], *, include_written: bool) -> None:
    if not chapter_ids:
        return
    chapters = list(
        await db.scalars(
            select(Chapter)
            .where(Chapter.id.in_(chapter_ids), Chapter.status == "generating")
            .order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    busy = [c for c in chapters if include_written or not is_written(c)]
    if busy:
        raise ConflictError(f"范围内有章节正在生成中：第 {_chapter_nos(busy)} 章，请等待其完成后再试")


async def validate_start(
    db: AsyncSession, ctx: "TaskContext", *, user_id: int, project_id: int, req: BatchGenerateRequest
) -> dict[str, Any]:
    """发起前同步校验；返回 TaskSpec 所需字段。不修改数据。"""
    project = await get_owned_project(db, user_id, project_id)
    chapters, _, _ = await _range_chapters(db, project.id, req)
    ai_config_id = await _check_config(db, ctx, user_id, req.ai_config_id)
    await style_service.validate_choice(db, user_id, req.style)
    # Skill 快照：之后编辑 Skill 不影响续跑与重试（重试复制 input）
    skill = await skill_service.snapshot(db, user_id, req.skill_id)
    if not any(req.include_written or not is_written(c) for c in chapters):
        raise BadRequestError(NOTHING_TO_GENERATE)
    await _check_not_generating(db, [c.id for c in chapters], include_written=req.include_written)
    if await open_batch(db, project.id) is not None:
        raise ConflictError(OPEN_BATCH_CONFLICT)
    return {
        "project_id": project.id,
        "title": batch_title(chapters[0].chapter_no, chapters[-1].chapter_no),
        "input": {
            "request": {"project_id": project.id, **req.model_dump(mode="json")},
            "chapter_ids": [c.id for c in chapters],
            "ai_config_id": ai_config_id,
            "skill": skill,
        },
    }


async def revalidate(db: AsyncSession, ctx: "TaskContext", task: Task) -> int:
    """重试 / 继续前：项目、模型配置仍有效，剩余章节没有在生成中，项目没有其他未结束的批量。返回 ai_config_id。"""
    req = BatchGenerateRequest.model_validate(task.input.get("request") or {})
    project = await get_owned_project(db, task.user_id, int(task.project_id or 0))
    ai_config_id = await _check_config(db, ctx, task.user_id, req.ai_config_id)
    await style_service.validate_choice(db, task.user_id, req.style)
    remaining = list(task.input.get("chapter_ids") or [])[task.step_cursor :]
    await _check_not_generating(db, remaining, include_written=req.include_written)
    if await open_batch(db, project.id, exclude_id=task.id) is not None:
        raise ConflictError(OPEN_BATCH_CONFLICT)
    return ai_config_id


# ---------------- 断点与汇总 ----------------


def new_checkpoint(chapters: dict[int, Chapter], chapter_ids: list[int]) -> dict[str, Any]:
    """checkpoint 结构：phase（cursor 所指章节的阶段：generate | analyze）、current（正在生成的章节与抢占前状态，
    供 on_abort 回退）、since_review（距上个审核点已生成的章数）、items（每章结果，与 chapter_ids 一一对应）。"""
    items = []
    for cid in chapter_ids:
        c = chapters.get(cid)
        items.append(
            {
                "chapter_id": cid,
                "chapter_no": c.chapter_no if c else None,
                "title": c.title if c else "",
                "state": "pending",
                "target_words": 0,
                "words": 0,
                "analysis": None,
                "warning": "",
                "error": "",
            }
        )
    return {"phase": "generate", "current": None, "since_review": 0, "items": items}


def summarize(checkpoint: dict[str, Any] | None, cursor: int, total: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = list((checkpoint or {}).get("items") or [])

    def brief(item: dict[str, Any], key: str) -> dict[str, Any]:
        return {k: item.get(k) for k in ("chapter_id", "chapter_no", "title")} | {key: item.get(key) or ""}

    counts = {s: sum(1 for i in items if i.get("state") == s) for s in ITEM_STATES}
    warnings = [f"第 {i.get('chapter_no')} 章：{i['warning']}" for i in items if i.get("warning")]
    return {
        "total": total,
        "done": counts["done"],
        "skipped_written": counts["skipped_written"],
        "skipped_missing": counts["skipped_missing"],
        "failed": [brief(i, "error") for i in items if i.get("state") == "failed"],
        "analysis_failed": [brief(i, "warning") for i in items if i.get("analysis") == "failed"],
        "remaining": max(0, total - cursor),
        "warnings": warnings,
    }


def finish_result(task: Task) -> dict[str, Any]:
    total = len(task.input.get("chapter_ids") or [])
    return {**summarize(task.checkpoint, task.step_cursor, total), "ended_early": True}


# ---------------- 执行 ----------------


@dataclass
class BatchJob:
    task_id: int
    user_id: int
    project_id: int
    req: BatchGenerateRequest
    chapter_ids: list[int]
    cursor: int
    checkpoint: dict[str, Any]
    # 进度估算：当前章下标、目标字数与已报告过的最大进度（保证单调）
    position: int = 0
    target: int = 3000
    last_progress: int = 0
    warnings: list[str] = field(default_factory=list)
    # 创建任务时的 Skill 快照（tasks.input.skill）
    skill: dict[str, Any] | None = None

    @property
    def total(self) -> int:
        return len(self.chapter_ids)


async def build_batch_job(ctx: "TaskContext", task: Task) -> BatchJob:
    """只读出请求、断点；每章的 ChapterJob 在循环中按最新数据组装。"""
    req = BatchGenerateRequest.model_validate(task.input.get("request") or {})
    chapter_ids = [int(i) for i in task.input.get("chapter_ids") or []]
    async with ctx.sessionmaker() as db:
        project = await get_owned_project(db, task.user_id, int(task.project_id or 0))
        checkpoint = copy.deepcopy(task.checkpoint) if task.checkpoint else None
        if not checkpoint or len(checkpoint.get("items") or []) != len(chapter_ids):
            rows = await db.scalars(select(Chapter).where(Chapter.id.in_(chapter_ids)))
            checkpoint = new_checkpoint({c.id: c for c in rows}, chapter_ids)
    # 上次运行遗留的 current 已由 on_abort 回退；从断点重新领取
    checkpoint["current"] = None
    checkpoint.setdefault("phase", "generate")
    checkpoint.setdefault("since_review", 0)
    return BatchJob(
        task_id=task.id,
        user_id=task.user_id,
        project_id=project.id,
        req=req,
        chapter_ids=chapter_ids,
        cursor=min(task.step_cursor, len(chapter_ids)),
        checkpoint=checkpoint,
        position=task.step_cursor,
        skill=task.input.get("skill"),
    )


async def _write_checkpoint(db: AsyncSession, job: BatchJob) -> None:
    """写入断点（不 commit）。"""
    await db.execute(
        update(Task)
        .where(Task.id == job.task_id)
        .values(step_cursor=job.cursor, checkpoint=copy.deepcopy(job.checkpoint))
    )


async def _save(ctx: "TaskContext", job: BatchJob) -> None:
    async with ctx.sessionmaker() as db:
        await _write_checkpoint(db, job)
        await db.commit()


async def restore_current(ctx: "TaskContext", task_id: int) -> None:
    """on_abort：断点中记录的当前章仍处于 generating 时还原为抢占前状态（幂等）。"""
    async with ctx.sessionmaker() as db:
        checkpoint = await db.scalar(select(Task.checkpoint).where(Task.id == task_id))
        current = (checkpoint or {}).get("current")
        if not current:
            return
        await db.execute(
            update(Chapter)
            .where(Chapter.id == int(current["chapter_id"]), Chapter.status == "generating")
            .values(status=current.get("prev_status") or "draft")
        )
        await db.commit()


def progress(job: BatchJob, words: int) -> int:
    fraction = min(1.0, words / max(job.target, 1)) * 0.8
    value = int((job.position + fraction) * 100 / max(job.total, 1))
    job.last_progress = max(job.last_progress, max(1, min(95, value)))
    return job.last_progress


def _stage(job: BatchJob, index: int, text: str) -> "Event":
    return "stage", {"stage": f"第 {index + 1}/{job.total} 章 · {text}"}


async def _needs_work(ctx: "TaskContext", job: BatchJob, start: int) -> bool:
    """start 之后是否还有需要生成的章节（审核点只在后面还有活时暂停）。"""
    ids = job.chapter_ids[start:]
    if not ids:
        return False
    if job.req.include_written:
        async with ctx.sessionmaker() as db:
            return await db.scalar(select(Chapter.id).where(Chapter.id.in_(ids)).limit(1)) is not None
    async with ctx.sessionmaker() as db:
        rows = await db.scalars(select(Chapter.content).where(Chapter.id.in_(ids)))
        return any(not (c or "").strip() for c in rows)


async def _generate_chapter(
    ctx: "TaskContext", job: BatchJob, index: int, item: dict[str, Any]
) -> AsyncIterator["Event"]:
    """生成一章（含重试）；结果写入 item（state done / failed、words、warning、error）。"""
    cid = job.chapter_ids[index]
    settings = ctx.settings
    gen_req = GenerateChapterRequest(
        ai_config_id=job.req.ai_config_id,
        instruction=job.req.instruction,
        target_words=item["target_words"],
        auto_continue=job.req.auto_continue,
        style=job.req.style,
    )
    attempt = 0
    error = ""
    while True:
        if attempt:
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)] if RETRY_DELAYS else 0
            yield _stage(job, index, f"生成失败，第 {attempt} 次重试")
            if delay > 0:
                await asyncio.sleep(delay)
            yield "reset", {}
        attempt += 1
        retryable = True
        result: dict[str, Any] | None = None
        try:
            async with ctx.sessionmaker() as db:
                chapter = await db.get(Chapter, cid)
                if chapter is None or chapter.project_id != job.project_id:
                    retryable = False
                    raise BadRequestError("章节已被删除")
                prev_status = await generation_service.claim_chapter(db, chapter)
                # 抢占与记录 current 同一事务提交：崩溃时 recover → on_abort 一定能按断点还原该章
                job.checkpoint["current"] = {"chapter_id": cid, "prev_status": prev_status}
                await _write_checkpoint(db, job)
                await db.commit()
            try:
                async with ctx.sessionmaker() as db:
                    chapter_job = await generation_service.build_chapter_job(
                        db,
                        user_id=job.user_id,
                        chapter_id=cid,
                        mode="generate",
                        req=gen_req,
                        prev_status=prev_status,
                        ai_config_id=job.req.ai_config_id,
                        cipher=ctx.cipher,
                        factory=ctx.factory,
                        ai_timeout=settings.ai_request_timeout,
                        allow_private=settings.allow_private_ai_endpoints,
                        context_budget=settings.context_char_budget,
                        task_id=job.task_id,
                        skill=job.skill,
                    )
            except AppError:
                # 配置被删 / 章节被复位等：不重试；章节仍是本任务抢占的 generating，还原
                retryable = False
                await restore_current(ctx, job.task_id)
                raise
            async with aclosing(generation_service.run_chapter_job(chapter_job, ctx.sessionmaker)) as events:
                async for event, data in events:
                    if event == "done":
                        result = data
                    elif event == "error":
                        error = str(data.get("detail") or "生成失败")
                    else:
                        yield event, data
        except AppError as exc:
            error = exc.detail
        # run_chapter_job 失败时已自行回退章节状态；成功时章节为 done
        job.checkpoint["current"] = None
        if result is not None:
            chapter_out = result.get("chapter") or {}
            item.update(state="done", words=int(chapter_out.get("word_count") or 0), error="")
            if result.get("warning"):
                item["warning"] = str(result["warning"])
            return
        if not retryable or attempt > MAX_RETRIES:
            item.update(state="failed", error=error or "生成失败")
            return


async def _analyze_chapter(
    ctx: "TaskContext", job: BatchJob, index: int, item: dict[str, Any]
) -> AsyncIterator["Event"]:
    """在本任务内分析并回写；失败只记警告。模型原文不进入任务输出。"""
    cid = job.chapter_ids[index]
    settings = ctx.settings
    async with ctx.sessionmaker() as db:
        project = await db.get(Project, job.project_id)
        enabled = project is not None and analyze_enabled(settings, project)
    if not enabled:
        item["analysis"] = "skipped"
        return
    yield _stage(job, index, "分析中")
    error = ""
    try:
        async with ctx.sessionmaker() as db:
            analysis_job = await analysis_service.build_analysis_job(
                db,
                user_id=job.user_id,
                chapter_id=cid,
                ai_config_id=job.req.ai_config_id,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=settings.ai_request_timeout,
                allow_private=settings.allow_private_ai_endpoints,
                task_id=job.task_id,
            )
        done = False
        async with aclosing(analysis_service.run_analysis_job(analysis_job, ctx.sessionmaker)) as events:
            async for event, data in events:
                if event == "done":
                    done = True
                elif event == "error":
                    error = str(data.get("detail") or "分析失败")
        if not done and not error:
            error = "分析失败"
    except AppError as exc:
        error = exc.detail
    if error:
        item["analysis"] = "failed"
        note = f"分析失败：{error}"
        item["warning"] = f"{item['warning']}；{note}" if item.get("warning") else note
    else:
        item["analysis"] = "ok"
        # 本章记忆已随分析写入；有向量模型时排队生成向量（与独立分析任务的 on_success 一致）
        await kb_index_service.enqueue_memory(ctx.sessionmaker, job.project_id)


async def run_batch_job(job: BatchJob, ctx: "TaskContext") -> AsyncIterator["Event"]:
    cp = job.checkpoint
    items: list[dict[str, Any]] = cp["items"]
    while job.cursor < job.total:
        i = job.cursor
        cid = job.chapter_ids[i]
        item = items[i]
        job.position = i

        if cp["phase"] == "generate":
            async with ctx.sessionmaker() as db:
                chapter = await db.get(Chapter, cid)
                if chapter is not None and chapter.project_id != job.project_id:
                    chapter = None
                if chapter is not None:
                    item.update(chapter_no=chapter.chapter_no, title=chapter.title)
                    written = is_written(chapter)
                    plans = await plan_words(db, [chapter])
            skip = None
            if chapter is None:
                skip = "skipped_missing"
            elif written and not job.req.include_written:
                skip = "skipped_written"
            if skip is not None:
                item.update(state=skip, error="")
                job.cursor = i + 1
                await _save(ctx, job)
                yield "chapter_end", {"index": i, "item": dict(item)}
                continue

            target, source = target_for(job.req, cid, plans)
            item.update(state="pending", target_words=target, words_source=source, error="", warning="", analysis=None)
            job.target = target
            yield "reset", {}
            yield (
                "chapter_start",
                {
                    "index": i,
                    "total": job.total,
                    "chapter_id": cid,
                    "chapter_no": item["chapter_no"],
                    "title": item["title"],
                    "target_words": target,
                },
            )
            yield _stage(job, i, "生成中")
            async for event in _generate_chapter(ctx, job, i, item):
                yield event
            if item["state"] == "failed":
                if job.req.skip_on_fail:
                    job.cursor = i + 1
                    await _save(ctx, job)
                    yield "chapter_end", {"index": i, "item": dict(item)}
                    continue
                # 停在该章：重试时从这里续跑
                await _save(ctx, job)
                yield "chapter_end", {"index": i, "item": dict(item)}
                yield "error", {"detail": f"第 {item['chapter_no']} 章生成失败：{item['error']}"}
                return
            cp["phase"] = "analyze"
            await _save(ctx, job)

        # phase == analyze：正文已写好，还没分析
        async for event in _analyze_chapter(ctx, job, i, item):
            yield event
        cp["phase"] = "generate"
        cp["since_review"] = int(cp.get("since_review") or 0) + 1
        job.cursor = i + 1
        job.position = i + 1
        await _save(ctx, job)
        yield "chapter_end", {"index": i, "item": dict(item)}

        review_every = job.req.review_every
        if review_every > 0 and cp["since_review"] >= review_every and await _needs_work(ctx, job, job.cursor):
            cp["since_review"] = 0
            await _save(ctx, job)
            yield "paused", summarize(cp, job.cursor, job.total)
            return

    yield "done", summarize(cp, job.cursor, job.total)
