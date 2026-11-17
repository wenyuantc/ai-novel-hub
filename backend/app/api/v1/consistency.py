"""一致性检查：预览与发起（consistency.check 任务，返回 202）、概要、问题清单与状态；
修改影响：派生内容过期清单、评估影响（consistency.impact 任务，返回 202）与确认状态。

取消 / 重试 / 订阅进度走 /tasks/{id}/...。读取 content.read，改状态与确认 content.edit，发起、重新核查与评估 ai.use。
"""

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.consistency import (
    AssessRequest,
    CheckPreview,
    CheckRequest,
    CheckScope,
    ConsistencySummary,
    FactCategory,
    ImpactDetail,
    ImpactOverview,
    ImpactRunning,
    ImpactUpdate,
    IssueOut,
    IssuePage,
    IssueStatus,
    IssueUpdate,
    RecheckRequest,
)
from app.schemas.task import TaskOut
from app.services.consistency import check, impact, impact_task, query

router = APIRouter(prefix="/projects/{project_id}/consistency", tags=["consistency"])


@router.post("/checks/preview", response_model=CheckPreview)
async def preview_check(project_id: int, body: CheckScope, user: CurrentUser, db: DbDep) -> CheckPreview:
    return await check.preview(db, user.id, project_id, body)


@router.post("/checks", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def start_check(
    project_id: int, body: CheckRequest, user: CurrentUser, db: DbDep, engine: TaskEngineDep,
) -> TaskOut:
    # 锁外预检 + 中央受控受理：作者与 accepted editor 共用。
    return await check.create_check_task(db, engine, user.id, project_id, body.model_dump(mode="json"))


@router.get("/summary", response_model=ConsistencySummary)
async def get_summary(project_id: int, user: CurrentUser, db: DbDep) -> ConsistencySummary:
    return await query.summary(db, user.id, project_id)


@router.get("/issues", response_model=IssuePage)
async def list_issues(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    issue_status: Annotated[IssueStatus | None, Query(alias="status")] = None,
    category: FactCategory | None = None,
    chapter_id: Annotated[int | None, Query(ge=1)] = None,
    needs_recheck: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> IssuePage:
    return await query.list_issues(
        db, user.id, project_id, status=issue_status, category=category, chapter_id=chapter_id,
        needs_recheck=needs_recheck, page=page, page_size=page_size,
    )


@router.get("/issues/{issue_id}", response_model=IssueOut)
async def get_issue(project_id: int, issue_id: int, user: CurrentUser, db: DbDep) -> IssueOut:
    return await query.get_issue(db, user.id, project_id, issue_id)


@router.patch("/issues/{issue_id}", response_model=IssueOut)
async def update_issue(
    project_id: int, issue_id: int, body: IssueUpdate, user: CurrentUser, db: DbDep,
) -> IssueOut:
    return await query.update_issue(db, user.id, project_id, issue_id, body)


@router.post("/issues/{issue_id}/recheck", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def recheck_issue(
    project_id: int, issue_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep,
    body: RecheckRequest | None = None,
) -> TaskOut:
    # 范围 = 该问题现存证据章节；其余与发起检查相同。
    params = {"mode": "issue", "issue_id": issue_id, "ai_config_id": body.ai_config_id if body else None}
    return await check.create_check_task(db, engine, user.id, project_id, params)


@router.get("/impacts", response_model=ImpactOverview)
async def list_impacts(project_id: int, user: CurrentUser, db: DbDep) -> ImpactOverview:
    return await impact.overview(db, user.id, project_id)


@router.get("/impacts/running", response_model=ImpactRunning)
async def get_running_impact(project_id: int, user: CurrentUser, db: DbDep) -> ImpactRunning:
    # 评估进行中的轮询入口：只查任务，不读正文。须注册在 /impacts/{chapter_id} 之前。
    return await impact.running(db, user.id, project_id)


@router.get("/impacts/{chapter_id}", response_model=ImpactDetail)
async def get_impact(project_id: int, chapter_id: int, user: CurrentUser, db: DbDep) -> ImpactDetail:
    return await impact.get_impact(db, user.id, project_id, chapter_id)


@router.post("/impacts/{chapter_id}/assess", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def assess_impact(
    project_id: int, chapter_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep,
    body: AssessRequest | None = None,
) -> TaskOut:
    # 与检查相同的锁外预检 + 中央受控受理；没有改动前的台账时 400。
    return await impact_task.create_assess_task(
        db, engine, user.id, project_id, chapter_id, body.ai_config_id if body else None,
    )


@router.patch("/impacts/{chapter_id}", response_model=ImpactDetail)
async def update_impact(
    project_id: int, chapter_id: int, body: ImpactUpdate, user: CurrentUser, db: DbDep,
) -> ImpactDetail:
    return await impact.update_impact(db, user.id, project_id, chapter_id, body)
