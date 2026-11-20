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
    EditorUser,
    FactoryDep,
    SessionmakerDep,
    SettingsDep,
    TaskEngineDep,
)
from app.schemas.rich_document import SceneDocumentOut
from app.schemas.scene import SceneMutation, SceneOut
from app.schemas.scene_ai import (
    SceneGenerateRequest,
    ScenePolishRequest,
    SceneReviseRequest,
    SceneRewriteApply,
    SceneRewriteRequest,
    SectionRewriteApply,
    SectionRewriteRequest,
)
from app.schemas.scene_restructure import (
    RestructureApply,
    RestructurePreview,
    RestructureRequest,
    RestructureResult,
    RestructureStart,
)
from app.schemas.task import TaskOut
from app.services import generation_service, rewrite_service, rich_document_section_ai, scene_restructure

router = APIRouter(tags=["scene-ai"])
_BASE = "/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}"
_RESTRUCTURE = "/projects/{project_id}/chapters/{chapter_id}/scene-restructure"
PathId = Annotated[int, Path(ge=1, le=2_147_483_647)]


def _params(project_id: int, chapter_id: int, scene_id: int, body) -> dict:
    return {"project_id": project_id, "chapter_id": chapter_id, **body.model_dump(mode="json"),
            "target": {"kind": "scene", "scene_id": scene_id}}


@router.post(f"{_BASE}/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneGenerateRequest,
                         user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await generation_service.create_chapter_task(db, engine, user.id, "chapter.generate",
                                                       _params(project_id, chapter_id, scene_id, body))


@router.post(f"{_BASE}/polish", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def polish_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: ScenePolishRequest,
                       user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await generation_service.create_chapter_task(db, engine, user.id, "chapter.polish",
                                                       _params(project_id, chapter_id, scene_id, body))


@router.post(f"{_BASE}/revise", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def revise_scene(project_id: PathId, chapter_id: PathId, scene_id: PathId, body: SceneReviseRequest,
                       user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await generation_service.create_chapter_task(db, engine, user.id, "chapter.revise",
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
    return sse_response(
        rewrite_service.run_rewrite_job(job, sessionmaker),
        frame_lease=rewrite_service.output_frame_lease(job, sessionmaker),
        frame_timeout=rewrite_service.OUTPUT_LEASE_SECONDS,
    )


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
    return sse_response(
        rewrite_service.run_rewrite_job(job, sessionmaker),
        frame_lease=rewrite_service.output_frame_lease(job, sessionmaker),
        frame_timeout=rewrite_service.OUTPUT_LEASE_SECONDS,
    )


@router.post(f"{_BASE}/sections/{{section_key}}/rewrite/apply", response_model=SceneDocumentOut)
async def apply_section_rewrite(
    project_id: PathId, chapter_id: PathId, scene_id: PathId, section_key: UUID, body: SectionRewriteApply,
    user: CurrentUser, db: DbDep,
) -> SceneDocumentOut:
    return await rich_document_section_ai.apply_section_rewrite(
        db, user_id=user.id, project_id=project_id, chapter_id=chapter_id, scene_id=scene_id,
        section_key=section_key, data=body,
    )


@router.post(f"{_RESTRUCTURE}/preview", response_model=RestructurePreview)
async def preview_restructure(
    project_id: PathId, chapter_id: PathId, body: RestructureRequest, user: EditorUser, db: DbDep,
    settings: SettingsDep,
) -> RestructurePreview:
    return await scene_restructure.preview(db, user.id, project_id, chapter_id, body, settings=settings)


@router.post(f"{_RESTRUCTURE}/suggestions", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def start_restructure(
    project_id: PathId, chapter_id: PathId, body: RestructureStart, user: EditorUser, db: DbDep, engine: TaskEngineDep,
) -> TaskOut:
    return await scene_restructure.create_task(db, engine, user.id, project_id, chapter_id, body)


@router.get(f"{_RESTRUCTURE}/suggestions/{{task_key}}", response_model=RestructureResult)
async def get_restructure(
    project_id: PathId, chapter_id: PathId, task_key: UUID, user: EditorUser, db: DbDep,
    settings: SettingsDep, cipher: CipherDep,
) -> RestructureResult:
    return await scene_restructure.get_result(
        db, user.id, project_id, chapter_id, task_key, settings=settings, cipher=cipher,
    )


@router.post(f"{_RESTRUCTURE}/suggestions/{{task_key}}/apply", response_model=SceneMutation)
async def apply_restructure(
    project_id: PathId, chapter_id: PathId, task_key: UUID, body: RestructureApply, user: EditorUser, db: DbDep,
    settings: SettingsDep, cipher: CipherDep,
) -> SceneMutation:
    return await scene_restructure.apply_result(
        db, user.id, project_id, chapter_id, task_key, body, settings=settings, cipher=cipher,
    )
