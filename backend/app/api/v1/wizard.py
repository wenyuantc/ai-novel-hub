from typing import Literal

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.chapter import WizardRequest
from app.schemas.task import TaskOut
from app.services import task_service

router = APIRouter(prefix="/projects/{project_id}/wizard", tags=["wizard"])


@router.post("/{kind}", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def run_wizard(
    project_id: int,
    kind: Literal["outline", "characters", "world", "careers"],
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: WizardRequest | None = None,
) -> TaskOut:
    params = {"project_id": project_id, **(body or WizardRequest()).model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, f"wizard.{kind}", params)
