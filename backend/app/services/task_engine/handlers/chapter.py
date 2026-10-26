"""chapter.generate / chapter.polish / chapter.revise：包装 generation_service。

成功后（on_success）按项目开关自动排队分析本章（analysis_service.enqueue_auto）。
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Task
from app.schemas.chapter import GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.services import analysis_service, generation_service
from app.services.chapter_service import count_words
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

logger = logging.getLogger(__name__)

MODE_LABELS = {"generate": "AI 生成", "polish": "AI 润色", "revise": "按建议重写", "analyze": "章节分析"}
_MODES: dict[str, generation_service.Mode] = {
    "chapter.generate": "generate",
    "chapter.polish": "polish",
    "chapter.revise": "revise",
}
_REQUESTS = {"generate": GenerateChapterRequest, "polish": PolishChapterRequest, "revise": ReviseChapterRequest}


def _mode(kind: str) -> generation_service.Mode:
    return _MODES[kind]


def _request(kind: str, params: dict[str, Any]) -> generation_service.ChapterRequest:
    return _REQUESTS[_mode(kind)].model_validate(params)


def chapter_title(chapter_no: int, title: str, mode: str) -> str:
    head = f"第 {chapter_no} 章"
    # 默认标题「第N章」与前缀重复，不再拼接
    if title and title.replace(" ", "") != f"第{chapter_no}章":
        head = f"{head} {title}"
    return f"{head} · {MODE_LABELS[mode]}"[:200]


class ChapterHandler:
    kinds = ("chapter.generate", "chapter.polish", "chapter.revise")

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        mode = _mode(kind)
        req = _request(kind, params)
        chapter_id = int(params["chapter_id"])
        claim = await generation_service.validate_and_claim(
            db,
            user_id=user_id,
            chapter_id=chapter_id,
            mode=mode,
            req=req,
            cipher=ctx.cipher,
            factory=ctx.factory,
            ai_timeout=ctx.settings.ai_request_timeout,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        return TaskSpec(
            project_id=claim.project_id,
            chapter_id=claim.chapter_id,
            resource_key=f"chapter:{claim.chapter_id}",
            title=chapter_title(claim.chapter_no, claim.chapter_title, mode),
            input={
                "request": {"chapter_id": claim.chapter_id, **req.model_dump(mode="json")},
                "chapter_id": claim.chapter_id,
                "prev_status": claim.prev_status,
                "ai_config_id": claim.ai_config_id,
                "source_words": claim.source_words,
            },
        )

    async def build_job(self, ctx: TaskContext, task: Task) -> generation_service.ChapterJob:
        params = dict(task.input["request"])
        async with ctx.sessionmaker() as db:
            return await generation_service.build_chapter_job(
                db,
                user_id=task.user_id,
                chapter_id=int(task.input["chapter_id"]),
                mode=_mode(task.kind),
                req=_request(task.kind, params),
                prev_status=task.input["prev_status"],
                # 按原请求重新解析：未指定 = 运行时的默认配置；指定的配置已删除 → 任务失败「模型配置不存在」
                ai_config_id=params.get("ai_config_id"),
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
                context_budget=ctx.settings.context_char_budget,
                task_id=task.id,
            )

    def run(self, job: generation_service.ChapterJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return generation_service.run_chapter_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        mode = _mode(task.kind)
        if mode == "generate":
            target = int(task.input.get("request", {}).get("target_words") or 3000)
        elif mode == "revise":
            request = task.input.get("request", {})
            target = int(request.get("target_words") or task.input.get("source_words") or 0) or 1000
        else:
            target = int(task.input.get("source_words") or 0) or 1000
        return max(1, min(95, count_words(text) * 100 // max(target, 1)))

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        """章节仍处于 generating 时还原为创建任务前的状态（与生成失败语义一致）。"""
        chapter_id = task.input.get("chapter_id")
        if chapter_id is None:
            return
        try:
            async with ctx.sessionmaker() as db:
                await db.execute(
                    update(Chapter)
                    .where(Chapter.id == int(chapter_id), Chapter.status == "generating")
                    .values(status=task.input.get("prev_status") or "draft")
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 回退章节 %s 状态失败", task.id, chapter_id)

    async def on_success(self, ctx: TaskContext, task: Task) -> None:
        """正文已被 AI 覆盖：按项目开关自动排队分析本章（D2）。"""
        await analysis_service.enqueue_auto(ctx, task.project_id, task.chapter_id)


register(ChapterHandler())
