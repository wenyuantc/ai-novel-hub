"""明确场景目标的 AI 入口；业务归属、版本与写入由服务校验。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, status
from fastapi.responses import StreamingResponse

from app.api.sse import sse_response
from app.core.deps import (
    CipherDep,
    CurrentUser,
    DbDep,
    FactoryDep,
    SessionmakerDep,
    SettingsDep,
    TaskEngineDep,
)
from app.schemas.rich_document import SceneDocumentOut
from app.schemas.scene import SceneOut
from app.schemas.scene_ai import (
    SceneGenerateRequest,
    ScenePolishRequest,
    SceneReviseRequest,
    SceneRewriteApply,
    SceneRewriteRequest,
    SectionRewriteApply,
    SectionRewriteRequest,
)
from app.schemas.task import TaskOut
from app.services import rewrite_service, rich_document_section_ai, task_service

router = APIRouter(tags=["scene-ai"])
_BASE = "/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}"
PathId = Annotated[int, Path(ge=1, le=2_147_483_647)]


def _params(project_id: int, chapter_id: int, scene_id: int, body) -> dict:
    return {"project_id": project_id, "chapter_id": chapter_id, **body.model_dump(mode="json"),
            "target": {"kind": "scene", "scene_id": scene_id}}


@router.post(f"{_BASE}/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneGenerateRequest,
                         user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await task_service.create_task(db, engine, user.id, "chapter.generate",
                                          _params(project_id, chapter_id, scene_id, body))


@router.post(f"{_BASE}/polish", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def polish_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: ScenePolishRequest,
                       user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await task_service.create_task(db, engine, user.id, "chapter.polish",
                                          _params(project_id, chapter_id, scene_id, body))


@router.post(f"{_BASE}/revise", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def revise_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneReviseRequest,
                       user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await task_service.create_task(db, engine, user.id, "chapter.revise",
                                          _params(project_id, chapter_id, scene_id, body))


@router.post(f"{_BASE}/rewrite", response_class=StreamingResponse)
async def rewrite_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneRewriteRequest,
                        user: CurrentUser, db: DbDep, settings: SettingsDep, cipher: CipherDep,
                        factory: FactoryDep, sessionmaker: SessionmakerDep) -> StreamingResponse:
    job = await rewrite_service.prepare_rewrite_job(
        db, user_id=user.id, project_id=project_id, chapter_id=chapter_id, scene_id=scene_id, req=body,
        cipher=cipher, factory=factory, ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints, context_budget=settings.context_char_budget,
    )
    await db.close()
    return sse_response(rewrite_service.run_rewrite_job(job, sessionmaker))


@router.post(f"{_BASE}/rewrite/apply", response_model=SceneOut)
async def apply_scene_rewrite(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneRewriteApply,
                              user: CurrentUser, db: DbDep) -> SceneOut:
    return await rewrite_service.apply_scene_rewrite(db, user.id, project_id, chapter_id, scene_id, body)


@router.post(f"{_BASE}/sections/{{section_key}}/rewrite", response_class=StreamingResponse)
async def rewrite_section(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, section_key: UUID, body: SectionRewriteRequest,
    user: CurrentUser, db: DbDep, settings: SettingsDep, cipher: CipherDep, factory: FactoryDep,
    sessionmaker: SessionmakerDep,
) -> StreamingResponse:
    job = await rich_document_section_ai.prepare_section_rewrite(
        db, user_id=user.id, project_id=project_id, chapter_id=chapter_id, scene_id=scene_id,
        section_key=section_key, req=body, cipher=cipher, factory=factory, ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints, context_budget=settings.context_char_budget,
    )
    await db.close()
    return sse_response(rewrite_service.run_rewrite_job(job, sessionmaker))


@router.post(f"{_BASE}/sections/{{section_key}}/rewrite/apply", response_model=SceneDocumentOut)
async def apply_section_rewrite(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, section_key: UUID, body: SectionRewriteApply,
    user: CurrentUser, db: DbDep,
) -> SceneDocumentOut:
    return await rich_document_section_ai.apply_section_rewrite(
        db, user_id=user.id, project_id=project_id, chapter_id=chapter_id, scene_id=scene_id,
        section_key=section_key, data=body,
    )
