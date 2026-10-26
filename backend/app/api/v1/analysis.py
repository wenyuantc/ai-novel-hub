"""章节分析、回写条目与按建议重写。全部先做归属校验，他人资源一律 404。"""

from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.analysis import (
    AnalysisBrief,
    AnalysisChangeOut,
    AnalysisOut,
    AnalyzePendingResult,
    AnalyzeRequest,
    ChapterAnalysisState,
)
from app.schemas.chapter import ReviseChapterRequest
from app.schemas.foreshadow import ConfirmChangeRequest
from app.schemas.task import TaskOut
from app.services import analysis_service, task_service, writeback_service

router = APIRouter(tags=["analysis"])


@router.post("/chapters/{chapter_id}/analyze", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def analyze_chapter(
    chapter_id: int,
    response: Response,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: AnalyzeRequest | None = None,
) -> TaskOut:
    task, created = await analysis_service.request_analyze(db, engine, user.id, chapter_id, body or AnalyzeRequest())
    if not created:
        # 同章已有排队中的分析：直接返回那条任务
        response.status_code = status.HTTP_200_OK
    return task


@router.post("/projects/{project_id}/analysis/analyze-pending", response_model=AnalyzePendingResult)
async def analyze_pending(
    project_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep, body: AnalyzeRequest | None = None
) -> AnalyzePendingResult:
    return await analysis_service.analyze_pending(db, engine, user.id, project_id, body or AnalyzeRequest())


@router.get("/projects/{project_id}/analysis/statuses", response_model=list[ChapterAnalysisState])
async def analysis_statuses(project_id: int, user: CurrentUser, db: DbDep) -> list[ChapterAnalysisState]:
    return await analysis_service.project_states(db, user.id, project_id)


@router.get("/chapters/{chapter_id}/analysis-status", response_model=ChapterAnalysisState)
async def chapter_analysis_status(chapter_id: int, user: CurrentUser, db: DbDep) -> ChapterAnalysisState:
    return await analysis_service.chapter_state(db, user.id, chapter_id)


@router.get("/chapters/{chapter_id}/analyses", response_model=list[AnalysisBrief])
async def list_analyses(chapter_id: int, user: CurrentUser, db: DbDep) -> list[AnalysisBrief]:
    return await analysis_service.list_analyses(db, user.id, chapter_id)


@router.get("/analyses/{analysis_id}", response_model=AnalysisOut)
async def get_analysis(analysis_id: int, user: CurrentUser, db: DbDep) -> AnalysisOut:
    return await analysis_service.get_analysis(db, user.id, analysis_id)


@router.post("/analysis-changes/{change_id}/revert", response_model=AnalysisChangeOut)
async def revert_change(change_id: int, user: CurrentUser, db: DbDep) -> AnalysisChangeOut:
    return await writeback_service.revert_change(db, user.id, change_id)


@router.post("/analysis-changes/{change_id}/confirm", response_model=AnalysisChangeOut)
async def confirm_change(
    change_id: int, user: CurrentUser, db: DbDep, body: ConfirmChangeRequest | None = None
) -> AnalysisChangeOut:
    overrides = body.overrides if body is not None else None
    return await writeback_service.confirm_change(db, user.id, change_id, overrides)


@router.post("/analysis-changes/{change_id}/reject", response_model=AnalysisChangeOut)
async def reject_change(change_id: int, user: CurrentUser, db: DbDep) -> AnalysisChangeOut:
    return await writeback_service.reject_change(db, user.id, change_id)


@router.post("/chapters/{chapter_id}/revise", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def revise_chapter(
    chapter_id: int, body: ReviseChapterRequest, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> TaskOut:
    params = {"chapter_id": chapter_id, **body.model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "chapter.revise", params)
