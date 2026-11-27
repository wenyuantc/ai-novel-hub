from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.core.deps import DbDep, EditorUser, TaskEngineDep
from app.plugins.drama.api.episodes import ResourceId
from app.plugins.drama.services import task_results, task_start
from app.plugins.drama.services.task_material import StartDramaTask
from app.schemas.task import TaskOut

router = APIRouter(prefix="/plugins/drama", tags=["短剧任务"])


class ConfirmDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_key: UUID


@router.post("/episodes/{episode_id}/tasks", status_code=202, response_model=TaskOut)
async def episode_task(
    episode_id: ResourceId, data: StartDramaTask, user: EditorUser, db: DbDep, engine: TaskEngineDep
):
    return await task_start.start(db, engine, user.id, data, episode_id=episode_id)


@router.post("/series/{series_id}/tasks", status_code=202, response_model=TaskOut)
async def series_task(series_id: ResourceId, data: StartDramaTask, user: EditorUser, db: DbDep, engine: TaskEngineDep):
    return await task_start.start(db, engine, user.id, data, series_id=series_id)


@router.post("/episodes/{episode_id}/tasks/{task_id}/apply")
async def confirm_episode(episode_id: ResourceId, task_id: ResourceId, data: ConfirmDraft, user: EditorUser, db: DbDep):
    return await task_results.confirm(db, user.id, task_id, str(data.task_key), episode_id=episode_id)


@router.post("/series/{series_id}/tasks/{task_id}/apply")
async def confirm_series(series_id: ResourceId, task_id: ResourceId, data: ConfirmDraft, user: EditorUser, db: DbDep):
    return await task_results.confirm(db, user.id, task_id, str(data.task_key), series_id=series_id)
