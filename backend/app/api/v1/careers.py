"""职业、阶段、持有人与角色挂接 / 挂接历史路由；AI 生成职业走后台任务（202）。"""

from typing import Literal

from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.career import (
    AssignmentCreate,
    AssignmentOut,
    AssignmentUpdate,
    CareerBatchCreate,
    CareerCreate,
    CareerGenerateRequest,
    CareerHistoryOut,
    CareerHolderOut,
    CareerOut,
    CareerUpdate,
)
from app.schemas.task import TaskOut
from app.services import career_service as svc
from app.services import task_service

router = APIRouter(prefix="/projects/{project_id}", tags=["careers"])


@router.get("/careers", response_model=list[CareerOut])
async def list_careers(project_id: int, user: CurrentUser, db: DbDep) -> list[CareerOut]:
    return await svc.list_careers(db, user.id, project_id)


@router.post("/careers", response_model=CareerOut, status_code=status.HTTP_201_CREATED)
async def create_career(project_id: int, body: CareerCreate, user: CurrentUser, db: DbDep) -> CareerOut:
    return await svc.create_career(db, user.id, project_id, body)


@router.post("/careers/batch", response_model=list[CareerOut], status_code=status.HTTP_201_CREATED)
async def create_careers(project_id: int, body: CareerBatchCreate, user: CurrentUser, db: DbDep) -> list[CareerOut]:
    return await svc.create_careers(db, user.id, project_id, body)


@router.post("/careers/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_careers(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: CareerGenerateRequest | None = None,
) -> TaskOut:
    params = {"project_id": project_id, **(body or CareerGenerateRequest()).model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "career.generate", params)


@router.get("/careers/{career_id}", response_model=CareerOut)
async def get_career(project_id: int, career_id: int, user: CurrentUser, db: DbDep) -> CareerOut:
    return await svc.get_career(db, user.id, project_id, career_id)


@router.patch("/careers/{career_id}", response_model=CareerOut)
async def update_career(project_id: int, career_id: int, body: CareerUpdate, user: CurrentUser, db: DbDep) -> CareerOut:
    return await svc.update_career(db, user.id, project_id, career_id, body)


@router.delete("/careers/{career_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_career(project_id: int, career_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_career(db, user.id, project_id, career_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/careers/{career_id}/holders", response_model=list[CareerHolderOut])
async def list_holders(project_id: int, career_id: int, user: CurrentUser, db: DbDep) -> list[CareerHolderOut]:
    return await svc.list_holders(db, user.id, project_id, career_id)


@router.get("/character-careers", response_model=list[AssignmentOut])
async def list_project_assignments(project_id: int, user: CurrentUser, db: DbDep) -> list[AssignmentOut]:
    return await svc.list_project_assignments(db, user.id, project_id)


@router.get("/characters/{character_id}/careers", response_model=list[AssignmentOut])
async def list_assignments(project_id: int, character_id: int, user: CurrentUser, db: DbDep) -> list[AssignmentOut]:
    return await svc.list_assignments(db, user.id, project_id, character_id)


@router.post("/characters/{character_id}/careers", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
async def assign(
    project_id: int, character_id: int, body: AssignmentCreate, user: CurrentUser, db: DbDep
) -> AssignmentOut:
    return await svc.assign(db, user.id, project_id, character_id, body)


@router.patch("/characters/{character_id}/careers/{assignment_id}", response_model=AssignmentOut)
async def update_assignment(
    project_id: int, character_id: int, assignment_id: int, body: AssignmentUpdate, user: CurrentUser, db: DbDep
) -> AssignmentOut:
    return await svc.update_assignment(db, user.id, project_id, character_id, assignment_id, body)


@router.delete("/characters/{character_id}/careers/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_assignment(
    project_id: int,
    character_id: int,
    assignment_id: int,
    user: CurrentUser,
    db: DbDep,
    chapter_id: int | None = None,
    chapter_no: int | None = None,
    anchor_kind: Literal["chapter", "planned"] | None = None,
    boundary: Literal["start", "end"] = "start",
    edit_mode: Literal["baseline", "progression", "delete"] | None = None,
    expected_revision: int | None = None,
) -> Response:
    await svc.remove_assignment(
        db,
        user.id,
        project_id,
        character_id,
        assignment_id,
        chapter_id=chapter_id,
        chapter_no=chapter_no,
        anchor_kind=anchor_kind,
        boundary=boundary,
        edit_mode=edit_mode,
        expected_revision=expected_revision,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/characters/{character_id}/career-history", response_model=list[CareerHistoryOut])
async def list_history(project_id: int, character_id: int, user: CurrentUser, db: DbDep) -> list[CareerHistoryOut]:
    return await svc.list_history(db, user.id, project_id, character_id)
