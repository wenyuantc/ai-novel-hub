"""关系类型、人物关系、关系历史与图谱路由。"""

from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.entity_ai import RelationsGenerateRequest
from app.schemas.relationship import (
    RelationshipBatchCreate,
    RelationshipCreate,
    RelationshipGraph,
    RelationshipHistoryOut,
    RelationshipOut,
    RelationshipTypeCreate,
    RelationshipTypeOut,
    RelationshipUpdate,
)
from app.schemas.task import TaskOut
from app.services import relationship_service as svc
from app.services import task_service

router = APIRouter(prefix="/projects/{project_id}", tags=["relationships"])


@router.get("/relationship-types", response_model=list[RelationshipTypeOut])
async def list_types(project_id: int, user: CurrentUser, db: DbDep) -> list[RelationshipTypeOut]:
    return await svc.list_types(db, user.id, project_id)


@router.post("/relationship-types", response_model=RelationshipTypeOut, status_code=status.HTTP_201_CREATED)
async def create_type(
    project_id: int, body: RelationshipTypeCreate, user: CurrentUser, db: DbDep
) -> RelationshipTypeOut:
    return await svc.create_type(db, user.id, project_id, body)


@router.delete("/relationship-types/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_type(project_id: int, type_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_type(db, user.id, project_id, type_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/relationships", response_model=list[RelationshipOut])
async def list_relationships(
    project_id: int, user: CurrentUser, db: DbDep, character_id: int | None = None
) -> list[RelationshipOut]:
    return await svc.list_relationships(db, user.id, project_id, character_id=character_id)


@router.post("/relationships", response_model=RelationshipOut, status_code=status.HTTP_201_CREATED)
async def create_relationship(
    project_id: int, body: RelationshipCreate, user: CurrentUser, db: DbDep
) -> RelationshipOut:
    return await svc.create_relationship(db, user.id, project_id, body)


@router.post("/relationships/batch", response_model=list[RelationshipOut], status_code=status.HTTP_201_CREATED)
async def create_relationships(
    project_id: int, body: RelationshipBatchCreate, user: CurrentUser, db: DbDep
) -> list[RelationshipOut]:
    return await svc.create_relationships(db, user.id, project_id, body)


@router.post("/relationships/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_relationships(
    project_id: int, body: RelationsGenerateRequest, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> TaskOut:
    params = {"project_id": project_id, **body.model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "relations.generate", params)


@router.patch("/relationships/{rel_id}", response_model=RelationshipOut)
async def update_relationship(
    project_id: int, rel_id: int, body: RelationshipUpdate, user: CurrentUser, db: DbDep
) -> RelationshipOut:
    return await svc.update_relationship(db, user.id, project_id, rel_id, body)


@router.delete("/relationships/{rel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relationship(project_id: int, rel_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_relationship(db, user.id, project_id, rel_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/relationships/{rel_id}/history", response_model=list[RelationshipHistoryOut])
async def list_history(project_id: int, rel_id: int, user: CurrentUser, db: DbDep) -> list[RelationshipHistoryOut]:
    return await svc.list_history(db, user.id, project_id, rel_id)


@router.get("/relationship-graph", response_model=RelationshipGraph)
async def graph(project_id: int, user: CurrentUser, db: DbDep) -> RelationshipGraph:
    return await svc.graph(db, user.id, project_id)
