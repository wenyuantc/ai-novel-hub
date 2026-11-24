"""剧情记忆：人工维护允许管理员；检索和上下文预览保持归属。

派生向量：作者补全书空向量；已授权编辑者只向量化本次条目（任务归编辑者，E6-c）；管理员人工保存不建向量任务。
"""

import logging
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
from app.services import context_builder, kb_index_service, kb_service
from app.services import memory_service as svc
from app.services.project_access import require_project

router = APIRouter(tags=["memories"])
BASE = "/projects/{project_id}/memories"
logger = logging.getLogger(__name__)


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


async def _enqueue_vectors(engine, db, write_actor: kb_service.ContentWriteActor, memory_id: int) -> None:
    actor, project_id = write_actor.actor, write_actor.project_id
    try:
        queued = None
        if write_actor.source == "owner":
            # 人工写入已经提交，派生向量只允许同一原 token 的作者（作者沿用全书补空向量）。
            queued = await kb_index_service.enqueue_memory(
                engine.ctx.sessionmaker, actor_id=actor.id, project_id=project_id, authenticated_actor=actor,
            )
        elif write_actor.source == "editor" and write_actor.grant is not None:
            # 授权编辑者（E6-c，裁决 4）：只向量化本次写入的条目，按写入时的原邀请轮次受理，任务归编辑者；
            # 无授权或作者无向量配置时不建任务（只维护关键词）。
            queued = await kb_index_service.enqueue_collaborator_memory(
                engine, authenticated_actor=actor, project_id=project_id, grant=write_actor.grant,
                memory_ids=[memory_id],
            )
        if queued is not None:
            engine.notify_queue_changed()
    except Exception:
        # 不输出 SQL 参数、配置或输入正文；人工记忆已提交，不能把派生故障误报为写入失败。
        logger.warning("记忆向量任务未能排队")
    # 检查期间 token、能力来源或邀请轮次可能变化；换新事务取得最终屏障。
    await kb_service.lock_content_write_actor(db, write_actor)


@router.post(BASE, response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def create_memory(
    project_id: int, body: MemoryCreate, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> MemoryOut:
    write_actor = await kb_service.capture_content_write_actor(db, user.id, project_id)
    await kb_service.lock_content_write_actor(db, write_actor)
    out = await svc.create_memory(db, write_actor.actor.id, project_id, body)
    await _enqueue_vectors(engine, db, write_actor, out.id)
    return out


@router.post(f"{BASE}/search", response_model=MemorySearchResult)
async def search_memories(
    project_id: int, body: MemorySearchRequest, user: CurrentUser, db: DbDep, env: EmbedEnvDep
) -> MemorySearchResult:
    # 作者沿用向量检索；协作者的关键词模式在服务层重验原授权。
    await require_project(db, user.id, project_id, "ai.use")
    return await svc.search_debug(db, user.id, project_id, body, env=env)


@router.patch(BASE + "/{memory_id}", response_model=MemoryOut)
async def update_memory(
    project_id: int, memory_id: int, body: MemoryUpdate, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> MemoryOut:
    write_actor = await kb_service.capture_content_write_actor(db, user.id, project_id)
    await kb_service.lock_content_write_actor(db, write_actor)
    out = await svc.update_memory(db, write_actor.actor.id, project_id, memory_id, body)
    await _enqueue_vectors(engine, db, write_actor, out.id)
    return out


@router.delete(BASE + "/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(project_id: int, memory_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_memory(db, user.id, project_id, memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chapters/{chapter_id}/context-preview", response_model=ContextPreview)
async def context_preview(chapter_id: int, user: CurrentUser, db: DbDep, env: EmbedEnvDep) -> ContextPreview:
    knowledge, memories = await context_builder.retrieval_preview(db, user.id, chapter_id, env=env)
    return ContextPreview(knowledge=knowledge, memories=memories)
