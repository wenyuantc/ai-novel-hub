"""当前账号在作品中的写作统计和个人目标。"""

from typing import Annotated

from fastapi import APIRouter, Path

from app.core.deps import CurrentUser, DbDep, EditorUser
from app.schemas.serialization import CalendarDate
from app.schemas.writing_stats import GoalOut, GoalUpdate, Overview
from app.services import writing_stats_overview

router = APIRouter(prefix="/projects/{project_id}/writing-stats", tags=["writing-stats"])
ProjectId = Annotated[int, Path(ge=1, le=2_147_483_647)]


@router.get("/overview", response_model=Overview)
async def overview(
    project_id: ProjectId, user: CurrentUser, db: DbDep,
    start_date: CalendarDate | None = None, end_date: CalendarDate | None = None,
):
    return await writing_stats_overview.overview(db, user.id, project_id, start_date, end_date)


@router.put("/goal", response_model=GoalOut)
async def set_goal(project_id: ProjectId, user: EditorUser, db: DbDep, body: GoalUpdate):
    return await writing_stats_overview.set_goal(db, user.id, project_id, body)
