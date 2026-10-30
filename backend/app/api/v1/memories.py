"""剧情记忆：人工维护允许管理员；检索和上下文预览保持归属，跨用户保存不创建向量任务。"""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentUser, DbDep, EmbedEnvDep, TaskEngineDep
from app.schemas.memory import (
    ContextPreview,
    MemoryCreate,
    MemoryKind,
    MemoryOrigin,
    MemoryOut,
    MemoryPage,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryUpdate,
)
from app.services import context_builder, kb_index_service
from app.services import memory_service as svc

router = APIRouter(tags=["memories"])
BASE = "/projects/{project_id}/memories"


@router.get(BASE, response_model=MemoryPage)
async def list_memories(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    kind: MemoryKind | None = None,
    origin: MemoryOrigin | None = None,
    chapter_from: Annotated[int | None, Query(ge=0)] = None,
    chapter_to: Annotated[int | None, Query(ge=0)] = None,
    pinned: bool | None = None,
    muted: bool | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MemoryPage:
    return await svc.list_memories(
        db,
        user.id,
        project_id,
        kind=kind,
        origin=origin,
        chapter_from=chapter_from,
        chapter_to=chapter_to,
        pinned=pinned,
        muted=muted,
        q=q,
        page=page,
        page_size=page_size,
    )


async def _enqueue_vectors(engine, db, user_id: int, project_id: int) -> None:
    if not await svc.may_enqueue_vectors(db, user_id, project_id):
        return
    # 有向量模型时为新增 / 修改的记忆排队生成向量（关键词检索立即可用）
    if await kb_index_service.enqueue_memory(engine.ctx.sessionmaker, project_id) is not None:
        engine.notify()


@router.post(BASE, response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def create_memory(
    project_id: int, body: MemoryCreate, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> MemoryOut:
    out = await svc.create_memory(db, user.id, project_id, body)
    await _enqueue_vectors(engine, db, user.id, project_id)
    return out


@router.post(f"{BASE}/search", response_model=MemorySearchResult)
async def search_memories(
    project_id: int, body: MemorySearchRequest, user: CurrentUser, db: DbDep, env: EmbedEnvDep
) -> MemorySearchResult:
    return await svc.search_debug(db, user.id, project_id, body, env=env)


@router.patch(BASE + "/{memory_id}", response_model=MemoryOut)
async def update_memory(
    project_id: int, memory_id: int, body: MemoryUpdate, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> MemoryOut:
    out = await svc.update_memory(db, user.id, project_id, memory_id, body)
    await _enqueue_vectors(engine, db, user.id, project_id)
    return out


@router.delete(BASE + "/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(project_id: int, memory_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_memory(db, user.id, project_id, memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chapters/{chapter_id}/context-preview", response_model=ContextPreview)
async def context_preview(chapter_id: int, user: CurrentUser, db: DbDep, env: EmbedEnvDep) -> ContextPreview:
    knowledge, memories = await context_builder.retrieval_preview(db, user.id, chapter_id, env=env)
    return ContextPreview(knowledge=knowledge, memories=memories)
