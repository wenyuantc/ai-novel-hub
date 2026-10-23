"""角色的组合操作：带关系一并创建、AI 生成角色、AI 解析旧关系备注（后两者为后台任务，202）。"""

from typing import Any

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.entity_ai import CharacterGenerateRequest, RelationsParseRequest
from app.schemas.relationship import CharacterWithRelations, CharacterWithRelationsOut
from app.schemas.task import TaskOut
from app.services import relationship_service, task_service

router = APIRouter(prefix="/projects/{project_id}/characters", tags=["characters"])


@router.post("/with-relations", response_model=CharacterWithRelationsOut, status_code=status.HTTP_201_CREATED)
async def create_with_relations(
    project_id: int, body: CharacterWithRelations, user: CurrentUser, db: DbDep
) -> dict[str, Any]:
    return await relationship_service.create_character_with_relations(db, user.id, project_id, body)


@router.post("/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_character(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: CharacterGenerateRequest | None = None,
) -> TaskOut:
    params = {"project_id": project_id, **(body or CharacterGenerateRequest()).model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "character.generate", params)


@router.post("/{character_id}/parse-relations", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def parse_relations(
    project_id: int,
    character_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: RelationsParseRequest | None = None,
) -> TaskOut:
    params = {
        "project_id": project_id,
        "character_id": character_id,
        **(body or RelationsParseRequest()).model_dump(mode="json"),
    }
    return await task_service.create_task(db, engine, user.id, "relations.parse", params)
